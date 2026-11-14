"""Workflow C — preparing for a meeting from real calendar, planner and mail.

The briefing is assembled from data Vantage actually holds. It reports steps it
ran, asks rather than guesses when a name is ambiguous, and never claims to
attend the meeting or to have sent anything.
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

    get_db().execute("DELETE FROM calendar_events")
    get_db().execute("DELETE FROM vantage_tasks")
    yield


def _event(title: str, *, days: int = 2, duration: int = 60, **kw) -> str:
    """Create a real local-calendar event and return its uid."""
    from jarvis.integrations.calendar_provider import CalendarEvent, LocalCalendarProvider

    start = (datetime.now().astimezone() + timedelta(days=days)).replace(
        hour=14, minute=0, second=0, microsecond=0)
    event = CalendarEvent(uid="", title=title, start=start,
                          end=start + timedelta(minutes=duration), **kw)
    result = LocalCalendarProvider().create(event)
    assert result["ok"], result
    return result["uid"]


def _app_settings():
    """The settings instance the routes actually use.

    ``jarvis.api.app`` re-exports the FastAPI object, so the module has to be
    fetched from ``sys.modules``. This matters for more than tidiness:
    ``test_phase45`` reloads ``jarvis.config``, which leaves *two* ``get_settings``
    functions in play — the reloaded one and the reference the app bound at
    import. They can hold different ``home`` values, so a test that seeds through
    one and asserts through the other fails only in a full-suite run.
    """
    import sys

    return sys.modules["jarvis.api.app"].get_settings()


def _mail(subject: str, *, sender: str = "dana@example.com", body: str = "") -> None:
    """Write one real .eml into the inbox the app's own provider reads."""
    from jarvis.integrations.email_provider import LocalMailProvider

    inbox = LocalMailProvider(_app_settings()).folder("inbox")
    raw = (
        f"From: {sender}\r\n"
        f"To: me@example.com\r\n"
        f"Subject: {subject}\r\n"
        "Date: Thu, 22 Oct 2026 09:00:00 +0000\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "\r\n"
        f"{body or subject}\r\n"
    )
    (inbox / f"{abs(hash(subject))}-{subject[:20].replace(' ', '_')}.eml").write_text(
        raw, encoding="utf-8")


def test_it_prepares_the_next_meeting_and_reports_its_steps(client):
    _event("Quarterly planning", days=3, location="Room 4",
           description="Review the roadmap")

    data = client.post("/meetings/prepare", json={}).json()

    assert data["ok"] is True, data
    assert data["meeting"]["title"] == "Quarterly planning"
    assert data["workflow"], "the steps that ran must be reported"
    assert any("calendar" in s for s in data["workflow"]), data["workflow"]
    assert any("mailbox" in s for s in data["workflow"]), data["workflow"]
    assert "does not attend" in data["note"], data["note"]
    assert any(p.startswith("Location: Room 4") for p in data["talking_points"]), data
    assert any(p.startswith("Agenda/notes:") for p in data["talking_points"]), data


def test_it_finds_related_mail_by_topic_and_by_invited_address(client):
    _event("Vendor review", days=2, description="Talk to priya@example.com about terms")
    _mail("Notes before the Vendor review")

    # Prove the seed is visible to the provider this request will use, so a
    # failure below says whether the mail was missing or the search missed it.
    from jarvis.integrations.email_provider import get_email_provider

    provider = get_email_provider(_app_settings())
    direct = provider.search(query="Vendor review", limit=5)
    assert "Notes before the Vendor review" in [m.subject for m in direct], (
        f"seed not visible to {provider.name}: {list(direct)}")

    data = client.post("/meetings/prepare", json={"meeting": "vendor"}).json()

    assert data["ok"] is True, data
    subjects = [m["subject"] for m in data["related_email"]]
    assert "Notes before the Vendor review" in subjects, (
        f"provider={provider.name} email_error={data.get('email_error')!r} "
        f"workflow={data.get('workflow')}")
    assert any("related message" in p for p in data["talking_points"]), data


def test_open_tasks_that_name_the_meeting_are_surfaced(client):
    _event("Design sync", days=1)
    client.post("/planner/tasks", json={"title": "Prepare the Design sync deck"})

    data = client.post("/meetings/prepare", json={"meeting": "Design sync"}).json()

    assert data["ok"] is True, data
    assert any(t["title"] == "Prepare the Design sync deck"
               for t in data["related_tasks"]), data["related_tasks"]
    assert any("task" in p for p in data["talking_points"]), data


def test_an_ambiguous_name_asks_which_meeting_rather_than_picking_one(client):
    _event("Budget review", days=2)
    _event("Budget review", days=5)

    data = client.post("/meetings/prepare", json={"meeting": "Budget review"}).json()

    assert data["ok"] is False
    assert data["meeting"] is None
    assert len(data["candidates"]) == 2, data


def test_an_unknown_meeting_is_reported_not_invented(client):
    _event("Standup", days=1)

    data = client.post("/meetings/prepare", json={"meeting": "Board offsite"}).json()

    assert data["ok"] is False
    assert "no meeting matching" in data["reason"], data
    assert data["meeting"] is None


def test_nothing_upcoming_is_said_plainly(client):
    data = client.post("/meetings/prepare", json={}).json()
    assert data["ok"] is False
    assert "no upcoming meeting" in data["reason"], data


def test_an_unreadable_calendar_is_reported_not_rendered_as_empty(client, monkeypatch):
    from jarvis.integrations.calendar_provider import (
        ProviderResults, ProviderStatus, get_calendar_provider)

    def _down(settings=None):
        class _Down:
            name = "down"

            def list_events(self, start, end, limit=50):
                return ProviderResults(
                    [], ProviderStatus.NETWORK_ERROR, detail="host unreachable")
        return _Down()

    monkeypatch.setattr(
        "jarvis.integrations.calendar_provider.get_calendar_provider", _down)

    data = client.post("/meetings/prepare", json={}).json()

    assert data["ok"] is False
    assert data["reason"] == "calendar could not be read", data
    assert "calendar could not be read" in data["workflow"]


def test_the_briefing_offers_a_join_link_only_when_the_event_has_one(client):
    _event("Remote sync", days=2,
           description="Join at https://meet.example.com/abc-defg-hij")
    _event("In person sync", days=4, location="Room 1")

    with_link = client.post("/meetings/prepare", json={"meeting": "Remote sync"}).json()
    join = [a for a in with_link["prep_actions"] if a["action"] == "open_meeting"][0]
    assert join["params"]["join_url"] == "https://meet.example.com/abc-defg-hij"

    without = client.post("/meetings/prepare", json={"meeting": "In person"}).json()
    join = [a for a in without["prep_actions"] if a["action"] == "open_meeting"][0]
    assert join["params"]["join_url"] == ""


def test_an_overlapping_event_is_reported_as_a_conflict(client):
    from jarvis.integrations.calendar_provider import CalendarEvent, LocalCalendarProvider

    start = (datetime.now().astimezone() + timedelta(days=3)).replace(
        hour=10, minute=0, second=0, microsecond=0)
    provider = LocalCalendarProvider()
    provider.create(CalendarEvent(uid="", title="Main sync", start=start,
                                  end=start + timedelta(hours=1)))
    provider.create(CalendarEvent(uid="", title="Clashing call", start=start + timedelta(minutes=30),
                                  end=start + timedelta(minutes=90)))

    data = client.post("/meetings/prepare", json={"meeting": "Main sync"}).json()

    assert data["ok"] is True, data
    assert data["conflict"] is not None, data
    assert data["conflict"]["title"] == "Clashing call"


def test_preparing_changes_nothing(client):
    _event("Retro", days=2)
    client.post("/planner/tasks", json={"title": "Prepare Retro notes"})
    before_tasks = client.get("/planner/tasks").json()
    before_events = client.get("/calendar/events", params={"date": "next-14", "days": 14})
    data = client.post("/meetings/prepare", json={"meeting": "Retro"}).json()
    after_tasks = client.get("/planner/tasks").json()

    assert data["ok"] is True, data
    assert len(before_tasks) == len(after_tasks), "preparing must not create tasks"
    assert "Nothing was sent or changed" in data["note"], data["note"]