"""Encrypted device transport (v0.6).

TLS (via the platform OpenSSL) + HMAC-SHA256 device authentication, with
checksum-verified, non-overwriting file transfer.
"""

from .endpoint import DeviceEndpoint
from .protocol import (
    MAX_CHUNK_BYTES,
    MAX_FILE_BYTES,
    ProtocolError,
    TransferState,
    can_transition,
    sha256_file,
    safe_filename,
    unique_destination,
)
from .server import TransferServer
from .tls import TLSUnavailable, describe_tls, ensure_certificate

__all__ = [
    "DeviceEndpoint",
    "TransferServer",
    "TransferState",
    "ProtocolError",
    "can_transition",
    "sha256_file",
    "safe_filename",
    "unique_destination",
    "MAX_FILE_BYTES",
    "MAX_CHUNK_BYTES",
    "ensure_certificate",
    "describe_tls",
    "TLSUnavailable",
]
