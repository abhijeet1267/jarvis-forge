"""Scheduler: edit a job in place and read its run history."""

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
    db.execute("DELETE FROM scheduled_jobs")
    db.execute("DELETE FROM schedule_runs")
    yield


def _create(client, **kw):
    body = {"action": "reminder", "schedule": "every morning at 8",
            "params": {"message": "stretch"}}
    body.update(kw)
    r = client.post("/schedules", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_a_job_can_be_renamed_and_rescheduled(client):
    job = _create(client)
    edited = client.patch(f"/schedules/{job['schedule_id']}",
                          json={"name": "Morning stretch", "schedule": "every morning at 7"})
    assert edited.status_code == 200, edited.text
    out = edited.json()
    assert out["name"] == "Morning stretch"
    assert "07:00" in out["schedule"], out["schedule"]
    # Same identity — it is an edit, not a replacement.
    assert out["schedule_id"] == job["schedule_id"]
    # And the list shows the new values.
    listed = [j for j in client.get("/schedules").json()["schedules"]
              if j["schedule_id"] == job["schedule_id"]][0]
    assert listed["name"] == "Morning stretch"


def test_an_edit_recomputes_next_run_from_now(client):
    job = _create(client)
    before = job["next_run"]
    edited = client.patch(f"/schedules/{job['schedule_id']}",
                          json={"schedule": "every evening at 6"}).json()
    assert edited["next_run"] != before, "next_run must be recomputed, not preserved"


def test_an_unparseable_schedule_is_refused(client):
    job = _create(client)
    r = client.patch(f"/schedules/{job['schedule_id']}", json={"schedule": "whenever, really"})
    assert r.status_code == 400
    assert "could not understand" in r.json()["detail"].lower()


def test_an_edit_cannot_smuggle_a_risky_action(client):
    job = _create(client)
    r = client.patch(f"/schedules/{job['schedule_id']}", json={"action": "send_email"})
    assert r.status_code == 400, r.text
    assert "too risky" in r.json()["detail"].lower()
    # The job is unchanged.
    assert client.get(f"/schedules/{job['schedule_id']}").json()["schedule"]["action"] == "reminder"


def test_history_is_returned_per_job(client):
    job = _create(client)
    detail = client.get(f"/schedules/{job['schedule_id']}").json()
    assert "history" in detail and isinstance(detail["history"], list)
    # Running it records a run in that history.
    client.post(f"/schedules/{job['schedule_id']}/run")
    detail = client.get(f"/schedules/{job['schedule_id']}").json()
    assert detail["history"], "a run was not recorded in history"
    assert detail["history"][0].get("outcome"), detail["history"][0]


def test_editing_a_missing_job_is_404(client):
    assert client.patch("/schedules/SCHED-nope", json={"name": "x"}).status_code == 404


# ---------------------------------------------------------------------------
# A bare hour is ambiguous, and the scheduler must ask rather than guess
# ---------------------------------------------------------------------------

def test_a_bare_hour_asks_instead_of_guessing():
    """"every day at 7" is a morning alarm as often as an evening one.

    Measured before the fix: it became 19:00 silently, and "every morning at 7"
    became 08:00 (the day-part *default* overriding the hour the person said).
    """
    from jarvis.scheduler.schedule import parse_schedule

    assert parse_schedule("every day at 7") is None, "a bare hour must not be guessed"
    assert parse_schedule("every day at 6") is None
    # An explicit clock or a day part resolves it.
    assert "07:00" in parse_schedule("every morning at 7").describe()
    assert "19:00" in parse_schedule("every evening at 7").describe()
    assert "07:00" in parse_schedule("every day at 7am").describe()
    assert "19:00" in parse_schedule("every day at 7 pm").describe()
    # Explicit HH:MM is never ambiguous.
    assert "10:00" in parse_schedule("every day at 10:00").describe()
    assert "06:00" in parse_schedule("every day at 06:00").describe()


def test_the_error_names_what_it_refused(client):
    r = client.post("/schedules", json={"action": "reminder", "schedule": "every day at 7"})
    assert r.status_code == 400
    assert "could not understand" in r.json()["detail"].lower()
    assert "every day at 7" in r.json()["detail"]
