"""The mission API: the wire is protected, honest, and enforces the state machine.

The routes are thin, so these tests are about the contract crossing the wire:
authentication is required, an illegal transition is refused with a reason a
client can show, and a mission created over HTTP is durable.
"""

from __future__ import annotations

from tests.conftest import TEST_TOKEN

HEADERS = {"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"}


def _client():
    from fastapi.testclient import TestClient
    from jarvis.api.app import app

    return TestClient(app, base_url="http://127.0.0.1:8765")


def test_mission_routes_require_authentication():
    """No token, no missions. The surface is not public."""
    client = _client()
    response = client.get("/missions", headers={"Host": "127.0.0.1"})
    assert response.status_code in (401, 403), response.text


def test_create_and_read_a_mission_over_http():
    client = _client()
    created = client.post("/missions", headers=HEADERS, json={
        "request": "organize the sample project",
        "conditions": [{"statement": "README documents the flag",
                        "verifier": "file content"}],
        "steps": [{"title": "update the README", "tool": "read_file"}],
    })
    assert created.status_code == 200, created.text
    payload = created.json()

    assert payload["state"] == "draft"
    assert payload["request"] == "organize the sample project"
    assert payload["verdict"] == "unknown", (
        "a fresh mission has checked nothing, so it cannot claim success")

    fetched = client.get(f"/missions/{payload['mission_id']}", headers=HEADERS)
    assert fetched.status_code == 200
    assert fetched.json()["mission_id"] == payload["mission_id"]


def test_an_illegal_transition_is_a_409_with_the_reason():
    """The client can show the person why the move was refused."""
    client = _client()
    mission_id = client.post("/missions", headers=HEADERS, json={
        "request": "one condition", "conditions": [{"statement": "x"}],
    }).json()["mission_id"]

    refused = client.post(f"/missions/{mission_id}/transition", headers=HEADERS,
                          json={"state": "completed"})
    assert refused.status_code == 409, refused.text
    assert "not a legal transition" in refused.json()["detail"]


def test_an_unknown_mission_is_a_404_not_an_empty_success():
    client = _client()
    assert client.get("/missions/MIS-does-not-exist", headers=HEADERS).status_code == 404


def test_the_list_includes_what_was_created():
    client = _client()
    mission_id = client.post("/missions", headers=HEADERS, json={
        "request": "listed mission", "conditions": [{"statement": "x"}],
    }).json()["mission_id"]

    listed = client.get("/missions", headers=HEADERS).json()["missions"]
    assert any(m["mission_id"] == mission_id for m in listed)


def test_a_mission_needs_a_request():
    client = _client()
    assert client.post("/missions", headers=HEADERS,
                       json={"request": "   "}).status_code == 422


def test_cancel_moves_a_draft_to_cancelled():
    client = _client()
    mission_id = client.post("/missions", headers=HEADERS, json={
        "request": "cancel me", "conditions": [{"statement": "x"}],
    }).json()["mission_id"]

    cancelled = client.post(f"/missions/{mission_id}/cancel", headers=HEADERS)
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["state"] == "cancelled"

# -- ParallelWorld over the wire -------------------------------------------

def test_preview_does_not_write_and_apply_requires_approval(tmp_path):
    """The two promises, checked through HTTP rather than the class."""
    target = tmp_path / "doc.txt"
    target.write_text("before\n", encoding="utf-8")
    client = _client()
    ops = [{"op": "replace", "path": str(target), "content": "after\n"}]

    preview = client.post("/files/preview", headers=HEADERS, json={"operations": ops})
    assert preview.status_code == 200, preview.text
    assert preview.json()["applicable"] is True
    assert target.read_text(encoding="utf-8") == "before\n", "preview wrote to the file"

    refused = client.post("/files/apply", headers=HEADERS, json={"operations": ops})
    assert refused.status_code == 409
    assert target.read_text(encoding="utf-8") == "before\n"

    applied = client.post("/files/apply", headers=HEADERS,
                          json={"operations": ops, "approved": True})
    assert applied.status_code == 200, applied.text
    assert applied.json()["applied"] is True
    assert target.read_text(encoding="utf-8") == "after\n"


def test_the_file_routes_require_authentication(tmp_path):
    client = _client()
    response = client.post("/files/preview", headers={"Host": "127.0.0.1"},
                           json={"operations": [{"op": "delete",
                                                 "path": str(tmp_path / "x")}]})
    assert response.status_code in (401, 403)
