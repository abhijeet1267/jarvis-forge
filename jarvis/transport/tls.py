"""TLS material for device transfers (v0.6).

``cryptography`` and ``nacl`` are not installed, but the platform's OpenSSL CLI
is, and Python's ``ssl`` module is stdlib. So the transport uses **real TLS**
with a self-signed certificate generated locally — no hand-rolled cipher.

The certificate is pinned by fingerprint: the client is told the expected
fingerprint out of band (at pairing) and refuses to talk to anything else.
That is what makes a self-signed certificate safe here.
"""

from __future__ import annotations

import hashlib
import shutil
import ssl
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from ..config import Settings, get_settings
from ..logging_setup import get_logger

log = get_logger("transport.tls")


class TLSUnavailable(RuntimeError):
    """Raised when no usable TLS material can be produced."""


def openssl_available() -> bool:
    return shutil.which("openssl") is not None


@dataclass
class TLSMaterial:
    cert_path: Path
    key_path: Path
    fingerprint: str  # SHA-256 of the DER certificate, lowercase hex


def _certificate_fingerprint(cert_path: Path) -> str:
    """SHA-256 of the certificate in DER form (stable across PEM formatting)."""
    pem = cert_path.read_text(encoding="utf-8")
    der = ssl.PEM_cert_to_DER_cert(pem)
    return hashlib.sha256(der).hexdigest()


def ensure_certificate(settings: Optional[Settings] = None) -> TLSMaterial:
    """Return the local certificate, generating it once if needed."""
    settings = settings or get_settings()
    directory = settings.home / "devices" / "tls"
    directory.mkdir(parents=True, exist_ok=True)

    cert_path = directory / "server.crt"
    key_path = directory / "server.key"

    if cert_path.exists() and key_path.exists():
        return TLSMaterial(cert_path, key_path, _certificate_fingerprint(cert_path))

    if not openssl_available():
        raise TLSUnavailable(
            "openssl is not installed, so an encrypted transport cannot be set "
            "up. Install openssl or run transfers over a trusted local link."
        )

    proc = subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048",
            "-keyout", str(key_path), "-out", str(cert_path),
            "-days", "825", "-nodes",
            "-subj", "/CN=jarvis-forge-local",
            "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0 or not cert_path.exists():
        raise TLSUnavailable(
            f"could not generate a TLS certificate: {proc.stderr.strip()[:300]}"
        )

    key_path.chmod(0o600)
    material = TLSMaterial(cert_path, key_path, _certificate_fingerprint(cert_path))
    log.info("tls certificate generated", extra_fields={"fingerprint": material.fingerprint[:16]})
    return material


def server_context(settings: Optional[Settings] = None) -> Tuple[ssl.SSLContext, TLSMaterial]:
    """A server-side context presenting the local certificate."""
    material = ensure_certificate(settings)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(material.cert_path), keyfile=str(material.key_path))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context, material


def client_context(expected_fingerprint: str, settings: Optional[Settings] = None) -> ssl.SSLContext:
    """A client context that accepts *only* the pinned certificate.

    Certificate verification is on, but the trust anchor is the pinned
    fingerprint rather than the system store — which is the correct model for a
    self-signed certificate exchanged during pairing.
    """
    material = ensure_certificate(settings)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cafile=str(material.cert_path))
    context.check_hostname = False  # pinned by fingerprint, not by hostname
    context.verify_mode = ssl.CERT_REQUIRED
    context.minimum_version = ssl.TLSVersion.TLSv1_2

    if expected_fingerprint:
        actual = material.fingerprint
        if actual.lower() != expected_fingerprint.lower():
            raise TLSUnavailable(
                "TLS certificate fingerprint does not match the pinned value. "
                "Refusing to connect."
            )
    return context


def describe_tls(settings: Optional[Settings] = None) -> dict:
    """TLS posture for /health. Never raises."""
    settings = settings or get_settings()
    try:
        material = ensure_certificate(settings)
        return {
            "available": True,
            "fingerprint": material.fingerprint[:16] + "…",
            "cert_path": str(material.cert_path),
            "openssl": openssl_available(),
        }
    except TLSUnavailable as exc:
        return {"available": False, "reason": str(exc), "openssl": openssl_available()}
