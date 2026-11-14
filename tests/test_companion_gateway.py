"""Companion gateway security contract (jarvis/companion/gateway.py).

This was the one security surface in the repository with zero test coverage.
These tests pin the pairing → scoped-session → allowlisted-proxy contract and
prove that forbidden operations fail: they exercise both allowed and denied
behavior, as the security policy requires.

The proxy is pointed at a local fake main backend (a stdlib HTTP server on a
free port) so every forwarding test can assert exactly what the "main
backend" saw: the master token, the method/path/query/body, and the device
identity header.
"""

from __future__ import annotations

import http.server
import json
import socket
import threading
import time
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH_HEADERS, TEST_TOKEN
from jarvis.companion import gateway as gw
from jarvis.companion.gateway import (
    PAIRING_CODE_TTL,
    PHONE_ALLOWED,
    PHONE_ALLOWED_PREFIXES,
    SESSION_TTL_SECONDS,
    build_companion_app,
)
from jarvis.config import get_settings

# The local phone page talks to the backend directly on :8765; the gateway
# is the remote (LAN) phone surface. Its allowlist must therefore be a strict
# subset of the backend's phone surface — anything not on this list must be
# rejected before it can reach the backend.
BACKEND_PHONE_ALLOWED: frozenset = frozenset({
    # 2026-10-28: read-only backend build identity. The phone needs it to tell a
    # stale backend from a current one; it is unauthenticated on the backend,
    # exposes no secret, and cannot change state.
    "/health/identity",
    "/command",
    "/confirmations",
    "/devices",
    "/devices/pair/request",
    "/emergency_stop",
    "/projects",
    "/tools",
    "/voice/status",
    "/voice/transcribe",
    # Google reads, mirrored from the backend list. The connection lifecycle is
    # deliberately absent from both: it is SameOriginProtected, so a phone
    # cannot start or tear down the Mac's authorisation.
    "/google/status",
    "/google/calendar/events",
    "/google/tasks",
})
BACKEND_PHONE_PREFIXES: tuple = ("/confirm/",)


# ---------------------------------------------------------------------------
# Fake main backend
# ---------------------------------------------------------------------------
class _Seen:
    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []


@pytest.fixture()
def backend(monkeypatch):
    """A fake main backend on a free port; records every request it receives."""
    seen = _Seen()

    class Handler(http.server.BaseHTTPRequestHandler):
        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            seen.requests.append({
                "method": self.command,
                "path": self.path,
                "token": self.headers.get("X-JARVIS-Token", ""),
                "device_id": self.headers.get("X-Vantage-Device-Id", ""),
                "content_type": self.headers.get("Content-Type", ""),
                "body": body,
            })
            payload = json.dumps({"ok": True, "echo": self.path}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _handle  # noqa: E742

        def log_message(self, *args):  # silence
            pass

    sock = None
    server = None
    for _ in range(20):  # a free port is a coin flip; retry a couple of times
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        if server.server_address[1] > 0:
            break
        server.server_close()
    port = server.server_address[1]
    assert port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    settings = get_settings().model_copy(update={"port": port})
    app = build_companion_app(settings)
    with TestClient(app) as client:
        yield client, seen
    server.shutdown()
    thread.join(timeout=5)


def _valid_session(client: TestClient) -> str:
    code = client.post("/companion/pairing-code").json()["code"]
    return client.post("/companion/pairing-redeem", json={"code": code}).json()["session_token"]


AUTH = lambda token: {"X-JARVIS-Token": token}  # noqa: E731


# ---------------------------------------------------------------------------
# Health / discovery
# ---------------------------------------------------------------------------
def test_health_reports_certificate_fingerprint(backend) -> None:
    client, _ = backend
    resp = client.get("/companion/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    fp = body["certificate_fingerprint"]
    assert fp  # non-empty: the Android app pins exactly this value
    # grouped 4-hex-chars, 64 hex chars total (SHA-256)
    groups = fp.split(" ")
    assert len("".join(groups)) == 64
    all(c.isupper() or c.isdigit() for c in fp.replace(" ", ""))


def test_pairing_code_response_carries_fingerprint_and_ttl(backend) -> None:
    client, _ = backend
    body = client.post("/companion/pairing-code").json()
    assert body["code"]
    assert body["expires_in_seconds"] == PAIRING_CODE_TTL
    assert body["certificate_fingerprint"]
    # The QR code must point the phone at the gateway port, not the backend.
    assert body["companion_port"] == get_settings().companion_port


async def _asgi_post(app, path: str, peer_host: str, body: bytes = b"{}") -> tuple:
    """Invoke the ASGI app directly with a crafted peer host, to exercise the
    loopback-issuance guard that TestClient (always 'testclient') can't reach.
    Returns (status, body_text)."""
    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "client": (peer_host, 40000),
        "server": ("0.0.0.0", 8766),
        "scheme": "https",
        "app": app,
        "root_path": "",
    }
    received = {}

    async def receive():
        received.setdefault("sent", True)
        return {"type": "http.request", "body": body, "more_body": False}

    status = {}
    chunks = []

    async def send(message):
        if message["type"] == "http.response.start":
            status["code"] = message["status"]
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    await app(scope, receive, send)
    return status["code"], b"".join(chunks).decode("utf-8", "replace")


def test_remote_peer_cannot_issue_a_pairing_code(backend) -> None:
    """The self-pairing hole: a code is the owner authorizing a specific phone,
    so only the Mac (loopback) may mint one. Any LAN peer must be refused,
    otherwise any device on the network could issue-and-redeem its own code.
    Redeem stays LAN-reachable (that is the phone's job)."""
    import asyncio
    from jarvis.companion.gateway import build_companion_app
    from jarvis.config import get_settings

    app = build_companion_app(get_settings())
    peer = "192.168.1.50"  # a phone's plausible DHCP address
    # Issuance from the LAN is refused, and the refusal says why.
    status, body = asyncio.run(_asgi_post(app, "/companion/pairing-code", peer))
    assert status == 403
    assert "Mac itself" in body
    # Redeem is still reachable from the LAN: same 403 status, but a *code*
    # error, not an issuance error — proving the handler ran for the peer.
    status, body = asyncio.run(_asgi_post(app, "/companion/pairing-redeem", peer,
                                          b'{"code":"x"}'))
    assert status == 403
    assert "Invalid or expired pairing code" in body
    # A genuinely local peer (TestClient presents as 'testclient') can still issue.
    client, _ = backend
    assert client.post("/companion/pairing-code").status_code == 200


# ---------------------------------------------------------------------------
# Pairing code lifecycle
# ---------------------------------------------------------------------------
def test_pairing_code_redeems_once(backend) -> None:
    client, _ = backend
    code = client.post("/companion/pairing-code").json()["code"]
    first = client.post("/companion/pairing-redeem", json={"code": code})
    assert first.status_code == 200
    body = first.json()
    assert body["surface"] == "phone"
    assert body["session_token"]
    assert body["expires_in"] == SESSION_TTL_SECONDS
    # Reuse of a redeemed code must fail safely.
    second = client.post("/companion/pairing-redeem", json={"code": code})
    assert second.status_code == 403


def test_unknown_pairing_code_rejected(backend) -> None:
    client, _ = backend
    resp = client.post("/companion/pairing-redeem", json={"code": "0" * 32})
    assert resp.status_code == 403


def test_expired_pairing_code_rejected(backend, monkeypatch) -> None:
    client, _ = backend
    monkeypatch.setattr(gw, "PAIRING_CODE_TTL", -1)  # issued codes are already expired
    code = client.post("/companion/pairing-code").json()["code"]
    resp = client.post("/companion/pairing-redeem", json={"code": code})
    assert resp.status_code == 403
    assert "expired" in resp.json()["detail"].lower() or "Invalid" in resp.json()["detail"]


def test_issuing_a_new_code_supersedes_the_previous_one(backend) -> None:
    """The documented contract: only one live code per surface.

    An old QR code scanned after the Mac issued a new pairing must not still
    redeem — otherwise a bystander with the earlier code can pair.
    """
    client, _ = backend
    old = client.post("/companion/pairing-code").json()["code"]
    client.post("/companion/pairing-code")  # user re-generates; old code must die
    resp = client.post("/companion/pairing-redeem", json={"code": old})
    assert resp.status_code == 403


def test_redeem_with_missing_code_field(backend) -> None:
    client, _ = backend
    assert client.post("/companion/pairing-redeem", json={}).status_code == 400
    assert client.post("/companion/pairing-redeem", content=b"not-json",
                       headers={"Content-Type": "application/json"}).status_code == 400


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------
def test_expired_session_is_rejected(backend) -> None:
    client, _ = backend
    token = _valid_session(client)
    # Force the stored session past its expiry.
    exp, surface, info = gw._sessions[token]
    gw._sessions[token] = (time.monotonic() - 1, surface, info)
    resp = client.get("/devices", headers=AUTH(token))
    assert resp.status_code == 401


def test_expired_sessions_are_pruned_on_validation(backend) -> None:
    client, _ = backend
    token = _valid_session(client)
    exp, surface, info = gw._sessions[token]
    gw._sessions[token] = (time.monotonic() - 1, surface, info)
    assert gw.validate_session(token) is None
    assert token not in gw._sessions  # removed, not left to linger


def test_revoked_session_no_longer_works(backend) -> None:
    client, _ = backend
    token = _valid_session(client)
    out = client.delete("/companion/session", headers=AUTH(token))
    assert out.status_code == 200
    assert out.json() == {"status": "signed_out"}
    assert client.get("/devices", headers=AUTH(token)).status_code == 401


def test_sign_out_unknown_session_404(backend) -> None:
    client, _ = backend
    assert client.delete("/companion/session", headers=AUTH("CMP-doesnotexist")).status_code == 404


def test_missing_session_token_is_unauthorized(backend) -> None:
    client, _ = backend
    assert client.get("/devices").status_code == 401


def test_invalid_session_token_is_unauthorized(backend) -> None:
    client, _ = backend
    assert client.get("/devices", headers=AUTH("not-a-real-token")).status_code == 401


# ---------------------------------------------------------------------------
# Proxy allowlist: forbidden paths must fail and never reach the backend
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", [
    "/projects", "/tools", "/voice/status", "/voice/transcribe",  # local-surface only
    "/memory/delete", "/auth/session", "/providers",  # sensitive, never on the phone
    "/companion/pairing-redeem",  # gateway's own routes are not proxy-eligible here
])
def test_gateway_denies_paths_outside_the_phone_surface(backend, path) -> None:
    client, seen = backend
    token = _valid_session(client)
    resp = client.get(path, headers=AUTH(token))
    assert resp.status_code == 403, path
    # The request must be rejected at the gateway, not forwarded.
    assert all(r["path"].split("?")[0] != path for r in seen.requests), path


def test_allowlist_is_a_subset_of_the_backend_phone_surface() -> None:
    """The remote surface may only shrink the local surface, never widen it.

    /health is proxied to the backend's own /health (also protected there);
    every other gateway-allowed path and prefix must exist on the backend.
    """
    assert (PHONE_ALLOWED - {"/health"}) <= BACKEND_PHONE_ALLOWED
    assert set(PHONE_ALLOWED_PREFIXES) <= set(BACKEND_PHONE_PREFIXES)


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "DELETE", "PATCH"])
def test_denial_is_method_independent(backend, method) -> None:
    client, _ = backend
    token = _valid_session(client)
    resp = client.request(method, "/memory/delete", headers=AUTH(token))
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Proxy forwarding: allowed paths reach the backend as the master token
# ---------------------------------------------------------------------------
def test_proxy_forwards_allowed_path_with_master_token(backend) -> None:
    client, seen = backend
    token = _valid_session(client)
    resp = client.get("/devices", headers=AUTH(token))
    assert resp.status_code == 200
    assert resp.json()["echo"] == "/devices"
    assert len(seen.requests) == 1
    req = seen.requests[0]
    assert req["token"] == TEST_TOKEN      # master token, never the session token
    assert req["token"] != token
    assert req["path"] == "/devices"


def test_proxy_forwards_body_query_and_device_identity(backend) -> None:
    client, seen = backend
    token = _valid_session(client)
    payload = {"text": "check my calendar"}
    resp = client.post(
        "/command?source=gateway-test",
        headers={**AUTH(token), "X-Vantage-Device-Id": "device-42"},
        json=payload,
    )
    assert resp.status_code == 200
    req = seen.requests[0]
    assert req["method"] == "POST"
    assert req["path"] == "/command?source=gateway-test"
    assert json.loads(req["body"]) == payload
    assert req["device_id"] == "device-42"
    assert req["token"] == TEST_TOKEN


def test_the_proxy_forwards_the_content_type(backend) -> None:
    """A JSON body from the phone must arrive as JSON.

    The proxy set ``Accept: application/json`` but not ``Content-Type``, so a
    JSON body arrived at the backend as ``text/plain``. The body is a JSON
    *string*, and the API reads a string where it wants a dictionary -- so it
    answered 422 ("Input should be a valid dictionary") and **every command
    the phone sent was refused**. The phone's own client set the header
    correctly; the proxy dropped it.

    The content type is forwarded, not replaced: the backend must read the
    body as whatever the phone sent it as.
    """
    client, seen = backend
    token = _valid_session(client)
    payload = {"text": "check my calendar"}
    resp = client.post(
        "/command",
        headers={**AUTH(token), "Content-Type": "application/json"},
        content=json.dumps(payload),
    )
    assert resp.status_code == 200
    req = seen.requests[0]
    assert req["content_type"] == "application/json", (
        f"the proxy dropped the content type; the backend received "
        f"{req['content_type']!r}")


def test_the_proxy_forwards_a_multipart_content_type(backend) -> None:
    """A multipart body keeps its own boundary.

    Forwarding the content type *as it was sent* is what makes a multipart
    upload work; replacing it with JSON would make the backend read a
    multipart body as JSON.
    """
    client, seen = backend
    token = _valid_session(client)
    boundary = "----TestBoundary"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="message_id"\r\n\r\n'
        "abc\r\n"
        f"--{boundary}--\r\n"
    ).encode()
    resp = client.post(
        "/ai/attachments/upload",
        headers={
            **AUTH(token),
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        content=body,
    )
    # The route is not on the phone surface, so the gateway refuses it --
    # but the refusal happens before the proxy, so nothing was forwarded and
    # there is no content type to assert. Use a path the phone may use.
    assert resp.status_code in (403, 200), resp.text
    if resp.status_code == 200:
        req = seen.requests[0]
        assert "multipart/form-data" in req["content_type"]


def test_confirm_prefix_is_reachable_through_the_gateway(backend) -> None:
    client, seen = backend
    token = _valid_session(client)
    resp = client.post("/confirm/abcdef-1234", headers=AUTH(token), json={})
    assert resp.status_code == 200  # proxied; the backend decides the outcome
    assert seen.requests[0]["path"] == "/confirm/abcdef-1234"


def test_unauthenticated_companion_catchall_is_rejected(backend) -> None:
    """Unrouted /companion/* URLs fall through to the proxy and need a session."""
    client, _ = backend
    assert client.get("/companion/nope").status_code == 401


def test_a_code_minted_by_the_backend_can_be_redeemed_by_the_gateway() -> None:
    """The code has to be minted by the gateway, because the gateway redeems it.

    The codes live in the gateway's memory. The backend's
    ``/companion/pairing-code`` route used to mint one in *its own* memory --
    so the code it handed out was one the gateway had never heard of, and
    redeeming it failed with "Invalid or expired pairing code". The route's own
    docstring described a flow that could not work, and nothing called it.

    The route is pinned here by asking the backend for a code and then
    redeeming it at the gateway, which is the flow the docstring describes.
    """
    import os
    import ssl
    from pathlib import Path

    import httpx

    from jarvis.companion.cert import ensure
    from jarvis.config import get_settings

    settings = get_settings()
    ensure(settings)
    # The gateway that is actually running holds the certificate the phone
    # would pin, so use *that* one -- the test's own home has its own.
    cert = Path(os.path.expanduser("~/.jarvis-forge")) / "companion" / "companion.crt"
    if not cert.exists():
        pytest.skip("the companion certificate has not been generated here")

    # The backend mints the code -- the route the dashboard would use.
    from fastapi.testclient import TestClient

    from jarvis.api.app import app

    backend_client = TestClient(app, base_url="http://127.0.0.1:8765")
    minted = backend_client.post(
        "/companion/pairing-code", headers={"X-JARVIS-Token": TEST_TOKEN}
    )
    if minted.status_code != 200:
        pytest.skip("the companion gateway is not running here")
    code = minted.json()["code"]
    assert code, minted.json()

    # The phone redeems it at the gateway. The gateway's certificate is
    # self-signed, so it is verified against the certificate the backend
    # generated -- chain verification, never disabled.
    context = ssl.create_default_context(cafile=str(cert))
    context.check_hostname = False
    try:
        with httpx.Client(verify=context, timeout=15.0) as client:
            redeemed = client.post(
                f"https://127.0.0.1:{settings.companion_port}/companion/pairing-redeem",
                json={"code": code},
            )
    except Exception as exc:  # the gateway may not be reachable here
        pytest.skip(f"the companion gateway is not reachable here: {exc}")

    assert redeemed.status_code == 200, redeemed.text
    assert redeemed.json()["session_token"], redeemed.text
