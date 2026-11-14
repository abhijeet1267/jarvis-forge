"""Professional networking — a recap drafted from work that was actually done.

Publishing stays refused: there is no approved LinkedIn integration, and the
product says so instead of offering a button that cannot work. What *is* real is
the part before publishing: a draft whose content comes only from tasks the
planner records as completed, grouped by project, with the count stated.

The honesty case is the one that matters: with nothing completed, no draft is
created and the reply says why. A fluent post about progress that did not happen
would be exactly the false success this project keeps refusing.
"""

from __future__ import annotations

from datetime import datetime, timedelta

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


def _done(client, title: str, project: str = "") -> str:
    """Create a task and complete it through the real routes."""
    task = client.post("/planner/tasks", json={"title": title, "project": project}).json()
    ref = task["task_ref"]
    client.post(f"/planner/tasks/{ref}/complete", json={})
    return ref


def test_nothing_completed_creates_no_draft_and_says_why(client):
    data = client.post("/posts/recap", json={"days": 7}).json()

    assert data["created"] is None, data
    assert data["completed_count"] == 0
    assert "nothing was completed" in data["reason"].lower(), data
    assert client.get("/posts").json()["drafts"] == [], "no draft may be invented"


def test_the_recap_names_only_work_that_was_completed(client):
    _done(client, "Ship the AMR demo", project="AMR")
    client.post("/planner/tasks", json={"title": "Something still open", "project": "AMR"})

    data = client.post("/posts/recap", json={"days": 7}).json()

    assert data["completed_count"] == 1, data
    body = data["created"]["body"]
    assert "Ship the AMR demo" in body, body
    assert "Something still open" not in body, "open work must not be claimed as done"
    assert "AMR:" in body, body


def test_completed_work_is_grouped_by_project(client):
    _done(client, "Write the release notes", project="Launch")
    _done(client, "Fix the pairing bug", project="Mobile")
    _done(client, "Tidy the inbox", project="")

    data = client.post("/posts/recap", json={"days": 7}).json()

    body = data["created"]["body"]
    for group in ("Launch:", "Mobile:", "General:"):
        assert group in body, (group, body)
    assert data["completed_count"] == 3


def test_the_draft_is_saved_as_a_real_draft_and_never_published(client):
    _done(client, "Close out the audit")

    data = client.post("/posts/recap", json={"days": 7}).json()

    assert data["created"]["status"] == "draft"
    assert data["publishing"]["supported"] is False
    drafts = client.get("/posts").json()["drafts"]
    assert any(d["draft_id"] == data["created"]["draft_id"] for d in drafts)


def test_the_draft_says_it_is_a_draft_and_a_person_posts_it(client):
    _done(client, "Finish the acceptance report")

    body = client.post("/posts/recap", json={"days": 7}).json()["created"]["body"]

    assert "Drafted by Vantage" in body, body
    assert "does not publish for you" in body, body


def test_work_completed_before_the_window_is_not_claimed(client):
    from jarvis.db.database import get_db

    ref = _done(client, "Old victory")
    old = (datetime.now().astimezone() - timedelta(days=40)).isoformat()
    get_db().execute("UPDATE vantage_tasks SET done_at = ? WHERE task_ref = ?", (old, ref))

    data = client.post("/posts/recap", json={"days": 7}).json()

    assert data["created"] is None, data
    assert data["completed_count"] == 0
    assert "Old victory" not in str(data)


def test_a_wider_window_includes_older_completions(client):
    _done(client, "A recent win")
    from jarvis.db.database import get_db

    ref = _done(client, "A win from last month")
    old = (datetime.now().astimezone() - timedelta(days=20)).isoformat()
    get_db().execute("UPDATE vantage_tasks SET done_at = ? WHERE task_ref = ?", (old, ref))

    week = client.post("/posts/recap", json={"days": 7}).json()
    month = client.post("/posts/recap", json={"days": 30}).json()

    assert week["completed_count"] == 1, week
    assert month["completed_count"] == 2, month["completed_count"]
    assert "A win from last month" in month["created"]["body"]


@pytest.mark.parametrize("days", [0, -1, 400, "many"])
def test_an_impossible_window_is_refused(client, days):
    assert client.post("/posts/recap", json={"days": days}).status_code == 422