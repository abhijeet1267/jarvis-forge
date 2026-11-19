"""`/command` deduplication, opt-in through the Idempotency-Key header.

The gap this closes: two identical POSTs used to produce two missions and run the
action twice, with no request id anywhere on the path. The fix must not change a
caller that does not ask for it -- there are ~18 of them -- so suppression is
opt-in by header, and these tests pin all three behaviours.

Same TestClient setup as tests/test_command_idempotency.py, so this drives the
real route rather than a stand-in.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jarvis.api.app import app
from tests.conftest import AUTH_HEADERS


def _client() -> TestClient:
    return TestClient(app, base_url="http://127.0.0.1:8765", headers=AUTH_HEADERS)


def _post(client, text, key=None, session="idem-test", device=None):
    headers = {}
    if key is not None:
        headers["Idempotency-Key"] = key
    if device is not None:
        headers["X-Vantage-Device-Id"] = device
    return client.post("/command", headers=headers,
                       json={"text": text, "session_id": session})


@pytest.fixture
def client():
    return _client()


def test_a_repeated_key_does_not_run_the_command_twice(client):
    first = _post(client, "open Calculator", key="k-1")
    assert first.status_code == 200, first.text
    second = _post(client, "open Calculator", key="k-1")
    assert second.status_code == 200, second.text

    a, b = first.json(), second.json()
    assert b["intent"] == "idempotent_replay", (
        "the repeat was not recognised as a duplicate: it ran again")
    assert a["mission_id"] and b["mission_id"] == a["mission_id"], (
        "the repeat did not reconcile to the original mission")


def test_the_same_key_with_different_text_is_refused(client):
    _post(client, "open Calculator", key="k-2")
    conflicted = _post(client, "open TextEdit", key="k-2")
    assert conflicted.status_code == 409, (
        "a key reused for different content must be refused, not silently "
        f"treated as a repeat; got {conflicted.status_code}")
    assert "Idempotency-Key" in conflicted.json().get("detail", "")


def test_no_key_keeps_the_old_behaviour(client):
    """Existing callers send no header and must be untouched.

    Two identical no-key requests are two submissions: they get two missions.
    Suppressing that would change 18 callers to fix one.
    """
    a = _post(client, "open Calculator", key=None).json()
    b = _post(client, "open Calculator", key=None).json()
    assert a["intent"] == "open_app" and b["intent"] == "open_app"
    assert a["mission_id"] != b["mission_id"], (
        "a no-key request was deduplicated; existing callers would change")


def test_different_keys_for_the_same_text_both_run(client):
    a = _post(client, "open Calculator", key="k-3a").json()
    b = _post(client, "open Calculator", key="k-3b").json()
    assert a["intent"] == "open_app" and b["intent"] == "open_app"
    assert a["mission_id"] != b["mission_id"]


def test_one_device_key_cannot_suppress_another_devices_command(client):
    """Device scope is part of the key's identity.

    Two device ids using the same key text must not see each other's record.
    """
    a = _post(client, "open Calculator", key="shared-key", device="dev-alpha")
    b = _post(client, "open Calculator", key="shared-key", device="dev-beta")
    assert a.status_code == 200 and b.status_code == 200
    assert b.json()["intent"] != "idempotent_replay", (
        "one device's key suppressed another device's command")