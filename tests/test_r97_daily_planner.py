"""Epic 1 — the daily planner: tasks, the day's agenda, and the next action.

A *task* here is the person's own to-do item, distinct from a *workflow run*
(``tasks``/``task_steps``, which is Vantage executing a multi-step goal). The
planner joins persisted tasks with calendar events that were really read, and is
careful about the things a planner most easily gets wrong:

* it never invents data — an unread calendar is *reported unread*, not shown as
  an empty day;
* a task waiting on unfinished dependencies is **blocked**, never listed as the
  next action;
* "next" is picked by a stated rule (overdue, then priority, then due time) and
  the rule is returned, so the recommendation is justifiable;
* overlapping events are **named as conflicts**, not silently stacked.

The routes are exercised through the real app (the same code the UI calls) so
this is an end-to-end check, not a unit test of a helper.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest


@pytest.fixture(autouse=True)
def _clean_tasks():
    """Each test starts with an empty task list.

    The session's JARVIS_HOME (and its sqlite db) is shared across the whole
    run, so a task created by one test would otherwise be "open" in the next and
    change which task the planner picks. Clearing the planner's own table per test
    keeps each test's expectation about *its* tasks meaningful.
    """
    from jarvis.db.database import get_db

    db = get_db()
    db.execute("DELETE FROM vantage_tasks")
    yield


@pytest.fixture()
def client():
    """The real app, exercised through TestClient (the same routes the UI calls).

    Authenticated with the project's test token (set by conftest), and a
    loopback base_url (the API rejects a non-loopback Host to defeat DNS
    rebinding).
    """
    from fastapi.testclient import TestClient
    from jarvis.api.app import app
    from tests.conftest import AUTH_HEADERS

    client = TestClient(app, base_url="http://127.0.0.1:8765")
    client.headers.update(AUTH_HEADERS)
    return client


# ---------------------------------------------------------------------------
# Task persistence through the real routes
# ---------------------------------------------------------------------------

def test_a_task_is_created_listed_and_persisted(client):
    created = client.post("/planner/tasks", json={
        "title": "Finish the AMR report", "priority": "high",
        "estimate_minutes": 90, "project": "AMR",
    })
    assert created.status_code == 200, created.text
    task = created.json()
    assert task["task_ref"].startswith("VT")
    assert task["status"] == "open" and task["priority"] == "high"

    listed = client.get("/planner/tasks").json()
    assert any(t["task_ref"] == task["task_ref"] for t in listed), listed

    # Persisted: a fresh read (new request) still finds it.
    again = [t for t in client.get("/planner/tasks").json()
             if t["task_ref"] == task["task_ref"]]
    assert again and again[0]["title"] == "Finish the AMR report"


def test_a_task_needs_a_title(client):
    assert client.post("/planner/tasks", json={"title": "   "}).status_code == 422
    assert client.post("/planner/tasks", json={}).status_code == 422


def test_a_task_can_be_edited(client):
    task = client.post("/planner/tasks", json={"title": "Draft"}).json()
    edited = client.patch(f"/planner/tasks/{task['task_ref']}",
                          json={"title": "Draft v2", "priority": "urgent"})
    assert edited.status_code == 200, edited.text
    assert edited.json()["title"] == "Draft v2"
    assert edited.json()["priority"] == "urgent"


def test_completing_a_task_records_evidence_and_closes_it(client):
    task = client.post("/planner/tasks", json={"title": "Ship it"}).json()
    done = client.post(f"/planner/tasks/{task['task_ref']}/complete",
                       json={"evidence": "signed and installed"}).json()
    assert done["status"] == "done"
    assert done["evidence"] == "signed and installed"
    assert done["done_at"]
    # A done task is no longer in the open list.
    open_refs = {t["task_ref"] for t in client.get("/planner/tasks").json()}
    assert task["task_ref"] not in open_refs


def test_cancelling_a_task_removes_it_from_open(client):
    task = client.post("/planner/tasks", json={"title": "Old idea"}).json()
    assert client.delete(f"/planner/tasks/{task['task_ref']}").status_code == 200
    open_refs = {t["task_ref"] for t in client.get("/planner/tasks").json()}
    assert task["task_ref"] not in open_refs


def test_unknown_task_is_a_404(client):
    assert client.patch("/planner/tasks/VT-nope", json={"title": "x"}).status_code == 404
    assert client.post("/planner/tasks/VT-nope/complete", json={}).status_code == 404
    assert client.delete("/planner/tasks/VT-nope").status_code == 404


# ---------------------------------------------------------------------------
# The day: agenda, next action, blocking, conflicts
# ---------------------------------------------------------------------------

def test_today_returns_an_agenda_with_the_next_action(client):
    client.post("/planner/tasks", json={"title": "Low thing", "priority": "low"})
    top = client.post("/planner/tasks", json={"title": "Urgent thing", "priority": "urgent"}).json()
    agenda = client.get("/planner/today").json()
    assert agenda["day"]
    assert agenda["next_action"], agenda
    assert agenda["next_action"]["task"]["task_ref"] == top["task_ref"], agenda["next_action"]
    assert agenda["next_action"]["rule"], "the rule that picked the action must be stated"


def test_an_overdue_task_is_picked_first_and_flagged(client):
    past = (datetime.now().astimezone() - timedelta(days=1)).isoformat()
    overdue = client.post("/planner/tasks", json={
        "title": "Overdue thing", "priority": "low", "due_at": past}).json()
    client.post("/planner/tasks", json={"title": "Fresh urgent", "priority": "urgent"})
    agenda = client.get("/planner/today").json()
    assert agenda["next_action"]["task"]["task_ref"] == overdue["task_ref"], agenda["next_action"]
    assert any(t["task_ref"] == overdue["task_ref"] and t["overdue"] for t in agenda["overdue"])


def test_a_blocked_task_is_not_the_next_action(client):
    blocker = client.post("/planner/tasks", json={"title": "Blocker", "priority": "urgent"}).json()
    blocked = client.post("/planner/tasks", json={
        "title": "Blocked", "priority": "urgent", "depends_on": [blocker["task_ref"]]}).json()
    agenda = client.get("/planner/today").json()
    assert agenda["next_action"]["task"]["task_ref"] != blocked["task_ref"], agenda["next_action"]
    assert any(b["task_ref"] == blocked["task_ref"] for b in agenda["blocked"]), agenda["blocked"]
    # Once the blocker is done, the dependent becomes doable.
    client.post(f"/planner/tasks/{blocker['task_ref']}/complete", json={})
    after = client.get("/planner/today").json()
    assert not any(b["task_ref"] == blocked["task_ref"] for b in after["blocked"])
    assert after["next_action"]["task"]["task_ref"] == blocked["task_ref"], after["next_action"]


def test_an_unread_calendar_is_reported_not_shown_as_empty(client, monkeypatch):
    """A failed calendar read must not read as "nothing scheduled"."""
    import jarvis.api.app as api
    from jarvis.integrations.calendar_provider import ProviderResults, ProviderStatus

    class Broken:
        def list_events(self, *a, **k):
            return ProviderResults([], ProviderStatus.UNAVAILABLE, detail="host down")

    monkeypatch.setattr("jarvis.integrations.calendar_provider.get_calendar_provider",
                        lambda settings=None: Broken())
    agenda = client.get("/planner/today").json()
    assert agenda["calendar_read_failed"] is True, agenda
    assert agenda["events"] == []


def test_overlapping_events_are_reported_as_conflicts(client, monkeypatch):
    """Two events that overlap are named, not silently stacked."""
    from datetime import datetime as _dt, timedelta as _td
    from jarvis.integrations.calendar_provider import (
        CalendarEvent, ProviderResults, ProviderStatus)

    now = _dt.now().astimezone()
    start = now.replace(hour=10, minute=0, second=0, microsecond=0)
    events = [
        CalendarEvent(uid="a", title="Standup", start=start, end=start + _td(hours=1)),
        CalendarEvent(uid="b", title="Design review", start=start + _td(minutes=30),
                      end=start + _td(hours=2)),
    ]

    class Reads:
        def list_events(self, *a, **k):
            return ProviderResults(events, ProviderStatus.HAS_RESULTS)

    monkeypatch.setattr("jarvis.integrations.calendar_provider.get_calendar_provider",
                        lambda settings=None: Reads())
    agenda = client.get("/planner/today").json()
    assert agenda["calendar_read_failed"] is False
    assert len(agenda["conflicts"]) == 1, agenda["conflicts"]
    conflict = agenda["conflicts"][0]
    assert {conflict["first"], conflict["second"]} == {"Standup", "Design review"}


def test_two_events_that_merely_abut_are_not_a_conflict(client, monkeypatch):
    """10:00-11:00 and 11:00-12:00 do not overlap; a boundary is not a clash."""
    from datetime import datetime as _dt, timedelta as _td
    from jarvis.integrations.calendar_provider import (
        CalendarEvent, ProviderResults, ProviderStatus)

    start = _dt.now().astimezone().replace(hour=10, minute=0, second=0, microsecond=0)
    events = [
        CalendarEvent(uid="a", title="One", start=start, end=start + _td(hours=1)),
        CalendarEvent(uid="b", title="Two", start=start + _td(hours=1), end=start + _td(hours=2)),
    ]

    class Reads:
        def list_events(self, *a, **k):
            return ProviderResults(events, ProviderStatus.HAS_RESULTS)

    monkeypatch.setattr("jarvis.integrations.calendar_provider.get_calendar_provider",
                        lambda settings=None: Reads())
    assert client.get("/planner/today").json()["conflicts"] == []

# ---------------------------------------------------------------------------
# Natural language: "plan my day" reaches the planner, not the calendar
# ---------------------------------------------------------------------------

def test_natural_language_reaches_the_planner_tools(core):
    """A planning sentence is planned as plan_day, not as a calendar listing."""
    for text in ("plan my day", "what should i work on next", "what's next"):
        plan = core.planner.parse(text, {})
        assert [a.tool for a in plan.actions] == ["plan_day"], (text, plan.intent, plan.actions)


def test_adding_a_task_by_sentence_persists_it(core):
    import asyncio
    from jarvis.core.models import CommandRequest

    response = asyncio.run(core.handle(CommandRequest(
        text="add a task to finish the AMR report", source="nl", session_id="nl-add")))
    assert response.actions and response.actions[0].ok, response.reply
    assert "finish the amr report" in response.reply.lower(), response.reply
    # It is on the list.
    listed = asyncio.run(core.handle(CommandRequest(
        text="list my tasks", source="nl", session_id="nl-add")))
    assert "finish the amr report" in listed.reply.lower(), listed.reply


def test_a_planner_reply_is_not_hedged(core):
    """A read and a completed local write are verified, so not "could not verify"."""
    import asyncio
    from jarvis.core.models import CommandRequest

    add = asyncio.run(core.handle(CommandRequest(
        text="add a task to check the build", source="nl", session_id="nl-hedge")))
    plan = asyncio.run(core.handle(CommandRequest(
        text="plan my day", source="nl", session_id="nl-hedge")))
    for response in (add, plan):
        assert "could not verify" not in response.reply, response.reply


def test_a_generic_scheduling_request_still_goes_to_the_calendar(core):
    """The planner route must not steal "schedule a meeting"."""
    plan = core.planner.parse("schedule a meeting with Alice tomorrow at 5pm", {})
    assert [a.tool for a in plan.actions] == ["create_event"], plan.actions
