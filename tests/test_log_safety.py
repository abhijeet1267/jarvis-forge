"""Credentials must not reach the HTTP access log.

Measured before this module existed, with uvicorn's defaults on this machine:

    INFO: 127.0.0.1:63705 - "GET /google/callback?code=4/0Ab...&state=... HTTP/1.1" 200 OK

A Google OAuth authorization code appears verbatim. It is short-lived but it is
a credential, and a log is precisely where the project's rules say secrets must
never be. These tests hold the scrubber to that.

The behavioural test at the end runs a REAL uvicorn server and greps its log,
because a filter tested only in memory can pass while the server still writes
the raw line -- which is how the leak was found in the first place.
"""

from __future__ import annotations

import logging
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from jarvis.log_safety import MASK, ScrubCredentialsFilter, install, scrub_target
from jarvis.security.privacy import redact

REPO = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# The scrubber
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("param", ["code", "state", "access_token", "refresh_token",
                                   "id_token", "token", "client_secret", "password",
                                   "api_key", "secret", "session_token", "bootstrap"])
def test_credential_parameters_are_masked(param):
    scrubbed = scrub_target(f"/google/callback?{param}=SECRETVALUE&keep=visible")
    assert "SECRETVALUE" not in scrubbed
    assert f"{param}={MASK}" in scrubbed
    # The non-secret parameter survives, so the log still says what happened.
    assert "keep=visible" in scrubbed


def test_ordinary_parameters_are_left_alone():
    target = "/dashboard?q=hello&page=2&barcode=12345"
    assert scrub_target(target) == target


def test_parameter_matching_is_by_whole_name():
    """`barcode` contains `code` but is not the `code` parameter."""
    assert "12345" in scrub_target("/x?barcode=12345")


def test_a_target_without_credentials_is_unchanged():
    assert scrub_target("POST /command HTTP/1.1") == "POST /command HTTP/1.1"


def test_empty_input_is_safe():
    assert scrub_target("") == ""


# ---------------------------------------------------------------------------
# The filter, as uvicorn actually builds records
# ---------------------------------------------------------------------------


def _access_record(args):
    return logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1,
        '%s - "%s %s HTTP/%s" %d', args, None)


def test_filter_scrubs_the_argument_tuple_uvicorn_uses():
    record = _access_record(
        ("127.0.0.1:1234", "GET", "/google/callback?code=SECRET&state=S", "1.1", 200))
    ScrubCredentialsFilter().filter(record)
    message = record.getMessage()
    assert "SECRET" not in message
    assert "code=[redacted]" in message
    assert "200" in message, "the status code must survive"


def test_filter_scrubs_a_preformatted_message():
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1,
                               "GET /x?token=SECRET99", None, None)
    ScrubCredentialsFilter().filter(record)
    assert "SECRET99" not in record.getMessage()


def test_filter_never_raises_on_odd_input():
    """A logging filter must not break the request it is describing."""
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1,
                               "%s", (object(),), None)
    assert ScrubCredentialsFilter().filter(record) is True


def test_install_is_idempotent():
    install(("test.uvicorn.access",))
    install(("test.uvicorn.access",))
    logger = logging.getLogger("test.uvicorn.access")
    assert sum(isinstance(f, ScrubCredentialsFilter) for f in logger.filters) == 1


# ---------------------------------------------------------------------------
# End to end: a real server, a real log file
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.parametrize("with_filter", [True, False])
def test_a_real_server_log_does_or_does_not_contain_the_code(tmp_path, with_filter):
    """Proves the filter works in the process that actually writes the log.

    The ``with_filter=False`` case is the control: it shows the leak is real
    and that this test would notice if the filter stopped being installed.
    """
    port = _free_port()
    app_file = tmp_path / "app.py"
    app_file.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(REPO)!r})\n"
        "from fastapi import FastAPI\n"
        + ("from jarvis.log_safety import install\ninstall()\n" if with_filter else "")
        + "app = FastAPI()\n"
        "@app.get('/google/callback')\n"
        "async def cb(code: str = '', state: str = ''):\n"
        "    return {'ok': True}\n",
        encoding="utf-8")
    log_file = tmp_path / "out.log"
    with log_file.open("w") as handle:
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app:app",
             "--port", str(port), "--log-level", "info"],
            cwd=str(tmp_path), stdout=handle, stderr=subprocess.STDOUT)
    try:
        # Wait for the server, rather than sleeping a guessed amount.
        deadline = time.time() + 20
        ready = False
        while time.time() < deadline:
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/google/callback?code=WARMUP&state=s", timeout=1)
                ready = True
                break
            except Exception:
                time.sleep(0.3)
        assert ready, "the test server did not start"
        urllib.request.urlopen(
            f"http://127.0.0.1:{port}/google/callback"
            f"?code=SECRET_AUTH_CODE_XYZ&state=statevalue", timeout=5).read()
        time.sleep(0.6)                       # let the access log flush
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    text = log_file.read_text(encoding="utf-8", errors="replace")
    if with_filter:
        assert "SECRET_AUTH_CODE_XYZ" not in text, (
            "the authorization code reached the access log:\n"
            + "\n".join(line for line in text.splitlines() if "callback" in line))
        assert "code=[redacted]" in text
    else:
        # The control. If this starts failing, uvicorn changed how it logs and
        # the filter's reason for existing should be re-measured.
        assert "SECRET_AUTH_CODE_XYZ" in text

# ---------------------------------------------------------------------------
# The relay forwards a credential on every call
#
# A transport error's text is not under this code's control: an httpx/h11
# exception can carry the request URL or header. The relay logged `str(exc)`
# verbatim and returned it to the client too.
# ---------------------------------------------------------------------------


def test_the_relay_redacts_a_credential_bearing_transport_error():
    import inspect

    import jarvis.relay as relay

    source = inspect.getsource(relay)
    # The forward-failure handler must redact before logging or returning.
    handler = source[source.index("relay forward failed") - 400:
                     source.index("relay forward failed") + 400]
    assert "redact(" in handler, (
        "the relay logs a transport error without redacting it")


def test_redaction_covers_the_shapes_a_transport_error_carries():
    cases = {
        "https://relay.example/x?token=SECRET123&other=1": "SECRET123",
        "header x-jarvis-token: SECRET123 rejected": "SECRET123",
        "Authorization: Bearer SECRET123": "SECRET123",
    }
    for text, secret in cases.items():
        assert secret not in scrub_target("") and secret not in redact(text), (
            f"{secret!r} survived redaction in {text!r}")


# ---------------------------------------------------------------------------
# Redaction must not mangle ordinary prose
#
# The key=value pattern is applied to every log line and user-facing message.
# It used to treat the next English word as a credential's value:
# "Your session token expired" -> "Your session token [redacted]", losing the
# verb, and "Token budget remaining: 4096" -> "Token [redacted] remaining".
# Measured on the committed HEAD pattern, so this predates the 2026-10-27 edits.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "Your session token expired. Sign in again.",
    "Token budget remaining: 4096",
    "Bearer authentication is required for this endpoint.",
    "Set the password in Settings.",
    "The token was rejected.",
    "Authorization is required.",
    "code_challenge_method=S256",
])
def test_ordinary_prose_is_left_alone(text):
    assert redact(text) == text, f"redaction mangled: {redact(text)!r}"


@pytest.mark.parametrize("text,secret", [
    ("Authorization: Bearer SECRET123", "SECRET123"),
    ("Bearer SECRET123", "SECRET123"),
    ("token=SECRET123", "SECRET123"),
    ("x-jarvis-token: SECRET123", "SECRET123"),
    ('password="hunter2"', "hunter2"),
    ("client_secret=abcdefgh12345678", "abcdefgh12345678"),
    ("Bearer ya29.a0AfB_byC123", "byC123"),
    ("refresh_token=1//0gAbCdEfGhIjKlMn", "0gAbCdEf"),
    ("session_token=abc123def456", "abc123def456"),
])
def test_real_credential_shapes_are_masked(text, secret):
    out = redact(text)
    assert secret not in out, f"{secret!r} survived: {out!r}"
    assert "[redacted]" in out
