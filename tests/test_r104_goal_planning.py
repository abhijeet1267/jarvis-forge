"""Feature 7 — connect the goal: a goal becomes ordered, real planner tasks."""

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
    get_db().execute("DELETE FROM vantage_tasks")
    yield


def test_a_goal_becomes_ordered_tasks(client):
    plan = client.post("/planner/plan-goal",
                       json={"goal": "Ship the AMR demo", "project": "AMR"}).json()
    tasks = plan["tasks"]
    assert len(tasks) == 3
    assert tasks[0]["title"].startswith("Define what")
    assert "Ship the AMR demo" in tasks[1]["title"]
    assert tasks[2]["title"].startswith("Verify")
    # Ordered: each depends on the one before it.
    assert tasks[0]["depends_on"] == []
    assert tasks[1]["depends_on"] == [tasks[0]["task_ref"]]
    assert tasks[2]["depends_on"] == [tasks[1]["task_ref"]]
    assert all(t["project"] == "AMR" for t in tasks)


def test_the_plan_is_persisted_and_blocked_in_order(client):
    plan = client.post("/planner/plan-goal", json={"goal": "Ship it"}).json()
    listed = {t["task_ref"]: t for t in client.get("/planner/tasks").json()}
    assert plan["tasks"][1]["task_ref"] in listed
    # The second task is blocked until the first is done.
    assert listed[plan["tasks"][1]["task_ref"]]["blocked"] is True
    assert listed[plan["tasks"][0]["task_ref"]]["blocked"] is False


def test_planning_creates_no_calendar_event(client):
    """A plan is tasks, not calendar writes."""
    plan = client.post("/planner/plan-goal", json={"goal": "Something"}).json()
    assert "calendar" in plan["note"].lower()
    for t in plan["tasks"]:
        assert "start_at" not in t or not t.get("start_at")


def test_a_goal_needs_text(client):
    assert client.post("/planner/plan-goal", json={"goal": "  "}).status_code == 422
