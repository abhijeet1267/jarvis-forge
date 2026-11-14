"""Self-signed certificate management for the companion gateway.

Uses the system ``openssl`` CLI (available on macOS and most Linux desktops)
to generate a self-signed X.509 certificate at startup. The certificate
fingerprint is displayed to the user (as a QR code or text) so the Android
app can pin it and verify the Mac's identity on the local network.

No ``cryptography`` or ``nacl`` dependency needed — the ``ssl`` stdlib module
and ``openssl`` CLI together handle certificate generation, fingerprint
computation, and TLS serving.
"""

from __future__ import annotations

import hashlib
import os
import ssl
import subprocess
from pathlib import Path
from typing import Optional

from ..config import Settings, get_settings
from ..logging_setup import get_logger

log = get_logger("companion.cert")

CERT_DAYS = 365
KEY_SIZE = 2048
#: The SHA-256 fingerprint of the certificate, used for out-of-band
#: verification during pairing. Displayed as hex, grouped in fours for
#: readability (same convention as SSH fingerprints and the device pairing
#: module).
FINGERPRINT_GROUP_LENGTH = 4

class CompanionCertError(Exception):
    """Certificate generation or loading failure."""


def _cert_dir(settings: Optional[Settings] = None) -> Path:
    s = settings or get_settings()
    d = s.home / "companion"
    d.mkdir(parents=True, exist_ok=True)
    d.chmod(0o700)
    return d


def cert_path(settings: Optional[Settings] = None) -> Path:
    return _cert_dir(settings) / "companion.crt"


def key_path(settings: Optional[Settings] = None) -> Path:
    return _cert_dir(settings) / "companion.key"


def _common_name(hostname: str) -> str:
    """A certificate CN that fits OpenSSL's 64-byte subject limit.

    The hostname is the identifying part, but a CI runner's hostname plus the
    ``vantage-companion-`` prefix routinely exceeds 64 bytes, and OpenSSL then
    refuses to build the request at all. Trim the host to what fits and append
    a short digest of the full hostname, so a shortened name stays unique.
    """
    import hashlib

    prefix = "vantage-companion-"
    full = f"{prefix}{hostname}"
    if len(full) <= 64:
        return full
    digest = hashlib.sha256(hostname.encode("utf-8")).hexdigest()[:8]
    keep = 64 - len(prefix) - len(digest) - 1
    return f"{prefix}{hostname[:max(1, keep)]}-{digest}"


def generate(settings: Optional[Settings] = None) -> None:
    """Generate a fresh self-signed certificate and private key.

    Called once at the companion gateway's first startup, or whenever the
    user requests a certificate rotation from Settings.
    """
    s = settings or get_settings()
    cert_p = cert_path(s)
    key_p = key_path(s)

    # Remove any stale key/cert first so openssl does not prompt.
    for p in (key_p, cert_p):
        if p.exists():
            p.unlink()

    # OpenSSL caps a subject attribute at 64 bytes (`ASN1_mbstring_ncopy:
    # string too long`), and a CI runner hostname is long enough to blow that
    # on its own -- GitHub's `runnervm...` names produced
    # "vantage-companion-lvl04-dq151-...-6AA266DDE8CF.local" and every
    # companion test failed at certificate generation. Bound the name and keep
    # it unique: when the hostname is trimmed, append a short digest of the full
    # name so two trimmed hosts never collide.
    common_name = _common_name(os.uname().nodename)
    proc = subprocess.run(
        [
            "openssl", "req", "-x509",
            "-newkey", f"rsa:{KEY_SIZE}",
            "-keyout", str(key_p),
            "-out", str(cert_p),
            "-days", str(CERT_DAYS),
            "-nodes",
            "-subj", f"/CN={common_name}",
            # The gateway verifies its own certificate as a trust anchor
            # (``_verified_context`` calls ``load_verify_locations``), so the
            # certificate must SAY it is a CA. Without ``CA:TRUE`` OpenSSL 3.x
            # -- which Python 3.13+ links against -- refuses it with
            # "invalid CA certificate"; Python 3.11/3.12 (OpenSSL 1.1/3.0
            # defaults) silently allowed it, so the breakage only appeared on
            # a newer interpreter. A self-signed leaf used as a pinned root is
            # the standard shape for a LAN device certificate; the fingerprint
            # pin, not the CA bit, is what establishes trust.
            "-addext", "basicConstraints=critical,CA:TRUE",
            "-addext", "keyUsage=critical,digitalSignature,keyEncipherment,keyCertSign",
            # The address clients actually dial, so a chain-verified client that
            # checks the host name (not just the fingerprint) also works.
            "-addext", "subjectAltName=IP:127.0.0.1,DNS:localhost",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise CompanionCertError(
            f"openssl certificate generation failed: {proc.stderr.strip()}"
        )

    key_p.chmod(0o600)
    cert_p.chmod(0o644)
    log.info("companion TLS certificate generated", extra_fields={
        "cn": common_name,
        "cert_path": str(cert_p),
    })


def fingerprint(settings: Optional[Settings] = None) -> str:
    """Standard X.509 SHA-256 fingerprint of the current companion certificate.

    This is the SHA-256 of the certificate in DER form — the same value
    ``openssl x509 -fingerprint -sha256`` prints and what a TLS client can
    independently compute from the certificate it receives during the
    handshake. The caller shows it to the user (QR code or text) and the
    Android app pins it.

    (The previous implementation hashed the PEM *file bytes*; a TLS client
    never sees those, so nothing a phone computes could match.)

    Returns an empty string when no cert exists yet.
    """
    cp = cert_path(settings)
    if not cp.exists():
        return ""
    try:
        der = _pem_to_der(cp.read_bytes())
    except Exception:
        return ""
    digest = hashlib.sha256(der).hexdigest().upper()
    # Group for readability: "ABCD EF01 2345 ..."
    return " ".join(
        digest[i:i + FINGERPRINT_GROUP_LENGTH]
        for i in range(0, len(digest), FINGERPRINT_GROUP_LENGTH)
    )


def _pem_to_der(pem_bytes: bytes) -> bytes:
    """PEM -> DER via openssl (already a hard dependency for generation),
    falling back to the stdlib's own converter if the CLI is unavailable."""
    try:
        proc = subprocess.run(
            ["openssl", "x509", "-outform", "der"],
            input=pem_bytes,
            capture_output=True,
            timeout=10,
        )
        if proc.returncode == 0 and proc.stdout:
            return proc.stdout
    except (OSError, subprocess.SubprocessError):
        pass
    return ssl.PEM_cert_to_DER_cert(pem_bytes.decode("utf-8", "replace"))


def ensure(settings: Optional[Settings] = None) -> str:
    """Make sure a certificate exists; return its SHA-256 fingerprint.

    Generates one if none exists yet. The return value is the grouped hex
    fingerprint for display.
    """
    if not cert_path(settings).exists():
        generate(settings)
    fp = fingerprint(settings)
    if not fp:
        raise CompanionCertError("Certificate exists but fingerprint is empty")
    return fp