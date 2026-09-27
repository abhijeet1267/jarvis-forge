"""Transfer protocol (v0.6).

Newline-delimited JSON over TLS. Every connection must authenticate before it
may offer or receive anything:

    client -> hello  {device_id}
    server -> challenge {challenge}
    client -> auth   {proof}          HMAC-SHA256(device_secret, challenge)
    server -> ready  {session}

    client -> offer  {filename, size, sha256}
    server -> accept {transfer_id}    (server chooses the final filename)
    client -> chunk  {seq, data}      base64, repeated
    client -> done   {sha256}
    server -> verified {transfer_id, bytes}

Rules encoded here, not left to the caller:

* A revoked or unpaired device can never get past ``auth``.
* The **server** decides the destination filename, so a client cannot choose a
  path or silently overwrite an existing file.
* Size limits and a per-message ceiling are enforced before any bytes are
  written.
* The transfer is written to a temporary file and only moved into place after
  the checksum matches — a failed transfer leaves no partial file behind.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from ..logging_setup import get_logger

log = get_logger("transport.protocol")

PROTOCOL_VERSION = 1

#: Largest single file we will move. Deliberately explicit rather than implicit.
MAX_FILE_BYTES = 512 * 1024 * 1024  # 512 MB
#: Ceiling on one base64 chunk payload, to bound memory per message.
MAX_CHUNK_BYTES = 256 * 1024  # 256 KB
#: Longest we will wait for a single protocol message.
MESSAGE_TIMEOUT = 60.0
#: Longest we will wait for the TCP connect *and* TLS handshake.
#
#: A peer that accepts the TCP connection but never completes the handshake
#: would otherwise hang the caller indefinitely -- the socket is "reachable" as
#: far as a port check is concerned.
CONNECT_TIMEOUT = 10.0

#: StreamReader line limit. asyncio defaults to 64 KiB, but a base64-encoded
#: chunk is ~4/3 its size plus JSON overhead -- without raising this, any file
#: larger than one chunk fails with a line-limit error.
STREAM_LIMIT = (MAX_CHUNK_BYTES * 4 // 3) + (64 * 1024)


class TransferState(str, Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    CONNECTING = "connecting"
    TRANSFERRING = "transferring"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


#: Legal transitions. Anything else is a bug and is refused.
TRANSITIONS: Dict[TransferState, set] = {
    TransferState.PENDING: {TransferState.CONFIRMED, TransferState.FAILED, TransferState.CANCELLED},
    TransferState.CONFIRMED: {TransferState.CONNECTING, TransferState.FAILED, TransferState.CANCELLED},
    TransferState.CONNECTING: {TransferState.TRANSFERRING, TransferState.FAILED, TransferState.CANCELLED},
    TransferState.TRANSFERRING: {TransferState.VERIFYING, TransferState.FAILED, TransferState.CANCELLED},
    TransferState.VERIFYING: {TransferState.COMPLETED, TransferState.FAILED},
    TransferState.COMPLETED: set(),
    TransferState.FAILED: set(),
    TransferState.CANCELLED: set(),
}


def can_transition(current: TransferState, nxt: TransferState) -> bool:
    return nxt in TRANSITIONS.get(current, set())


class ProtocolError(RuntimeError):
    """A protocol-level refusal that should be reported, not retried blindly."""


def encode(message: Dict[str, Any]) -> bytes:
    """Frame one message. Newline-delimited so the reader can split cleanly."""
    return (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")


def decode(line: bytes) -> Dict[str, Any]:
    try:
        payload = json.loads(line.decode("utf-8").strip())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"malformed message: {exc}") from exc
    if not isinstance(payload, dict) or "type" not in payload:
        raise ProtocolError("message must be an object with a 'type'")
    return payload


def new_challenge() -> str:
    return secrets.token_hex(16)


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def safe_filename(name: str) -> str:
    """Reduce a client-supplied name to a bare, harmless filename.

    Strips any directory component so a client cannot influence *where* a file
    lands — only what it is called.
    """
    candidate = os.path.basename(str(name or "").strip())
    candidate = candidate.replace("\x00", "")
    if candidate in ("", ".", ".."):
        return "received_file"
    # Keep it to a conservative character set.
    allowed = "".join(ch for ch in candidate if ch.isalnum() or ch in "._- ")
    return allowed.strip() or "received_file"


def unique_destination(directory: Path, filename: str) -> Path:
    """Pick a destination that does not overwrite anything.

    Silently clobbering a file on the receiving side is never acceptable, so a
    collision gets a numeric suffix instead.
    """
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / filename
    if not target.exists():
        return target

    stem, suffix = target.stem, target.suffix
    for counter in range(1, 1000):
        candidate = directory / f"{stem} ({counter}){suffix}"
        if not candidate.exists():
            return candidate
    raise ProtocolError(f"could not find a free name for {filename!r}")


@dataclass
class Offer:
    """A validated inbound file offer."""

    filename: str
    size: int
    sha256: str
    transfer_id: str = ""
    destination: Optional[Path] = None
    received: int = 0
    digest: Any = field(default=None, repr=False)

    @classmethod
    def parse(cls, message: Dict[str, Any]) -> "Offer":
        filename = safe_filename(message.get("filename", ""))
        try:
            size = int(message.get("size", 0))
        except (TypeError, ValueError):
            raise ProtocolError("size must be an integer")

        if size < 0:
            raise ProtocolError("size must not be negative")
        if size > MAX_FILE_BYTES:
            raise ProtocolError(
                f"file is {size} bytes, above the {MAX_FILE_BYTES} byte limit"
            )

        digest = str(message.get("sha256", "")).strip().lower()
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ProtocolError("sha256 must be a 64-character hex digest")

        return cls(filename=filename, size=size, sha256=digest, digest=hashlib.sha256())


def decode_chunk(message: Dict[str, Any]) -> bytes:
    """Decode one chunk, enforcing the per-message ceiling."""
    raw = message.get("data", "")
    if not isinstance(raw, str):
        raise ProtocolError("chunk data must be a base64 string")
    if len(raw) > (MAX_CHUNK_BYTES * 4 // 3) + 16:
        raise ProtocolError("chunk exceeds the maximum message size")
    try:
        payload = base64.b64decode(raw, validate=True)
    except (ValueError, TypeError) as exc:
        raise ProtocolError(f"chunk is not valid base64: {exc}") from exc
    if len(payload) > MAX_CHUNK_BYTES:
        raise ProtocolError("chunk exceeds the maximum chunk size")
    return payload


def encode_chunk(payload: bytes) -> str:
    return base64.b64encode(payload).decode("ascii")
