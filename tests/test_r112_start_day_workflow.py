"""Workflow A — "start my day" composes real state into one actionable plan."""

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


def test_an_empty_day_says_so_honestly(client):
    data = client.post("/planner/start-day", json={}).json()
    assert data["task_count"] == 0
    assert "Nothing is pending" in data["headline"], data["headline"]
    assert data["next_action"] is None


def test_the_first_task_becomes_the_headline_with_its_rule(client):
    client.post("/planner/tasks", json={"title": "Ship the demo", "priority": "urgent"})
    data = client.post("/planner/start-day", json={}).json()
    assert "Ship the demo" in data["headline"]
    assert "highest priority" in data["headline"], data["headline"]
    assert data["next_action"]["task"]["title"] == "Ship the demo"


def test_an_overdue_task_leads_and_is_reported(client):
    client.post("/planner/tasks", json={
        "title": "Late thing", "due_at": "2020-01-01T09:00:00"})
    data = client.post("/planner/start-day", json={}).json()
    assert "Late thing" in data["headline"]
    assert any(t["title"] == "Late thing" for t in data["overdue"])
    assert any(i["kind"] == "task_overdue" for i in data["attention"]), data["attention"]


def test_it_reports_which_steps_ran_and_changes_nothing(client):
    before = client.get("/planner/tasks").json()
    data = client.post("/planner/start-day", json={}).json()
    after = client.get("/planner/tasks").json()
    assert data["workflow"], "the workflow steps must be reported"
    assert any("calendar" in s for s in data["workflow"]), data["workflow"]
    assert "changed nothing" in data["note"]
    assert len(before) == len(after), "start-day must not create tasks"


def test_an_unreadable_calendar_is_called_out(client, monkeypatch):
    from jarvis.integrations.calendar_provider import ProviderResults, ProviderStatus

    class Broken:
        def list_events(self, *a, **k):
            return ProviderResults([], ProviderStatus.UNAVAILABLE, detail="host down")

    monkeypatch.setattr("jarvis.integrations.calendar_provider.get_calendar_provider",
                        lambda settings=None: Broken())
    data = client.post("/planner/start-day", json={}).json()
    assert data["calendar_read_failed"] is True
    assert any("could not be read" in s for s in data["workflow"]), data["workflow"]


def test_start_my_day_is_reachable_by_sentence(core):
    import asyncio
    from jarvis.core.models import CommandRequest

    for text in ("start my day", "begin the day"):
        plan = core.planner.parse(text, {})
        assert [a.tool for a in plan.actions] == ["start_day"], (text, plan.actions)

    asyncio.run(core.handle(CommandRequest(text="add a task to ship the AMR demo",
                                           source="t", session_id="sd-1")))
    response = asyncio.run(core.handle(CommandRequest(text="start my day",
                                                      source="t", session_id="sd-1")))
    assert response.actions and response.actions[0].ok, response.reply
    assert "ship the amr demo" in response.reply.lower(), response.reply
    assert "nothing was changed" in response.reply.lower(), response.reply


@__import__("pytest").mark.skipif(
    not __import__("importlib").util.find_spec("playwright"),
    reason="playwright not installed")
def test_the_start_day_button_works_in_the_page(isolated_server):
    from playwright.sync_api import sync_playwright

    base = isolated_server["base"]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(f"{base}/planner", wait_until="networkidle")
        page.wait_for_timeout(1200)
        # Unique title: the isolated server is shared, so an earlier test's task
        # may still be first. What matters is that start-day produced a real
        # headline from live state.
        page.fill("#title", "r112-start-day-demo")
        page.click("#add-form button[type=submit]")
        page.wait_for_timeout(1200)
        page.click("#start-day")
        page.wait_for_timeout(1600)
        note = page.locator("#start-day-note").inner_text()
        assert "r112-start-day-demo" in note or "Start with:" in note, note
        browser.close()
