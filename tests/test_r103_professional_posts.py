"""Feature 6 — professional posts: draft, schedule, remind; publish is refused.

Vantage has no approved publishing integration. The honest product behaviour is
to draft and schedule, and to *refuse* a publish with a clear reason rather than
report a post that never happened.
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from jarvis.api.app import app
    from tests.conftest import AUTH_HEADERS
    c = TestClient(app, base_url="http://127.0.0.1:8765")
    c.headers.update(AUTH_HEADERS)
    return c


@pytest.fixture(autouse=True)
def _clean_posts():
    from jarvis.db.database import get_db
    get_db().execute("DELETE FROM post_drafts")
    yield


def test_a_draft_is_saved_and_listed(client):
    created = client.post("/posts", json={"body": "Shipping the planner today."})
    assert created.status_code == 200, created.text
    draft = created.json()
    assert draft["status"] == "draft" and draft["channel"] == "linkedin"
    listed = client.get("/posts").json()
    assert any(d["draft_id"] == draft["draft_id"] for d in listed["drafts"])


def test_a_draft_needs_text(client):
    assert client.post("/posts", json={"body": "   "}).status_code == 422


def test_a_draft_can_be_edited(client):
    draft = client.post("/posts", json={"body": "First"}).json()
    edited = client.patch(f"/posts/{draft['draft_id']}", json={"body": "Second"}).json()
    assert edited["body"] == "Second"


def test_scheduling_sets_status_and_due_reminders_surface_it(client):
    past = "2020-01-01T09:00:00"
    draft = client.post("/posts", json={"body": "Scheduled post", "scheduled_for": past}).json()
    assert draft["status"] == "scheduled"
    listed = client.get("/posts").json()
    assert any(d["draft_id"] == draft["draft_id"] for d in listed["due"]), listed


def test_publish_is_refused_with_a_reason(client):
    draft = client.post("/posts", json={"body": "Do not pretend"}).json()
    r = client.post(f"/posts/{draft['draft_id']}/publish")
    assert r.status_code == 501, r.text
    assert "no approved publishing integration" in r.json()["detail"].lower()
    # And it did not change the draft's status to published.
    assert client.get("/posts").json()["drafts"][0]["status"] in ("draft", "scheduled")


def test_publishing_is_reported_unsupported(client):
    listed = client.get("/posts").json()
    assert listed["publishing"]["supported"] is False
    assert listed["publishing"]["reason"]


def test_a_draft_can_be_deleted(client):
    draft = client.post("/posts", json={"body": "temp"}).json()
    assert client.delete(f"/posts/{draft['draft_id']}").status_code == 200
    assert not any(d["draft_id"] == draft["draft_id"] for d in client.get("/posts").json()["drafts"])
