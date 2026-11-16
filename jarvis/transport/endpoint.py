"""Device endpoint — the phone side of the protocol (v0.6).

This is the **stand-in for the phone**. There is no Android app yet, so this
module implements the device half of the protocol as a real, runnable process.
A future Android client implements the same wire format; nothing about the
protocol is Mac-specific.

It is deliberately *not* wired into the orchestrator — the roadmap requires the
phone implementation stay out of the core.
"""

from __future__ import annotations

import asyncio
import json
import ssl
from pathlib import Path
from typing import Any, Dict, Optional

from ..config import Settings, get_settings
from ..devices.pairing import derive_proof
from ..logging_setup import get_logger
from .protocol import (
    CONNECT_TIMEOUT,
    MAX_CHUNK_BYTES,
    MESSAGE_TIMEOUT,
    STREAM_LIMIT,
    ProtocolError,
    decode,
    encode,
    encode_chunk,
    sha256_file,
)
from .tls import TLSUnavailable, client_context

log = get_logger("transport.endpoint")

READ_CHUNK = 64 * 1024


class DeviceEndpoint:
    """Client half of the transfer protocol."""

    def __init__(
        self,
        device_id: str,
        secret: bytes,
        settings: Optional[Settings] = None,
    ) -> None:
        self.device_id = device_id
        self.secret = secret
        self.settings = settings or get_settings()

    # -- pairing -------------------------------------------------------------
    @staticmethod
    async def request_pairing(
        api_base: str,
        device_id: str,
        device_name: str,
        public_key: str = "",
        api_token: str = "",
    ) -> Dict[str, Any]:
        """Ask JARVIS to pair. Grants nothing until the user approves."""
        import httpx

        # Pairing is a network call to the backend; the egress firewall decides
        # whether that address may be dialled, so an undeclared host is refused
        # rather than attempted.
        from ..security.egress import require

        require(api_base)

        headers = {"X-JARVIS-Token": api_token} if api_token else {}
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                f"{api_base.rstrip('/')}/devices/pair/request",
                json={
                    "device_id": device_id,
                    "device_name": device_name,
                    "public_key": public_key,
                },
                headers=headers,
            )
            if response.status_code >= 400:
                raise ProtocolError(
                    f"pairing request refused ({response.status_code}): {response.text[:200]}"
                )
            return response.json()

    # -- send ----------------------------------------------------------------
    async def send_file(
        self,
        host: str,
        port: int,
        path: Path,
        *,
        cert_fingerprint: str = "",
        on_progress=None,
    ) -> Dict[str, Any]:
        """Send one file to a listening :class:`TransferServer`.

        Returns a result dict. Never reports success unless the receiver sent
        back ``verified``.
        """
        source = Path(path).expanduser()
        if not source.is_file():
            return {"ok": False, "error": f"not a file: {source}"}

        size = source.stat().st_size
        digest = sha256_file(source)

        try:
            context = client_context(cert_fingerprint, self.settings)
        except TLSUnavailable as exc:
            return {"ok": False, "error": str(exc)}

        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(
                    host, port, ssl=context, server_hostname=None, limit=STREAM_LIMIT
                ),
                timeout=CONNECT_TIMEOUT,
            )
        except asyncio.TimeoutError:
            # The port answered but the TLS handshake never completed. Report it
            # rather than hanging the caller forever.
            return {
                "ok": False,
                "error": (
                    f"{host}:{port} accepted the connection but did not complete the "
                    f"TLS handshake within {CONNECT_TIMEOUT:.0f}s. Nothing was sent."
                ),
            }
        except (ConnectionError, OSError, ssl.SSLError) as exc:
            return {"ok": False, "error": f"could not connect to {host}:{port}: {exc}"}

        async def exchange(message: Dict[str, Any]) -> Dict[str, Any]:
            writer.write(encode(message))
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout=MESSAGE_TIMEOUT)
            if not line:
                raise ProtocolError("connection closed by peer")
            return decode(line)

        try:
            # --- handshake --------------------------------------------------
            challenge_message = await exchange(
                {"type": "hello", "device_id": self.device_id}
            )
            if challenge_message.get("type") != "challenge":
                return {"ok": False, "error": f"unexpected reply: {challenge_message.get('type')}"}

            challenge = str(challenge_message.get("challenge", ""))
            proof = derive_proof(self.secret, challenge)

            ready = await exchange({"type": "auth", "proof": proof})
            if ready.get("type") != "ready":
                return {
                    "ok": False,
                    "error": ready.get("message", "authentication failed"),
                    "rejected": True,
                }

            # --- offer ------------------------------------------------------
            accepted = await exchange(
                {"type": "offer", "filename": source.name, "size": size, "sha256": digest}
            )
            if accepted.get("type") != "accept":
                return {
                    "ok": False,
                    "error": accepted.get("message", "offer refused"),
                    "rejected": True,
                }

            transfer_id = accepted.get("transfer_id", "")
            final_name = accepted.get("filename", source.name)

            # --- stream -----------------------------------------------------
            sent = 0
            with source.open("rb") as handle:
                while True:
                    block = handle.read(MAX_CHUNK_BYTES)
                    if not block:
                        break
                    writer.write(encode({"type": "chunk", "data": encode_chunk(block)}))
                    await writer.drain()
                    sent += len(block)
                    if on_progress:
                        on_progress(sent, size)

            # --- finish -----------------------------------------------------
            result = await exchange({"type": "done", "sha256": digest})
            if result.get("type") == "verified":
                return {
                    "ok": True,
                    "transfer_id": transfer_id,
                    "bytes": result.get("bytes", sent),
                    "filename": final_name,
                    "sha256": digest,
                    "verified": True,
                }
            return {
                "ok": False,
                "transfer_id": transfer_id,
                "error": result.get("message", "receiver did not verify the file"),
            }

        except asyncio.TimeoutError:
            return {"ok": False, "error": "timed out during transfer"}
        except ProtocolError as exc:
            return {"ok": False, "error": str(exc)}
        except (ConnectionError, OSError) as exc:
            return {"ok": False, "error": f"connection lost: {exc}"}
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    def describe(self) -> Dict[str, Any]:
        return {
            "device_id": self.device_id,
            "has_secret": bool(self.secret),
            "role": "device endpoint (phone stand-in)",
        }
