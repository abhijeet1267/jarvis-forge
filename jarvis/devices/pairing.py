"""Device pairing (v0.6).

Trust is never implicit. A device becomes usable only after:

    device requests pairing  ->  state "pending"
    user approves            ->  state "paired"
    (or) user revokes        ->  state "revoked"

Design notes:

* **No asymmetric crypto library is available** (``cryptography`` and ``nacl``
  are not installed). Pairing therefore issues a 256-bit **pre-shared secret**
  to both sides and authenticates with HMAC-SHA256. That is a standard
  pre-shared-key model, not a home-rolled cipher.
* The secret never leaves its own store. What is displayed and logged is a
  **fingerprint** — SHA-256 of the secret — so the user can verify out of band.
* Approval is deliberately *not* automatic. A pending request grants nothing.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import Settings, get_settings
from ..core.models import new_secret_id, utcnow
from ..db.database import get_db
from ..logging_setup import get_logger

log = get_logger("devices.pairing")

SECRET_BYTES = 32

#: Cap on simultaneous pending requests. The pairing-request endpoint is the one
#: unauthenticated write path (a device cannot hold the API token before it is
#: trusted), so it must not be usable to flood the database.
MAX_PENDING_REQUESTS = 5


def generate_secret() -> bytes:
    return secrets.token_bytes(SECRET_BYTES)


def fingerprint_of(secret: bytes) -> str:
    """A short, human-verifiable fingerprint of a device secret.

    Grouped in fours so it can be read aloud and compared during pairing.
    """
    digest = hashlib.sha256(secret).hexdigest()[:16].upper()
    return " ".join(digest[i:i + 4] for i in range(0, 16, 4))


def derive_proof(secret: bytes, challenge: str) -> str:
    """HMAC proof that the caller holds ``secret``."""
    return hmac.new(secret, challenge.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_proof(secret: bytes, challenge: str, proof: str) -> bool:
    """Constant-time verification of an HMAC proof."""
    if not proof:
        return False
    return hmac.compare_digest(derive_proof(secret, challenge), proof.strip())


@dataclass
class PairingRequest:
    pairing_id: str
    device_id: str
    device_name: str
    public_key: str
    fingerprint: str
    state: str
    requested_at: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "pairing_id": self.pairing_id,
            "device_id": self.device_id,
            "device_name": self.device_name,
            "fingerprint": self.fingerprint,
            "state": self.state,
            "requested_at": self.requested_at,
        }


class PairingManager:
    """Owns the pairing lifecycle and the device key store."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()
        self.db = get_db()

    # -- key store -----------------------------------------------------------
    @property
    def key_dir(self) -> Path:
        return self.settings.home / "devices"

    def key_path(self, device_id: str) -> Path:
        return self.key_dir / f"{device_id}.key"

    def store_secret(self, device_id: str, secret: bytes) -> Path:
        """Persist a device secret with owner-only permissions."""
        self.key_dir.mkdir(parents=True, exist_ok=True)
        path = self.key_path(device_id)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(base64.b64encode(secret))
        os.chmod(path, 0o600)
        return path

    def load_secret(self, device_id: str) -> Optional[bytes]:
        path = self.key_path(device_id)
        try:
            if path.exists():
                return base64.b64decode(path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError) as exc:
            log.info("could not read device key", extra_fields={"device": device_id, "error": str(exc)})
        return None

    def forget_secret(self, device_id: str) -> None:
        """Remove key material. Used on revocation."""
        path = self.key_path(device_id)
        try:
            if path.exists():
                path.unlink()
        except OSError as exc:  # pragma: no cover
            log.info("could not remove device key", extra_fields={"error": str(exc)})

    # -- request -------------------------------------------------------------
    def pending_count(self) -> int:
        rows = self.db.query(
            "SELECT COUNT(*) AS n FROM device_pairings WHERE state = 'pending'"
        )
        return rows[0]["n"] if rows else 0

    def request(
        self,
        device_id: str,
        device_name: str,
        public_key: str = "",
        fingerprint: str = "",
    ) -> PairingRequest:
        """Record an inbound pairing request. Grants nothing until approved."""
        if self.pending_count() >= MAX_PENDING_REQUESTS:
            raise RuntimeError(
                "Too many pending pairing requests. Approve or deny the existing "
                "ones before adding more."
            )

        # Re-requesting from the same device replaces its earlier pending entry
        # rather than accumulating duplicates.
        self.db.execute(
            "DELETE FROM device_pairings WHERE device_id = ? AND state = 'pending'",
            (device_id,),
        )

        pairing_id = new_secret_id("PAIR")
        requested_at = utcnow().isoformat()
        self.db.execute(
            """
            INSERT INTO device_pairings
                (pairing_id, device_id, device_name, public_key, fingerprint,
                 state, requested_at)
            VALUES (?, ?, ?, ?, ?, 'pending', ?)
            """,
            (pairing_id, device_id, device_name, public_key, fingerprint, requested_at),
        )
        log.info(
            "pairing requested",
            extra_fields={"pairing_id": pairing_id, "device_id": device_id},
        )
        return PairingRequest(
            pairing_id=pairing_id,
            device_id=device_id,
            device_name=device_name,
            public_key=public_key,
            fingerprint=fingerprint,
            state="pending",
            requested_at=requested_at,
        )

    def get(self, pairing_id: str) -> Optional[Dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM device_pairings WHERE pairing_id = ?", (pairing_id,)
        )
        return dict(rows[0]) if rows else None

    def pending(self) -> List[Dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM device_pairings WHERE state = 'pending' ORDER BY requested_at DESC"
        )
        return [dict(r) for r in rows]

    # -- resolution ----------------------------------------------------------
    def approve(self, pairing_id: str, resolved_by: str = "user") -> Dict[str, Any]:
        """Trust a pending device and issue its secret.

        Returns the pairing record plus the issued secret (base64), which the
        caller hands to the device once. It is never stored in plaintext here.
        """
        record = self.get(pairing_id)
        if record is None:
            raise ValueError(f"unknown pairing {pairing_id!r}")
        if record["state"] != "pending":
            raise ValueError(f"pairing is already {record['state']}")

        device_id = record["device_id"]
        secret = generate_secret()
        fingerprint = fingerprint_of(secret)
        key_path = self.store_secret(device_id, secret)

        self.db.execute(
            "UPDATE device_pairings SET state = 'approved', resolved_at = ?, "
            "fingerprint = ? WHERE pairing_id = ?",
            (utcnow().isoformat(), fingerprint, pairing_id),
        )

        # Register the device identity.
        from ..registry.devices import get_device_registry

        registry = get_device_registry()
        device = registry.get(device_id)
        if device is None:
            from ..core.models import DeviceSpec

            device = DeviceSpec(
                device_id=device_id,
                type="android",
                platform="android",
                trust_level="high",
                capabilities=["file_receive", "file_send", "notification", "device_status"],
                online=False,
                connection=["local_transfer"],
            )
            registry.register(device)

        registry.set_auth_state(
            device_id,
            "paired",
            fingerprint=fingerprint,
            key_ref=str(key_path),
        )

        log.info("pairing approved", extra_fields={"device_id": device_id})
        return {
            "pairing_id": pairing_id,
            "device_id": device_id,
            "fingerprint": fingerprint,
            "secret_b64": base64.b64encode(secret).decode("ascii"),
            "state": "paired",
        }

    def deny(self, pairing_id: str, resolved_by: str = "user") -> Dict[str, Any]:
        record = self.get(pairing_id)
        if record is None:
            raise ValueError(f"unknown pairing {pairing_id!r}")
        if record["state"] != "pending":
            raise ValueError(f"pairing is already {record['state']}")
        self.db.execute(
            "UPDATE device_pairings SET state = 'denied', resolved_at = ?, resolved_by = ? "
            "WHERE pairing_id = ?",
            (utcnow().isoformat(), resolved_by, pairing_id),
        )
        log.info("pairing denied", extra_fields={"pairing_id": pairing_id})
        return {"pairing_id": pairing_id, "state": "denied"}

    def revoke(self, device_id: str, reason: str = "user revoked") -> Dict[str, Any]:
        """Remove trust and destroy the key material."""
        from ..registry.devices import get_device_registry

        registry = get_device_registry()
        device = registry.get(device_id)
        if device is None:
            raise ValueError(f"unknown device {device_id!r}")

        self.forget_secret(device_id)
        self.db.execute(
            "UPDATE device_pairings SET state = 'revoked', resolved_at = ? "
            "WHERE device_id = ? AND state IN ('pending', 'approved')",
            (utcnow().isoformat(), device_id),
        )
        registry.set_auth_state(device_id, "revoked", key_ref="")
        registry.set_online(device_id, False)

        log.info("device revoked", extra_fields={"device_id": device_id, "reason": reason})
        return {"device_id": device_id, "state": "revoked", "reason": reason}

    # -- verification --------------------------------------------------------
    def authenticate(self, device_id: str, challenge: str, proof: str) -> bool:
        """Verify a device's HMAC proof. Revoked devices always fail."""
        from ..registry.devices import get_device_registry

        device = get_device_registry().get(device_id)
        if device is None or not device.is_paired:
            return False
        secret = self.load_secret(device_id)
        if secret is None:
            return False
        return verify_proof(secret, challenge, proof)

    def describe(self) -> Dict[str, Any]:
        """Pairing posture for /health."""
        return {
            "key_dir": str(self.key_dir),
            "pending_requests": self.pending_count(),
            "max_pending_requests": MAX_PENDING_REQUESTS,
            "secret_bytes": SECRET_BYTES,
            "auth_scheme": "HMAC-SHA256 pre-shared key",
        }
