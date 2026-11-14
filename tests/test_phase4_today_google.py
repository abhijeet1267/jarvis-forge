"""Today / Phase 4 backend behaviour: overdue filtering, delete, verified reads.

These are *unit* tests with a substituted client or transport — no Google call
is made. The live operations are exercised separately and reported as such.

The rules under test are the ones that lie when they are wrong:

* a successful empty read is a verified fact, so the assistant must not append
  "I could not verify it happened" to its own correct answer;
* "overdue" is decided by the calendar day, because Google Tasks stores a date
  with no time;
* deleting a task is confirmed by reading the list back, not by trusting a 204;
* a delete that finds the task already gone is a success, not a failure that
  invites a retry which can only fail again.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from jarvis.integrations.calendar_provider import ProviderResults, ProviderStatus
from jarvis.integrations.google_tasks import GoogleTask, GoogleTasksClient
from jarvis.tools import google_tasks as tools
from jarvis.tools.base import ToolContext


class _FakeClient:
    """Stands in for GoogleTasksClient. Records what it was asked to do."""

    def __init__(self, tasks=None, status=ProviderStatus.HAS_RESULTS, detail="",
                 create=None, delete=None, remaining=None):
        self._tasks = tasks or []
        self._status = status
        self._detail = detail
        self._create = create
        self._delete = delete
        self._remaining = remaining
        self.deleted: list[tuple[str, str]] = []
        self.created: list[dict] = []

    def tasks(self, list_id="", show_completed=False, limit=100):
        return ProviderResults(list(self._tasks), self._status, detail=self._detail)

    def create(self, title, notes="", due=None, list_id="", source=""):
        self.created.append({"title": title, "notes": notes, "due": due,
                             "list_id": list_id, "source": source})
        return self._create or {"ok": False, "error": "not scripted"}

    def delete(self, task_id, list_id=""):
        self.deleted.append((task_id, list_id))
        return self._delete or {"ok": False, "error": "not scripted"}


def _task(task_id, title, due=None, done=False, list_id="@default"):
    return GoogleTask(id=task_id, title=title, status="completed" if done else "needsAction",
                      due=datetime.fromisoformat(due) if due else None, list_id=list_id)


def _install(monkeypatch, client):
    monkeypatch.setattr(tools, "_client", lambda settings: client)
    return client


# ---------------------------------------------------------------------------
# Overdue filtering
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_overdue_only_filters_by_calendar_day(monkeypatch):
    today = datetime.now().astimezone().date()
    yesterday = (today - timedelta(days=1)).isoformat()
    tomorrow = (today + timedelta(days=1)).isoformat()
    client = _install(monkeypatch, _FakeClient(tasks=[
        _task("1", "Late", yesterday),
        _task("2", "Today", today.isoformat()),
        _task("3", "Later", tomorrow),
        _task("4", "Undated", None),
    ]))
    result = await tools.list_google_tasks(ToolContext(), overdue_only=True)
    assert result.ok is True
    assert [t["id"] for t in result.output["overdue"]] == ["1"]
    assert "overdue" in result.message.lower()
    # The read happened, so its answer is a fact rather than an unknown.
    assert result.verified is True


@pytest.mark.asyncio
async def test_overdue_only_never_lists_a_task_due_today(monkeypatch):
    today = datetime.now().astimezone().date()
    _install(monkeypatch, _FakeClient(tasks=[_task("2", "Today", today.isoformat())]))
    result = await tools.list_google_tasks(ToolContext(), overdue_only=True)
    assert result.ok is True
    assert result.output["overdue"] == []
    assert "no google task is overdue" in result.message.lower()


@pytest.mark.asyncio
async def test_overdue_only_does_not_call_an_undated_task_overdue(monkeypatch):
    _install(monkeypatch, _FakeClient(tasks=[_task("4", "Someday", None)]))
    result = await tools.list_google_tasks(ToolContext(), overdue_only=True)
    assert result.output["overdue"] == []


@pytest.mark.asyncio
async def test_failed_read_is_a_failure_not_an_empty_overdue_list(monkeypatch):
    _install(monkeypatch, _FakeClient(
        tasks=[], status=ProviderStatus.AUTH_ERROR,
        detail="Google rejected the access token. Reconnect the account in Settings."))
    result = await tools.list_google_tasks(ToolContext(), overdue_only=True)
    assert result.ok is False
    assert "reconnect" in result.error.lower()
    # A failed read must never be reported as a verified empty answer.
    assert result.verified is False


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

def test_delete_reads_the_list_back_to_confirm(monkeypatch):
    client = GoogleTasksClient()
    calls: list[tuple[str, str]] = []

    def fake_send(method, url, token, payload, params=None):
        calls.append((method, url))
        return {}

    monkeypatch.setattr(client, "auth", type("A", (), {
        "access_token": lambda self: "token",
        "configured": True,
        "stored_refresh_token": lambda self: "rt",
    })())
    monkeypatch.setattr(client, "_send", fake_send)
    # The task is gone from the list afterwards.
    monkeypatch.setattr(client, "tasks",
                        lambda **kw: ProviderResults([], ProviderStatus.REAL_EMPTY))
    result = client.delete("task-1")
    assert result["ok"] is True
    assert result["verified"] is True
    assert calls[0][0] == "DELETE"
    assert calls[0][1].endswith("/tasks/task-1")


def test_delete_refuses_to_claim_success_when_the_task_is_still_there(monkeypatch):
    client = GoogleTasksClient()
    monkeypatch.setattr(client, "auth", type("A", (), {
        "access_token": lambda self: "token",
        "configured": True,
        "stored_refresh_token": lambda self: "rt",
    })())
    monkeypatch.setattr(client, "_send", lambda *a, **k: {})
    # Google said 204, but the task is still listed.
    monkeypatch.setattr(client, "tasks", lambda **kw: ProviderResults(
        [_task("task-1", "Still here")], ProviderStatus.HAS_RESULTS))
    result = client.delete("task-1")
    assert result["ok"] is False
    assert result["status"] == "unverified"


def test_delete_of_an_absent_task_is_success(monkeypatch):
    from jarvis.integrations.google_tasks import _TasksFailure

    client = GoogleTasksClient()
    monkeypatch.setattr(client, "auth", type("A", (), {
        "access_token": lambda self: "token",
        "configured": True,
        "stored_refresh_token": lambda self: "rt",
    })())

    def boom(*a, **k):
        raise _TasksFailure(ProviderStatus.PROVIDER_ERROR,
                            "That task or list does not exist on this Google account.")

    monkeypatch.setattr(client, "_send", boom)
    result = client.delete("gone")
    assert result["ok"] is True, "already gone is the end state the caller wanted"
    assert result.get("already_absent") is True


def test_delete_requires_an_id():
    assert GoogleTasksClient().delete("")["ok"] is False


# ---------------------------------------------------------------------------
# Verified reads feed the assistant's honesty
# ---------------------------------------------------------------------------

def test_a_successful_empty_calendar_read_is_verified():
    """The assistant appends "I could not verify it happened" to UNKNOWN only.

    An empty read from a provider that answered is a fact, so it must carry
    SUCCESS — otherwise a correct "nothing scheduled today" is delivered with a
    caveat that reads as a failure.
    """
    from jarvis.tools.base import Verification
    from jarvis.tools.calendar import daily_schedule

    class _Provider:
        name = "google"

        def list_events(self, *a, **k):
            return ProviderResults([], ProviderStatus.REAL_EMPTY)

    import jarvis.tools.calendar as cal_tools
    original = cal_tools.get_calendar_provider
    cal_tools.get_calendar_provider = lambda settings=None: _Provider()
    try:
        result = __import__("asyncio").run(daily_schedule(ToolContext()))
    finally:
        cal_tools.get_calendar_provider = original
    assert result.ok is True
    assert result.verification is Verification.SUCCESS
    assert result.verified is True