"""E10 — "needs attention" gathers real problems, and invents none."""

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
def _clean():
    from jarvis.db.database import get_db
    db = get_db()
    db.execute("DELETE FROM vantage_tasks")
    db.execute("DELETE FROM post_drafts")
    yield


def test_nothing_to_report_is_an_empty_list(client):
    data = client.get("/attention").json()
    assert data["count"] == 0 and data["items"] == []


def test_an_overdue_task_is_reported_with_where_to_fix_it(client):
    client.post("/planner/tasks", json={
        "title": "Late thing", "due_at": "2020-01-01T09:00:00"})
    data = client.get("/attention").json()
    assert data["count"] >= 1
    item = next(i for i in data["items"] if i["kind"] == "task_overdue")
    assert "Late thing" in item["title"]
    assert item["surface"] == "planner"


def test_a_due_post_schedule_is_a_reminder_not_a_publication(client):
    client.post("/posts", json={"body": "Due post", "scheduled_for": "2020-01-01T09:00:00"})
    data = client.get("/attention").json()
    item = next(i for i in data["items"] if i["kind"] == "post_reminder")
    assert item["surface"] == "operator"
    # Reminder only — the draft is still not published.
    assert client.get("/posts").json()["drafts"][0]["status"] == "scheduled"


def test_every_item_names_a_surface_that_can_resolve_it(client):
    client.post("/planner/tasks", json={"title": "x", "due_at": "2020-01-01T09:00:00"})
    for item in client.get("/attention").json()["items"]:
        assert item["surface"] in ("planner", "work", "operator"), item
        assert item["title"], item
