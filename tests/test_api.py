"""API surface tests, including the security controls."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from jarvis.api.app import app

# Set by tests/conftest.py before any jarvis import.
TEST_TOKEN = os.environ["JARVIS_API_TOKEN"]
AUTH_HEADERS = {"X-JARVIS-Token": TEST_TOKEN}


@pytest.fixture(scope="module")
def client() -> TestClient:
    """An authenticated client -- the normal, happy path.

    ``base_url`` must be a loopback address: the API rejects non-loopback Host
    headers to defeat DNS rebinding, and TestClient's default host
    (``testserver``) is correctly refused.
    """
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    client.headers.update(AUTH_HEADERS)
    return client


@pytest.fixture(scope="module")
def anon_client() -> TestClient:
    """An unauthenticated client, used to prove auth is enforced."""
    return TestClient(app, base_url="http://127.0.0.1:8765")


def test_root_and_health(client: TestClient) -> None:
    root = client.get("/")
    assert root.status_code == 200
    assert root.json()["name"] == "Vantage Core"

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert health.json()["tools"] > 0


def test_devices_endpoint(client: TestClient) -> None:
    devices = client.get("/devices").json()
    ids = {d["device_id"] for d in devices}
    assert {"laptop_main", "phone_main", "watch_main", "car_main"} <= ids


def test_tools_endpoint_exposes_risk(client: TestClient) -> None:
    tools = client.get("/tools").json()
    by_name = {t["name"]: t for t in tools}

    assert by_name["delete_file"]["risk"] == 3
    assert by_name["delete_file"]["requires_confirmation"] is True
    assert by_name["get_time"]["risk"] == 0


def test_command_endpoint(client: TestClient) -> None:
    response = client.post("/command", json={"text": "what time is it"})
    assert response.status_code == 200
    body = response.json()
    assert body["intent"] == "time"
    assert body["reply"]


def test_command_validation_rejects_empty(client: TestClient) -> None:
    assert client.post("/command", json={"text": ""}).status_code == 422


def test_command_validation_rejects_oversized(client: TestClient) -> None:
    """An oversized paste is a 422 at the HTTP boundary, not a 500 from the route.

    The length cap used to live only on CommandRequest, which the route builds
    *after* FastAPI validates the body -- so a 50k-char body passed the request
    model and raised inside the route (HTTP 500). It is now enforced on the body
    model, so the client learns the limit instead of seeing a server error.
    """
    from jarvis.core.models import MAX_COMMAND_CHARS

    response = client.post("/command", json={"text": "x" * (MAX_COMMAND_CHARS + 1)})
    assert response.status_code == 422
    # The client is told the limit, not just "something went wrong".
    assert "too long" in str(response.json().get("detail"))


def test_command_at_the_length_limit_is_accepted(client: TestClient) -> None:
    """Exactly at MAX_COMMAND_CHARS is a valid (long) command, not a rejection."""
    from jarvis.core.models import MAX_COMMAND_CHARS

    response = client.post("/command", json={"text": "x" * MAX_COMMAND_CHARS})
    # Passes validation (the core may then answer it); it must not be a 422.
    assert response.status_code != 422


def test_audit_endpoint(client: TestClient) -> None:
    client.post("/command", json={"text": "what is my battery"})
    rows = client.get("/audit?limit=5").json()
    assert isinstance(rows, list)
    assert rows


def test_memory_endpoint_roundtrip(client: TestClient) -> None:
    created = client.post(
        "/memory", json={"content": "API test fact about telemetry", "scope": "long_term"}
    )
    assert created.status_code == 200

    listing = client.get("/memory?limit=20").json()
    assert listing["stats"]["total"] >= 1


def test_dashboard_is_served(client: TestClient) -> None:
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert "Vantage" in response.text


# ---------------------------------------------------------------------------
# Voice endpoints (Phase 2)
# ---------------------------------------------------------------------------
def test_voice_status_endpoint(client: TestClient) -> None:
    status = client.get("/voice/status").json()
    assert {"enabled", "wake", "tts", "stt", "audio"} <= set(status)
    assert "jarvis" in status["wake"]["wake_words"]
    assert "available" in status["tts"]


def test_voice_command_requires_wake_word(client: TestClient) -> None:
    body = client.post(
        "/voice/command", json={"text": "what is my battery", "speak": False, "require_wake": True}
    ).json()
    assert body["rejected"] is True
    assert "wake word" in body["reject_reason"]


def test_voice_command_accepts_wake_word(client: TestClient) -> None:
    body = client.post(
        "/voice/command",
        json={"text": "Jarvis, what is my battery", "speak": False, "require_wake": True},
    ).json()
    assert body["rejected"] is False
    assert body["intent"] == "laptop_battery"
    assert "battery" in body["reply"].lower()


def test_voice_command_can_skip_wake_word(client: TestClient) -> None:
    body = client.post(
        "/voice/command", json={"text": "what time is it", "speak": False, "require_wake": False}
    ).json()
    assert body["rejected"] is False
    assert body["intent"] == "time"


def test_voice_command_blocks_dangerous_commands(client: TestClient) -> None:
    body = client.post(
        "/voice/command",
        json={"text": "Jarvis, delete everything", "speak": False, "require_wake": True},
    ).json()
    assert body["decision"] == "BLOCK"


def test_voice_interrupt_endpoint(client: TestClient) -> None:
    assert client.post("/voice/interrupt").json()["interrupted"] is True


def test_voice_reset_endpoint(client: TestClient) -> None:
    assert client.post("/voice/reset").json()["ok"] is True


def test_voice_command_validation(client: TestClient) -> None:
    assert client.post("/voice/command", json={"text": "   "}).status_code == 422


# ---------------------------------------------------------------------------
# Streaming (section 3)
# ---------------------------------------------------------------------------
def test_command_stream_emits_staged_events(client: TestClient) -> None:
    response = client.get("/command/stream", params={"text": "what time is it"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    body = response.text
    assert "event: status" in body
    assert "event: token" in body
    assert "event: done" in body
    # The final frame must carry the complete reply.
    assert '"intent": "time"' in body or '"intent":"time"' in body


def test_command_stream_reports_actions(client: TestClient) -> None:
    response = client.get("/command/stream", params={"text": "what is my battery"})
    assert "event: action" in response.text
    assert "get_battery_status" in response.text


# ---------------------------------------------------------------------------
# Security: authentication
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/health"),
        ("get", "/tools"),
        ("get", "/devices"),
        ("get", "/audit"),
        ("get", "/memory"),
        ("get", "/confirmations"),
        ("get", "/voice/status"),
        ("get", "/projects"),
        ("get", "/tasks"),
    ],
)
def test_read_endpoints_require_auth(anon_client: TestClient, method: str, path: str) -> None:
    assert getattr(anon_client, method)(path).status_code == 401


@pytest.mark.parametrize(
    "path,payload",
    [
        ("/command", {"text": "what is my battery"}),
        ("/memory", {"content": "should not be stored"}),
        ("/voice/command", {"text": "Jarvis, what time is it"}),
        ("/voice/speak", {"text": "should not speak"}),
    ],
)
def test_write_endpoints_require_auth(
    anon_client: TestClient, path: str, payload: dict
) -> None:
    assert anon_client.post(path, json=payload).status_code == 401


def test_wrong_token_is_rejected(anon_client: TestClient) -> None:
    response = anon_client.get("/tools", headers={"X-JARVIS-Token": "not-the-token"})
    assert response.status_code == 401


def test_bearer_token_also_works(anon_client: TestClient) -> None:
    response = anon_client.get("/tools", headers={"Authorization": f"Bearer {TEST_TOKEN}"})
    assert response.status_code == 200


def test_confirm_endpoint_requires_auth(anon_client: TestClient) -> None:
    assert anon_client.post("/confirm/CONF-whatever?approve=true").status_code == 401


def test_dashboard_is_public_and_carries_only_a_bootstrap(anon_client: TestClient) -> None:
    """The dashboard loads without a header, but never receives the master token.

    Phase 10 P0-2. This test previously asserted ``TEST_TOKEN in response.text``,
    i.e. it pinned the vulnerability: an unauthenticated GET handed out the
    credential that authorises ``POST /command``. The dashboard is now served a
    short-lived single-use bootstrap code which the page exchanges at
    ``POST /auth/session`` for a bounded session token.
    """
    response = anon_client.get("/dashboard")
    assert response.status_code == 200
    assert "Vantage" in response.text
    assert TEST_TOKEN not in response.text, "the master token must never reach an anonymous page"
    assert "__JARVIS_BOOTSTRAP__" not in response.text, "placeholder should be replaced"
    assert "jarvis-bootstrap" in response.text, "the page must carry a code to exchange"


def test_anonymous_get_of_auth_endpoints_never_yields_the_master_token(
    anon_client: TestClient,
) -> None:
    """No credential-less route may disclose the token that authorises commands."""
    for path in ("/auth/token", "/dashboard", "/", "/health", "/openapi.json", "/docs"):
        response = anon_client.get(path)
        assert TEST_TOKEN not in response.text, f"{path} disclosed the master token"


# ---------------------------------------------------------------------------
# Security: CORS
# ---------------------------------------------------------------------------
def test_cors_never_returns_a_wildcard(client: TestClient) -> None:
    """A wildcard would let any web page read command results."""
    response = client.get("/health", headers={"Origin": "https://evil.example.com"})
    assert response.headers.get("access-control-allow-origin") != "*"


def test_cors_allows_the_dashboard_origin(client: TestClient) -> None:
    origin = "http://127.0.0.1:8765"
    response = client.get("/health", headers={"Origin": origin})
    assert response.headers.get("access-control-allow-origin") == origin


def test_preflight_from_foreign_origin_does_not_grant_access(anon_client: TestClient) -> None:
    response = anon_client.options(
        "/command",
        headers={
            "Origin": "https://evil.example.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-jarvis-token",
        },
    )
    assert response.headers.get("access-control-allow-origin") != "*"


# ---------------------------------------------------------------------------
# Security: host header (DNS rebinding)
# ---------------------------------------------------------------------------
def test_non_loopback_host_header_is_rejected(anon_client: TestClient) -> None:
    response = anon_client.get("/health", headers={"Host": "evil.example.com"})
    assert response.status_code == 400


@pytest.mark.parametrize("host", ["127.0.0.1:8765", "localhost:8765", "localhost"])
def test_loopback_host_headers_are_accepted(anon_client: TestClient, host: str) -> None:
    response = anon_client.get("/health", headers={"Host": host, **AUTH_HEADERS})
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Security: hardening headers + posture
# ---------------------------------------------------------------------------
def test_security_headers_are_present(client: TestClient) -> None:
    response = client.get("/health")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"


def test_health_reports_security_posture(client: TestClient) -> None:
    security = client.get("/health").json()["security"]
    assert security["auth_enabled"] is True
    assert security["host_header_enforced"] is True
    assert "*" not in security["allowed_origins"]
    assert security["rate_limit_per_minute"] > 0
    assert security["confirm_timeout_seconds"] > 0


def test_health_reports_privacy_posture(client: TestClient) -> None:
    privacy = client.get("/health").json()["privacy"]
    assert privacy["redaction_enabled"] is True
    assert "memories" in privacy["delete_scopes"]


# ---------------------------------------------------------------------------
# Privacy endpoints (section 49)
# ---------------------------------------------------------------------------
def test_privacy_status_endpoint(client: TestClient) -> None:
    info = client.get("/privacy").json()
    assert info["redaction_enabled"] is True
    assert info["export_redacted_by_default"] is True


def test_privacy_export_is_redacted_by_default(client: TestClient) -> None:
    payload = client.get("/privacy/export").json()
    assert payload["redacted"] is True
    assert "data" in payload and "totals" in payload


def test_privacy_mode_toggle(client: TestClient) -> None:
    assert client.post("/privacy/mode", params={"enabled": "true"}).json()["privacy_mode"] is True
    assert client.post("/privacy/mode", params={"enabled": "false"}).json()["privacy_mode"] is False


def test_privacy_preview_does_not_delete(client: TestClient) -> None:
    before = client.get("/memory?limit=1").json()["stats"]["total"]
    preview = client.post("/privacy/preview", json={"scope": "memories"}).json()
    assert "would_remove" in preview
    after = client.get("/memory?limit=1").json()["stats"]["total"]
    assert after == before


def test_privacy_delete_requires_confirm_flag(client: TestClient) -> None:
    """Irreversible deletion must be refused without an explicit confirmation."""
    response = client.post("/privacy/delete", json={"scope": "memories", "confirm": False})
    assert response.status_code == 400
    assert "confirm" in response.json()["detail"]


def test_privacy_delete_rejects_unknown_scope(client: TestClient) -> None:
    response = client.post("/privacy/delete", json={"scope": "nope", "confirm": True})
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# Metrics and failures (section 46)
# ---------------------------------------------------------------------------
def test_metrics_endpoint(client: TestClient) -> None:
    data = client.get("/metrics").json()
    assert {"failures", "audit", "tasks"} <= set(data)
    assert "recovery_rate" in data["failures"]
    assert "chain_intact" in data["audit"]


def test_failures_endpoint(client: TestClient) -> None:
    assert isinstance(client.get("/failures?limit=5").json(), list)


# ---------------------------------------------------------------------------
# Task lifecycle (section 31)
# ---------------------------------------------------------------------------
def test_task_lifecycle_rejects_unknown_action(client: TestClient) -> None:
    response = client.post("/tasks/JOB-nope/frobnicate")
    assert response.status_code == 400


def test_task_lifecycle_404_for_unknown_job(client: TestClient) -> None:
    assert client.post("/tasks/JOB-does-not-exist/pause").status_code == 404


def test_task_lifecycle_roundtrip(client: TestClient) -> None:
    from jarvis.api.app import get_core

    job = get_core().tasks.create("lifecycle test", ["step_a", "step_b"])

    paused = client.post(f"/tasks/{job.job_id}/pause")
    assert paused.status_code == 200
    assert paused.json()["job"]["status"] == "paused"

    resumed = client.post(f"/tasks/{job.job_id}/resume")
    assert resumed.status_code == 200
    # Phase 26 R-11: this used to assert "running", which was the fabricated
    # state -- nothing in this build re-dispatches a job row, so a resumed job
    # reports the state its steps are actually in and says it ran nothing.
    assert resumed.json()["job"]["status"] == "incomplete", resumed.json()
    assert resumed.json()["executed"] == 0, resumed.json()
    assert [s["status"] for s in resumed.json()["job"]["steps"]] == ["pending", "pending"]
    assert "nothing ran" in resumed.json()["note"].lower(), resumed.json()["note"]

    retried = client.post(f"/tasks/{job.job_id}/retry")
    assert retried.status_code == 200
    assert retried.json()["executed"] == 0, retried.json()

    cancelled = client.post(f"/tasks/{job.job_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["job"]["status"] == "cancelled"


# ---------------------------------------------------------------------------
# New endpoints are protected too
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/privacy"),
        ("get", "/privacy/export"),
        ("get", "/metrics"),
        ("get", "/failures"),
    ],
)
def test_new_endpoints_require_auth(anon_client: TestClient, method: str, path: str) -> None:
    assert getattr(anon_client, method)(path).status_code == 401


@pytest.mark.parametrize(
    "path,payload",
    [
        ("/privacy/delete", {"scope": "memories", "confirm": True}),
        ("/privacy/preview", {"scope": "memories"}),
    ],
)
def test_new_post_endpoints_require_auth(
    anon_client: TestClient, path: str, payload: dict
) -> None:
    assert anon_client.post(path, json=payload).status_code == 401


# ---------------------------------------------------------------------------
# Peripherals: the dashboard's view of hardware-facing agents (v1.7/v1.8)
# ---------------------------------------------------------------------------

def test_health_reports_each_peripheral_honestly(client: TestClient) -> None:
    """Each gateway states its own reachability; /health passes it through."""
    peripherals = client.get("/health").json()["peripherals"]
    assert {"car", "watch", "home", "robotics"} <= set(peripherals)

    assert peripherals["car"]["configured"] is False
    assert peripherals["car"]["checked"] is False, "health must not probe the car"

    assert peripherals["watch"]["auth_state"] in {"unpaired", "pending", "paired", "revoked"}
    assert peripherals["watch"]["mode"] in {"queue", "simulate", "bridge"}

    assert peripherals["home"]["online"] is False
    assert peripherals["robotics"]["online"] is False
    assert peripherals["robotics"]["faults"] == []
    # "offline" and "configured but failing" must stay distinguishable, so the
    # failure evidence travels with the report.
    assert peripherals["home"]["bridge_error"] == ""
    assert peripherals["robotics"]["bridge_error"] == ""


def test_a_broken_peripheral_cannot_take_health_down(client: TestClient, monkeypatch) -> None:
    """One unplugged adapter is a row in the dashboard, not a dead API."""
    from jarvis.car import gateway as car_gateway

    def _explode() -> None:
        raise RuntimeError("adapter exploded")

    monkeypatch.setattr(car_gateway.get_car_gateway(), "posture", _explode)
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "adapter exploded" in body["peripherals"]["car"]["error"]
    assert body["peripherals"]["watch"]["mode"]


def test_dashboard_serves_the_peripheral_card(client: TestClient) -> None:
    """The page that renders this data must actually contain the card."""
    html = client.get("/dashboard").text
    assert 'id="peripherals"' in html
    assert "loadPeripherals" in html
    assert 'AUTH_WORD' in html, "the devices card must name the real state, not ONLINE/OFFLINE"


def test_dashboard_reads_health_once_per_cycle(client: TestClient) -> None:
    """One /health fetch feeds the header, peripherals and devices cards.

    Checked at source level on purpose: the dashboard polls every 8s forever, so
    a second fetch would quietly double the load on the endpoint that the car,
    watch and home panels all depend on.
    """
    html = client.get("/dashboard").text
    assert html.count('get("/health")') == 1


def test_dashboard_microphone_row_has_every_state(client: TestClient) -> None:
    """Every state the probe can report needs its own word.

    "Available"/"unavailable" hid three different facts, and the async probe
    added a fourth: an answer on its way is not an unreadable machine.
    """
    html = client.get("/dashboard").text

    assert "micRow(audio)" in html
    for word in ("ready", "measuring…", "not permitted", "no capture backend",
                 "consent unreadable"):
        assert f'"{word}"' in html, f"microphone state {word!r} is not named in the UI"
    assert 'line("Microphone", audio.capture_available' not in html


# ---------------------------------------------------------------------------
# /ai/chats — a conversation must be listable, or it cannot be resumed
# ---------------------------------------------------------------------------
# The dashboard started a new conversation on every page load and had no way
# to list the ones already stored. The ids were never lost; they were only
# unreachable. These pin the listing contract the page now depends on.
def test_conversations_can_be_listed_most_recent_first(client: TestClient) -> None:
    first = client.post("/ai/chats", json={"title": "First conversation"}).json()
    second = client.post("/ai/chats", json={"title": "Second conversation"}).json()
    client.post(f"/ai/chats/{first['id']}/messages",
                json={"role": "user", "content": "hello"})
    client.post(f"/ai/chats/{second['id']}/messages",
                json={"role": "user", "content": "hello"})

    listed = client.get("/ai/chats").json()
    ids = [c["id"] for c in listed]
    assert second["id"] in ids and first["id"] in ids, "the listing dropped a conversation"
    # `second` was updated last, so it leads.
    assert ids[0] == second["id"], "the listing is not most-recent-first"

    for entry in listed:
        assert {"id", "title", "created_at", "updated_at", "message_count"} <= set(entry), (
            "a listing entry is missing a field the history panel reads")
        assert entry["message_count"] >= 1


def test_listing_respects_the_limit(client: TestClient) -> None:
    for i in range(3):
        client.post("/ai/chats", json={"title": f"Limit {i}"})
    assert len(client.get("/ai/chats?limit=2").json()) == 2


def test_listing_requires_a_credential() -> None:
    anon = TestClient(app, base_url="http://127.0.0.1:8765").get("/ai/chats")
    assert anon.status_code == 401, "the conversation index is readable without a token"


def test_a_single_conversation_route_still_works(client: TestClient) -> None:
    """The listing is additive: `GET /ai/chats/{id}` is unchanged."""
    made = client.post("/ai/chats", json={"title": "Still addressable"}).json()
    got = client.get(f"/ai/chats/{made['id']}")
    assert got.status_code == 200 and got.json()["title"] == "Still addressable"
