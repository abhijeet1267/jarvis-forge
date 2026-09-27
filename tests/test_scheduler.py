"""Scheduler + daily briefing tests (v0.8).

The property that matters most here is the plan's hard rule: **a scheduled task
must never silently escalate its permissions.** Most of this file exists to
prove that from several angles.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from jarvis.core.models import PlannedAction, PolicyDecision, RiskLevel
from jarvis.core.briefing import BUILDERS, DEFAULT_SECTIONS, build_briefing
from jarvis.scheduler import (
    ACTIVE,
    CANCELLED,
    MAX_SCHEDULABLE_RISK,
    PAUSED,
    Recurrence,
    ScheduleStore,
    SchedulerRunner,
    parse_schedule,
)


@pytest.fixture()
def store() -> ScheduleStore:
    return ScheduleStore()


# ---------------------------------------------------------------------------
# Schedule parsing
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text,kind",
    [
        ("every morning", "daily"),
        ("every day", "daily"),
        ("every evening at 7", "daily"),
        ("every Monday", "weekly"),
        ("every Friday at 5 PM", "weekly"),
        ("every 30 minutes", "interval"),
        ("every 2 hours", "interval"),
        ("at 8 PM", "once"),
    ],
)
def test_schedule_phrases_parse(text: str, kind: str) -> None:
    recurrence = parse_schedule(text)
    assert recurrence is not None, text
    assert recurrence.kind == kind, f"{text} -> {recurrence.kind}"


@pytest.mark.parametrize("text", ["", "sometime later", "whenever", "banana"])
def test_unparseable_schedules_return_none(text: str) -> None:
    """Never invent a time -- return None so the caller can ask."""
    assert parse_schedule(text) is None


def test_named_times_of_day() -> None:
    assert parse_schedule("every morning").hour == 8
    assert parse_schedule("every evening").hour == 19
    assert parse_schedule("every night").hour == 21


def test_evening_hour_is_24h() -> None:
    assert parse_schedule("every day at 5 PM").hour == 17
    assert parse_schedule("every day at 7 PM").hour == 19
    assert parse_schedule("every day at 9 AM").hour == 9


def test_weekly_lands_on_the_named_day() -> None:
    recurrence = parse_schedule("every Monday at 9 AM")
    assert recurrence.weekday == 0 and recurrence.hour == 9

    now = datetime(2026, 9, 20, 12, 0).astimezone()  # a Sunday
    nxt = recurrence.next_after(now)
    assert nxt.weekday() == 0
    assert nxt > now


def test_once_in_the_past_moves_to_tomorrow() -> None:
    now = datetime(2026, 9, 20, 22, 0).astimezone()
    recurrence = parse_schedule("at 8 PM", now=now)
    assert recurrence.kind == "once"
    assert recurrence.at > now
    assert recurrence.at.day == 21


def test_daily_next_after_is_strictly_future() -> None:
    recurrence = parse_schedule("every morning")
    now = datetime(2026, 9, 20, 12, 0).astimezone()
    nxt = recurrence.next_after(now)
    assert nxt > now
    assert (nxt.hour, nxt.minute) == (8, 0)


# ---------------------------------------------------------------------------
# RRULE round-trip
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    ["every morning", "every Monday at 9 AM", "every 30 minutes", "every evening at 7"],
)
def test_rrule_round_trip(text: str) -> None:
    original = parse_schedule(text)
    restored = Recurrence.from_rrule(original.to_rrule())
    assert restored is not None
    assert restored.kind == original.kind
    assert (restored.hour, restored.minute) == (original.hour, original.minute)
    assert restored.weekday == original.weekday
    assert restored.interval_minutes == original.interval_minutes


def test_describe_is_human_readable() -> None:
    assert "Monday" in parse_schedule("every Monday").describe()
    assert "08:00" in parse_schedule("every morning").describe()
    assert "minutes" in parse_schedule("every 30 minutes").describe()


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------
def test_create_and_list(store: ScheduleStore) -> None:
    job = store.create("Test reminder", "every morning", "reminder", {"message": "stand up"})
    assert job.status == ACTIVE
    assert job.next_run is not None
    assert any(j.schedule_id == job.schedule_id for j in store.list())
    store.cancel(job.schedule_id)


def test_create_rejects_a_bad_schedule(store: ScheduleStore) -> None:
    with pytest.raises(ValueError, match="could not understand"):
        store.create("Bad", "whenever-ish", "reminder")


def test_pause_resume_and_cancel(store: ScheduleStore) -> None:
    job = store.create("Lifecycle", "every day at 10:00", "reminder")

    assert store.pause(job.schedule_id)
    assert store.get(job.schedule_id).status == PAUSED

    assert store.resume(job.schedule_id)
    assert store.get(job.schedule_id).status == ACTIVE

    assert store.cancel(job.schedule_id)
    assert store.get(job.schedule_id).status == CANCELLED


def test_paused_jobs_are_not_due(store: ScheduleStore) -> None:
    job = store.create("Not due", "every 1 minutes", "reminder")
    store.pause(job.schedule_id)
    assert not any(j.schedule_id == job.schedule_id for j in store.due())
    store.cancel(job.schedule_id)


def test_due_selects_only_arrived_jobs(store: ScheduleStore) -> None:
    job = store.create("Due soon", "every 1 minutes", "reminder")
    now = datetime.now().astimezone()

    # Its next run is a minute away, so it is not due *now*.
    assert not any(j.schedule_id == job.schedule_id for j in store.due(now))

    # A minute later it is.
    assert any(
        j.schedule_id == job.schedule_id for j in store.due(now + timedelta(minutes=2))
    )
    store.cancel(job.schedule_id)


def test_a_daily_job_is_not_due_immediately(store: ScheduleStore) -> None:
    """A job scheduled for tomorrow morning must not fire on creation."""
    job = store.create("Tomorrow", "every day at 06:00", "reminder")
    assert not any(j.schedule_id == job.schedule_id for j in store.due())
    store.cancel(job.schedule_id)


def test_record_run_advances_and_logs(store: ScheduleStore) -> None:
    job = store.create("Recorded", "every 1 minutes", "reminder")
    before = job.next_run

    store.record_run(job, "executed", "all good")
    updated = store.get(job.schedule_id)

    assert updated.run_count == 1
    assert updated.failure_count == 0
    assert updated.last_result == "all good"
    assert updated.next_run != before
    assert store.history(job.schedule_id)
    store.cancel(job.schedule_id)


def test_a_one_shot_job_retires_after_firing(store: ScheduleStore) -> None:
    job = store.create("One shot", "at 6 AM", "reminder")
    store.record_run(job, "executed", "done")
    assert store.get(job.schedule_id).status == CANCELLED


def test_failures_are_counted(store: ScheduleStore) -> None:
    job = store.create("Failing", "every 1 minutes", "reminder")
    store.record_run(job, "failed", "it broke")
    updated = store.get(job.schedule_id)
    assert updated.failure_count == 1
    assert updated.last_error == "it broke"
    store.cancel(job.schedule_id)


# ---------------------------------------------------------------------------
# The no-escalation rule
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_job_cannot_exceed_its_risk_ceiling(core, store: ScheduleStore) -> None:
    """A schedule must not become a way to gain authority later.

    The job is created with a READ ceiling but points at a destructive action;
    the runner must refuse rather than quietly execute it.
    """
    job = store.create(
        "Sneaky",
        "every 1 minutes",
        "delete_file",
        {"path": "/tmp/whatever"},
        max_risk=int(RiskLevel.READ),
    )

    outcome = await SchedulerRunner(store).run_job(core, job)
    assert outcome["outcome"] == "blocked"
    assert "cannot raise its own authority" in outcome["detail"]
    store.cancel(job.schedule_id)


@pytest.mark.asyncio
async def test_a_confirm_verdict_is_parked_not_approved(core, store: ScheduleStore) -> None:
    """The runner must never approve on the user's behalf."""
    job = store.create(
        "Needs approval",
        "every 1 minutes",
        "copy_file",
        {"source": "/tmp/a", "destination": "/tmp/b"},
        max_risk=int(RiskLevel.EXTERNAL),
    )

    outcome = await SchedulerRunner(store).run_job(core, job)

    # copy_file requires confirmation, so it must not have executed.
    assert outcome["outcome"] in ("awaiting_approval", "failed")
    if outcome["outcome"] == "awaiting_approval":
        assert outcome.get("confirmation_id")
    store.cancel(job.schedule_id)


@pytest.mark.asyncio
async def test_a_unknown_action_fails_cleanly(core, store: ScheduleStore) -> None:
    job = store.create("Ghost", "every 1 minutes", "not_a_real_tool")
    outcome = await SchedulerRunner(store).run_job(core, job)
    assert outcome["outcome"] == "failed"
    store.cancel(job.schedule_id)


@pytest.mark.asyncio
async def test_a_read_only_job_executes(core, store: ScheduleStore) -> None:
    job = store.create("Time check", "every 1 minutes", "get_time", {}, max_risk=int(RiskLevel.READ))
    outcome = await SchedulerRunner(store).run_job(core, job)
    assert outcome["outcome"] == "executed"
    store.cancel(job.schedule_id)


@pytest.mark.asyncio
async def test_a_builtin_briefing_job_runs(core, store: ScheduleStore) -> None:
    job = store.create("Morning", "every morning", "briefing", {"sections": ["datetime"]})
    outcome = await SchedulerRunner(store).run_job(core, job)
    assert outcome["outcome"] == "executed"
    store.cancel(job.schedule_id)


def test_destructive_actions_cannot_be_scheduled(core) -> None:
    """The ceiling for schedulable work excludes destructive and physical tools."""
    assert int(MAX_SCHEDULABLE_RISK) < int(RiskLevel.DESTRUCTIVE)
    for name in ("delete_file", "send_email", "cancel_event", "remove_phone"):
        spec = core.tools.get(name)
        assert int(spec.risk) > int(MAX_SCHEDULABLE_RISK), name


# ---------------------------------------------------------------------------
# Briefing
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_briefing_renders_all_default_sections(core) -> None:
    briefing = await build_briefing(core)
    assert "JARVIS DAILY BRIEFING" in briefing["text"]
    assert briefing["unknown"] == []
    assert "datetime" in briefing["sections"]


@pytest.mark.asyncio
async def test_briefing_respects_the_requested_subset(core) -> None:
    briefing = await build_briefing(core, sections=["datetime"])
    assert list(briefing["sections"].keys()) == ["datetime"]


@pytest.mark.asyncio
async def test_briefing_reports_unknown_sections(core) -> None:
    """An unknown section must be reported, not silently dropped."""
    briefing = await build_briefing(core, sections=["datetime", "telepathy"])
    assert "telepathy" in briefing["unknown"]
    assert "telepathy" not in briefing["sections"]


@pytest.mark.asyncio
async def test_briefing_survives_a_broken_section(core, monkeypatch) -> None:
    """One failing section must not take the whole briefing down."""

    async def explode(_core):
        raise RuntimeError("section exploded")

    monkeypatch.setitem(BUILDERS, "datetime", explode)
    briefing = await build_briefing(core, sections=["datetime"])
    assert "unavailable" in briefing["sections"]["datetime"][0]


def test_briefing_sections_are_known() -> None:
    for name in DEFAULT_SECTIONS:
        assert name in BUILDERS, name


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------
def test_creating_a_schedule_requires_confirmation(core) -> None:
    action = PlannedAction(
        tool="schedule_task",
        params={"schedule": "every morning", "action": "briefing"},
        device="laptop_main",
    )
    assert core.policy.evaluate(action).decision is PolicyDecision.CONFIRM


def test_cancelling_a_schedule_requires_confirmation(core) -> None:
    action = PlannedAction(
        tool="cancel_schedule", params={"schedule_id": "SCHED-x"}, device="laptop_main"
    )
    assert core.policy.evaluate(action).decision is PolicyDecision.CONFIRM


@pytest.mark.parametrize(
    "tool,params",
    [("list_schedules", {}), ("daily_briefing", {}), ("pause_schedule", {"schedule_id": "S-1"})],
)
def test_scheduler_reads_and_pauses_are_not_gated(core, tool: str, params: dict) -> None:
    action = PlannedAction(tool=tool, params=params, device="laptop_main")
    assert core.policy.evaluate(action).decision is PolicyDecision.EXECUTE


def test_scheduler_agent_is_registered(core) -> None:
    assert "scheduler" in core.agents
    assert core.agents["scheduler"].owns("schedule_task")
