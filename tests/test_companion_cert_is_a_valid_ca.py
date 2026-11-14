"""The companion certificate must be usable as a trust anchor on OpenSSL 3.x.

The gateway verifies its own self-signed certificate as a **CA**
(``server._verified_context`` → ``ssl.SSLContext.load_verify_locations``). A
certificate without ``basicConstraints: CA:TRUE`` is not a valid CA, and OpenSSL
3.x — which Python 3.13+ links against — refuses it at handshake time with
``certificate verify failed: invalid CA certificate``.

This shipped broken. On this machine's Python 3.11 the weaker OpenSSL accepted
the extension-less certificate, so every companion test passed; a portability
check on a fresh Python 3.13 interpreter reproduced the failure immediately
(the gateway logged "no healthy response within 15s" while uvicorn reported it
running on ``0.0.0.0`` — the TLS handshake was failing on the client side).

The fix adds ``basicConstraints`` / ``keyUsage`` / ``subjectAltName`` in
``jarvis/companion/cert.py``. These tests read the generated certificate's own
extensions, so a future edit that drops the flags fails here with the reason.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _openssl_text(cert: Path) -> str:
    proc = subprocess.run(
        ["openssl", "x509", "-in", str(cert), "-noout", "-text"],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


@pytest.fixture()
def generated_cert(tmp_path):
    """Generate a certificate the way the product does, into a scratch home."""
    from jarvis.companion import cert as certmod
    from jarvis.config import get_settings

    settings = get_settings().model_copy(update={"home": tmp_path})
    certmod.generate(settings)
    return certmod.cert_path(settings)


def test_the_certificate_says_it_is_a_ca(generated_cert):
    """Without CA:TRUE, OpenSSL 3.x rejects it as a trust anchor."""
    text = _openssl_text(generated_cert)
    assert "Basic Constraints" in text, "the certificate carries no basicConstraints"
    assert "CA:TRUE" in text, (
        "the companion certificate is not marked CA:TRUE; OpenSSL 3.x (Python "
        "3.13+) will refuse it with 'invalid CA certificate'")


def test_the_certificate_carries_key_usage_and_a_san(generated_cert):
    text = _openssl_text(generated_cert)
    assert "Key Usage" in text, "the certificate carries no keyUsage extension"
    assert "127.0.0.1" in text, (
        "the certificate has no subjectAltName for the address a client dials")


def test_python_accepts_it_as_a_trust_anchor(generated_cert):
    """The exact operation the gateway performs. This is version-independent:

    on OpenSSL 3.x it only succeeds if the CA bit is present.
    """
    import ssl

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_verify_locations(str(generated_cert))  # raises on an invalid CA
    assert True