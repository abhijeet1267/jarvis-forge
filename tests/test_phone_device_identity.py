"""A phone that redeems a pairing code is a device the backend knows.

The companion app calls itself `phone_<uuid8>` — an id it generates the first
time it pairs, and keeps. That id travels in the `X-Vantage-Device-Id` header
on every request, and the backend now reads it (the gateway replaces the
session token with the master token, so the header is the only place the
backend can still learn which handset a request came from).

But the id named a device the registry had never heard of. Nothing ever
registered it:

* the gateway's redeem route took only the pairing code, so the phone's
  identity was established and then discarded;
* the app generated its id *after* the redeem, so there was nothing to send
  even if the route had asked;
* the app's other pairing path (`/devices/pair/request`) is a separate,
  user-approved flow that a phone does not go through at setup.

So a phone's id was real, stable, forwarded, and read — and referred to
nothing. The permission gate has nothing to aim at for a device it has never
heard of, and the audit chain could not name the handset.

**Redeeming the pairing code is where the phone's identity is established**, so
that is where the backend should learn it. The device is registered as online
(it has just proved it can reach the gateway over TLS) and as paired (a
redeemed pairing code is the owner's act of trusting it).

These tests pin the registration and the client's half of the chain.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# A gateway, on a free port, with a fake main backend behind it
# ---------------------------------------------------------------------------
@pytest.fixture()
def backend(monkeypatch):
    """The companion gateway, with a fake main backend behind it.

    The same shape as the gateway suite's fixture: a real gateway app, a fake
    backend that records what it receives, and a real device registry in the
    test's own JARVIS_HOME.
    """
    import http.server
    import json
    import threading
    from typing import Any, Dict, List

    from fastapi.testclient import TestClient

    from jarvis.companion.gateway import build_companion_app
    from jarvis.config import get_settings

    class Handler(http.server.BaseHTTPRequestHandler):
        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            payload = json.dumps({"ok": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = do_PUT = do_DELETE = _handle  # noqa: E742

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    settings = get_settings().model_copy(update={"port": port})
    app = build_companion_app(settings)
    with TestClient(app) as client:
        yield client
    server.shutdown()
    thread.join(timeout=5)


# ---------------------------------------------------------------------------
# The gateway registers the phone
# ---------------------------------------------------------------------------
def test_redeeming_a_code_registers_the_device(backend, monkeypatch):
    """The phone is in the registry afterwards, and it is online."""
    from jarvis.registry.devices import get_device_registry

    client = backend
    registry = get_device_registry()
    device_id = "phone_testreg01"
    assert registry.get(device_id) is None, "the device must not exist beforehand"

    code = client.post("/companion/pairing-code").json()["code"]
    response = client.post(
        "/companion/pairing-redeem",
        json={"code": code, "device_id": device_id, "device_name": "Test Pixel"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["session_token"]

    device = registry.get(device_id)
    assert device is not None, "the phone was not registered by the redeem"
    assert device.online is True, (
        "a phone that has just proved it can reach the gateway is online")
    assert device.platform == "android"
    assert device.metadata.get("device_name") == "Test Pixel"


def test_a_redeem_without_a_device_id_still_works(backend):
    """The registration is additive; a client that names no device still pairs."""
    client = backend
    code = client.post("/companion/pairing-code").json()["code"]
    response = client.post("/companion/pairing-redeem", json={"code": code})
    assert response.status_code == 200, response.text
    assert response.json()["session_token"]


def test_the_gateway_asks_the_registry_for_the_device():
    """The registration goes through the registry, not a private table."""
    source = (REPO / "jarvis" / "companion" / "gateway.py").read_text(encoding="utf-8")
    assert "def _register_device" in source, (
        "the gateway has no device-registration step")
    assert "get_device_registry" in source, (
        "the registration does not go through the device registry")
    assert "registry.register(" in source, (
        "the registration does not register the device")


# ---------------------------------------------------------------------------
# The client names itself
# ---------------------------------------------------------------------------
def test_the_redeem_request_can_name_the_device():
    """The app can tell the backend which phone it is."""
    source = (REPO / "android" / "app" / "src" / "main" / "kotlin"
              / "com" / "vantage" / "companion" / "data" / "api"
              / "ApiModels.kt").read_text(encoding="utf-8")
    block = re.search(
        r"data class PairingRedeemRequest\((.*?)\n\)", source, re.S)
    assert block, "the redeem request is gone"
    body = block.group(1)
    assert "device_id" in body, "the redeem request cannot name the device"
    assert "device_name" in body, "the redeem request cannot name the device"


def test_the_client_sends_its_device_id_when_redeeming():
    source = (REPO / "android" / "app" / "src" / "main" / "kotlin"
              / "com" / "vantage" / "companion" / "data" / "api"
              / "VantageApiClient.kt").read_text(encoding="utf-8")
    redeem = re.search(
        r"suspend fun redeemPairingCode\(.*?\n    \}", source, re.S)
    assert redeem, "the redeem call is gone"
    body = redeem.group(0)
    assert "deviceId" in body, "the redeem call does not pass the device id"


def test_the_client_settles_its_identity_before_redeeming():
    """The id has to exist by the time the redeem happens.

    It used to be generated *after*, so there was nothing to send — the backend
    registered the phone and never learned its name.
    """
    source = (REPO / "android" / "app" / "src" / "main" / "kotlin"
              / "com" / "vantage" / "companion" / "ui" / "setup"
              / "SetupViewModel.kt").read_text(encoding="utf-8")
    generate_at = source.find('saveDeviceIdentity(deviceId, deviceName)')
    redeem_at = source.find("api.redeemPairingCode(")
    assert generate_at != -1, "the device identity is never settled"
    assert redeem_at != -1, "the redeem call is gone"
    assert generate_at < redeem_at, (
        "the device identity is settled AFTER the redeem, so the backend "
        "registers the phone without learning which phone it is"
    )


def test_the_device_id_is_a_stable_identity():
    """It is generated once and kept, not made per request.

    The id is what the audit chain names, so a per-request id would make every
    request look like a different handset.
    """
    source = (REPO / "android" / "app" / "src" / "main" / "kotlin"
              / "com" / "vantage" / "companion" / "ui" / "setup"
              / "SetupViewModel.kt").read_text(encoding="utf-8")
    # It is generated behind a "is it blank?" check, and stored.
    assert "if (store.deviceId.value.isBlank())" in source, (
        "the device id is not generated once -- it is regenerated")
    assert 'phone_${UUID.randomUUID().toString().take(8)}' in source, (
        "the device id is not the app's own stable id")
