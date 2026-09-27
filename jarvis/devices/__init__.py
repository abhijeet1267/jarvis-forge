"""Device identity and pairing (v0.6)."""

from .pairing import (
    MAX_PENDING_REQUESTS,
    PairingManager,
    PairingRequest,
    derive_proof,
    fingerprint_of,
    generate_secret,
    verify_proof,
)

__all__ = [
    "PairingManager",
    "PairingRequest",
    "generate_secret",
    "fingerprint_of",
    "derive_proof",
    "verify_proof",
    "MAX_PENDING_REQUESTS",
]
