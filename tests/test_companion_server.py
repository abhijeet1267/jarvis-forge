"""Companion gateway lifecycle (jarvis/companion/server.py).

R1.3: the gateway must be owned by the backend — started with it (LAN mode),
stopped with it, TLS-verified on the way up, and honest about failures.
These tests spawn a real gateway subprocess on a random free port against the
isolated test JARVIS_HOME (conftest), so the certificate is generated in
throwaway state, never the developer's.
"""

from __future__ import annotations

import http.server
import socket
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH_HEADERS
from jarvis.companion.server import CompanionServer, _verified_context
from jarvis.config import get_settings


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_lan_off_does_not_start_the_gateway():
    settings = get_settings().model_copy(update={"lan_mode": False, "companion_port": _free_port()})
    gw = CompanionServer(settings=settings)
    status = gw.start()
    assert status.running is False
    assert "LAN mode" in status.detail
    assert gw._proc is None  # nothing to leak


def test_gateway_starts_stops_and_is_tls_verified():
    settings = get_settings().model_copy(update={"lan_mode": True, "companion_port": _free_port()})
    gw = CompanionServer(settings=settings)
    try:
        status = gw.start()
        assert status.running is True, status.detail
        assert gw.status().running is True

        # The readiness probe already verified the TLS chain against the
        # exact cert we generated; confirm a direct, chain-verified request
        # works too — and (below) that a mismatched CA is refused.
        cert = settings.home / "companion" / "companion.crt"
        client = httpx.Client(verify=_verified_context(cert), timeout=5.0)
        r = client.get(f"https://127.0.0.1:{settings.companion_port}/companion/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["certificate_fingerprint"]

        # start() is idempotent: a second call must not spawn a second child.
        again = gw.start()
        assert again.running is True
        first_pid = gw._proc.pid
        time.sleep(0.2)
        assert gw._proc.pid == first_pid
    finally:
        gw.stop()
    assert gw._proc is None
    assert gw.status().running is False


def test_cert_and_fingerprint_are_stable_across_restarts():
    settings = get_settings().model_copy(update={"lan_mode": True, "companion_port": _free_port()})
    gw = CompanionServer(settings=settings)
    cert = settings.home / "companion" / "companion.crt"
    fp1 = None
    for _ in range(2):
        try:
            status = gw.start()
            assert status.running is True, status.detail
            client = httpx.Client(verify=_verified_context(cert), timeout=5.0)
            r = client.get(f"https://127.0.0.1:{settings.companion_port}/companion/health")
            fp = r.json()["certificate_fingerprint"]
            assert fp1 is None or fp == fp1, "fingerprint changed across restart"
            fp1 = fp
        finally:
            gw.stop()
    # The file itself is untouched by a second start: ensure() keeps the first.
    assert cert.exists()


def test_fingerprint_is_the_standard_x509_sha256():
    """The served fingerprint must be what a TLS client can independently
    compute: SHA-256 over the certificate in DER form (what
    ``openssl x509 -fingerprint -sha256`` and browser trust dialogs show).

    The old implementation hashed the PEM file bytes instead — a value a
    phone, which only ever sees the DER certificate during the handshake,
    can never reproduce. Pinning is meaningless if the pinned value is
    unverifiable.
    """
    import hashlib
    import ssl
    from jarvis.companion import cert as certmod

    settings = get_settings()
    certmod.ensure(settings)
    crt = certmod.cert_path(settings)

    # Independent reference computation, the way a client would do it.
    der = ssl.PEM_cert_to_DER_cert(crt.read_text())
    expected = hashlib.sha256(der).hexdigest().upper()

    fp = certmod.fingerprint(settings)
    assert fp.replace(" ", "") == expected
    # display format: grouped 4-hex chars
    assert fp == " ".join(expected[i:i + 4] for i in range(0, 64, 4))


def test_cert_verification_catches_a_wrong_ca():
    """verify= must actually verify. A client pinned to a *different* cert
    (one freshly generated in a scratch home) must be refused by the gateway."""
    import tempfile
    from pathlib import Path
    from jarvis.companion.cert import ensure
    tmp = Path(tempfile.mkdtemp(prefix="companion-scratch-"))
    scratch = get_settings().model_copy(update={"home": tmp})
    ensure(scratch)
    scratch_cert = scratch.home / "companion" / "companion.crt"
    main_settings = get_settings().model_copy(update={"lan_mode": True, "companion_port": _free_port()})
    main_cert = main_settings.home / "companion" / "companion.crt"
    if not main_cert.exists():  # make the test order-independent
        ensure(main_settings)
    assert scratch_cert.read_bytes() != main_cert.read_bytes()

    gw = CompanionServer(settings=main_settings)
    try:
        status = gw.start()
        assert status.running is True, status.detail
        # A client whose trusted CA is a *different* self-signed cert must be
        # refused at the TLS layer: the chain cannot build, so the handshake
        # fails before any application data. (httpx reports this as a
        # connection/protocol error — the exact type is backend-dependent.)
        client = httpx.Client(verify=_verified_context(scratch_cert), timeout=5.0)
        with pytest.raises(httpx.HTTPError):
            client.get(f"https://127.0.0.1:{main_settings.companion_port}/companion/health")
    finally:
        gw.stop()


def test_start_failure_is_reported_not_silent():
    """If the port is already taken, the gateway child dies on bind; start()
    must report that instead of claiming success or hanging 15s into silence."""
    port = _free_port()
    settings = get_settings().model_copy(update={"lan_mode": True, "companion_port": port})
    blocker = None
    for _ in range(20):  # a free port is a coin flip; retry binding
        try:
            blocker = http.server.ThreadingHTTPServer(("127.0.0.1", port),
                                                      http.server.SimpleHTTPRequestHandler)
            break
        except OSError:
            port = _free_port()
            settings = get_settings().model_copy(update={"lan_mode": True, "companion_port": port})
    assert blocker is not None
    import threading
    t = threading.Thread(target=blocker.serve_forever, daemon=True)
    t.start()
    try:
        gw = CompanionServer(settings=settings)
        status = gw.start()
        assert status.running is False
        assert status.detail  # an operator can read why
        assert "exited early" in status.detail or "healthy response" in status.detail
    finally:
        blocker.shutdown()
        t.join(timeout=5)


def test_main_backend_exposes_companion_status():
    """The macOS panel asks the backend it already talks to — with the same
    auth as every other route — for the gateway's real state."""
    from jarvis.api.app import app
    # base_url matches the Host the backend's anti-rebinding check expects
    # (same convention as tests/test_api.py).
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        r = client.get("/companion/status", headers=AUTH_HEADERS)
        assert r.status_code == 200
        body = r.json()
        assert set(body) == {"running", "port", "lan_mode", "detail"}
        # Default test settings have lan_mode off, so the gateway is off and
        # the reason must say so — never a silent `running: false`.
        assert body["running"] is False
        assert body["lan_mode"] is False
        assert "LAN mode" in body["detail"]
        # Unauthenticated, like every protected route.
        assert client.get("/companion/status").status_code in (401, 403)


def test_health_includes_companion_state():
    """The macOS panel polls /health (not a separate endpoint), so the gateway's
    real state must ride along there too — the same shape as /companion/status."""
    from jarvis.api.app import app
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        body = client.get("/health", headers=AUTH_HEADERS).json()
        assert "companion" in body
        c = body["companion"]
        assert set(c) == {"running", "port", "detail"}
        assert c["running"] is False
        assert c["port"] == get_settings().companion_port
        assert "LAN mode" in c["detail"]


def test_the_manager_is_not_replaced_by_an_equal_settings_object():
    """A new-but-equal Settings must not swap out the manager that owns the
    gateway child.

    The lifespan and the API routes each call ``get_companion_server`` with their
    own settings object, and every ``get_settings()`` / ``model_copy()`` returns a
    *fresh* object. Comparing by identity therefore replaced the manager on every
    call, and the manager that had actually spawned the gateway was dropped
    without being stopped — which is how a gateway child outlived its backend and
    kept holding :8766 with a certificate from a since-deleted test home.
    """
    from jarvis.companion import server as server_mod

    original = server_mod._server
    try:
        server_mod._server = None
        first = server_mod.get_companion_server(get_settings())
        # A different object, same meaning — as the lifespan and the API route
        # would each produce.
        equivalent = get_settings().model_copy(update={})
        second = server_mod.get_companion_server(equivalent)
        assert first is second, (
            "the companion manager must be reused across equal settings objects; "
            "replacing it orphans the running gateway child"
        )
    finally:
        server_mod._server = original


def test_a_manager_with_a_running_gateway_is_never_replaced():
    """The manager that owns a live gateway child must not be swapped out.

    Swapping it would leave the child running with nobody able to stop it.
    """
    from jarvis.companion import server as server_mod

    original = server_mod._server
    try:
        server_mod._server = None
        first = server_mod.get_companion_server(get_settings())
        # Pretend it owns a live child.
        first._proc = object()  # type: ignore[assignment]
        different = get_settings().model_copy(update={"companion_port": _free_port()})
        second = server_mod.get_companion_server(different)
        assert second is first, "a manager owning a live gateway must not be replaced"
    finally:
        server_mod._server = original
        if original is not None:
            original._proc = None
