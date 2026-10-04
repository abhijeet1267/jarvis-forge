"""Phase 26: recovery and risk trust, measured rather than assumed.

Four findings survived nine phases of being carried forward. All four were
re-measured on this tree before anything below was written, and each turned out
to be worse than its one-line summary:

* **R-2** -- the retry guard asked "is this destructive?" and stopped at risk 3,
  so the answer to "is it safe to run twice?" was never computed for anything
  below it. Measured: **45 registered tools** were re-executed up to twice extra
  purely because the failure *text* matched a regex -- ``move_file``,
  ``copy_file``, ``write_file``, ``create_event``, ``schedule_task``,
  ``send_to_device``, ``control_home_device``, ``remember_fact`` among them. The
  text that triggers it most reliably, ``command timed out after 30.0s``, is
  exactly the case where the effect is unknown.
* **RT-3** -- ``ToolSpec`` was a mutable pydantic model, and
  ``PermissionEngine.evaluate()`` re-reads the registry's *same object*. Setting
  ``spec.risk = RiskLevel.READ`` on ``delete_file`` moved the gate from
  ``CONFIRM (level 3 (destructive) action requires confirmation)`` to
  ``EXECUTE (low-risk action)``, flipped ``is_retryable`` to True, and rewrote
  the line the planner is shown to ``risk=read confirm=False``.
* **R-11** -- ``POST /tasks/{id}/retry`` returned a job set to ``running`` with
  its failed steps reset to ``pending`` and their ``detail`` erased, and
  executed nothing (measured 0 executions). Because the briefing counts
  ``running`` jobs, the false state reached the user as "1 job(s) in progress."
  ``resume()`` did the same thing.
* **R-13** -- ``record_failure`` swallowed every exception at ``log.info``, so a
  failure could be recorded nowhere with no signal, while ``failure_stats()``
  and ``recent_failures()`` raised straight through ``/api/metrics``,
  ``/api/failures`` and the briefing. Writes lost data silently; reads broke the
  API loudly.

R-12 (the confirmed-action path bypasses recovery entirely) is fixed at its
truthful half: that path now *records* its failure. It does not gain an
automatic retry -- one approval buys one attempt, and re-running a destructive
action on a guess is the thing R-2 exists to prevent.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jarvis.api.app import app
from jarvis.config import get_settings
from jarvis.core.models import (
    ActionResult,
    ActionStatus,
    CommandRequest,
    PlannedAction,
    PolicyDecision,
    RiskLevel,
)
from jarvis.core.recovery import IDEMPOTENT_TOOLS, RecoveryEngine
from jarvis.core.tasks import LifecycleReport
from jarvis.tools.base import unverified

PAGE = Path(__file__).resolve().parent.parent / "dashboard" / "index.html"
AUTH_HEADERS = {"X-JARVIS-Token": __import__("os").environ["JARVIS_API_TOKEN"]}
node = shutil.which("node")

#: Transient by the engine's own regex -- and each one writes to the world.
WRITING_TOOLS = [
    "move_file", "copy_file", "write_file", "create_event", "update_event",
    "schedule_task", "run_schedule_now", "send_to_device", "apply_code_change",
    "control_home_device", "run_home_routine", "remember_fact", "compress_path",
    "vibrate_watch", "register_phone",
]
TRANSIENT = "command timed out after 30.0s"


def failed(tool: str, msg: str) -> ActionResult:
    return ActionResult(ok=False, tool=tool, error=msg, message=msg, verified=False)


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, base_url="http://127.0.0.1:8765", headers=AUTH_HEADERS)


# ---------------------------------------------------------------------------
# 26-A / R-2 -- re-run safety, not "is it destructive"
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool", WRITING_TOOLS)
def test_a_transient_error_no_longer_buys_a_retry_for_a_tool_that_writes(core, tool):
    spec = core.tools.get(tool)
    assert spec is not None and spec.risk < RiskLevel.DESTRUCTIVE, spec
    assert core.recovery.is_retryable(spec, failed(tool, TRANSIENT)) is False, (
        f"{tool} was still re-executed on the strength of error text alone")


def test_no_registered_tool_is_retried_only_because_the_error_reads_transient(core):
    """The sweep that produced the number in the docstring: 45 -> 0.

    Anything that may be retried must be re-run-safe by its own declaration
    (read tier, or listed in ``IDEMPOTENT_TOOLS``), never by what the failure
    happened to say.
    """
    offenders = []
    for spec in core.tools.all():
        if core.recovery.is_retryable(spec, failed(spec.name, TRANSIENT)):
            if spec.risk is not RiskLevel.READ and spec.name not in IDEMPOTENT_TOOLS:
                offenders.append(f"{spec.name} ({spec.risk_label()})")
    assert offenders == [], f"re-run of these is not known to be safe: {offenders}"


@pytest.mark.parametrize("tool", ["get_time", "search_files", "read_file", "system_status"])
def test_read_only_work_is_still_retried_whatever_the_error_says(core, tool):
    """The fix must not have become a blanket 'never retry'."""
    spec = core.tools.get(tool)
    assert spec.risk is RiskLevel.READ or tool in IDEMPOTENT_TOOLS
    assert core.recovery.is_retryable(spec, failed(tool, "permission denied"))


def test_the_escalation_note_gives_the_real_reason(core):
    """Saying "the error is not transient" when it plainly was is a second lie."""
    engine = RecoveryEngine(max_retries=2, base_delay=0.0)
    calls = {"n": 0}

    async def execute(action, ctx):
        calls["n"] += 1
        return failed(action.tool, TRANSIENT)

    outcome = asyncio.run(engine.recover(
        PlannedAction(tool="move_file", params={"src": "/tmp/a", "dst": "/tmp/b"}),
        None, execute, core.tools.get("move_file"), failed("move_file", TRANSIENT)))

    assert calls["n"] == 0, "the action was re-executed anyway"
    assert outcome.recovered is False
    assert "not transient" not in outcome.note, outcome.note
    assert "twice" in outcome.note, outcome.note


def test_a_retry_that_ran_is_not_reported_as_never_having_happened(core):
    """Phase 9 R-12 (final-report numbering): notes said "not retried" after a
    retry had run. With the reason derived from the same decision that gated the
    retry, the note can no longer describe a different policy."""
    engine = RecoveryEngine(max_retries=3, base_delay=0.0)
    calls = {"n": 0}

    async def execute(action, ctx):
        calls["n"] += 1
        return failed(action.tool, "permission denied")

    outcome = asyncio.run(engine.recover(
        PlannedAction(tool="get_time", params={}), None, execute,
        core.tools.get("get_time"), failed("get_time", "permission denied")))

    assert calls["n"] >= 1, outcome.tried
    assert "not retried" not in outcome.note, outcome.note
    assert "retried 1 time" in outcome.note, outcome.note


# ---------------------------------------------------------------------------
# 26-B / RT-3 -- the tier the gate trusts cannot be rewritten under it
# ---------------------------------------------------------------------------

IMMUTABLE_FIELDS = [
    "name", "agent", "risk", "requires_confirmation", "reversible",
    "allowed_devices", "required_parameters", "tags",
]


@pytest.mark.parametrize("field", IMMUTABLE_FIELDS)
def test_a_tool_cannot_edit_the_fields_the_gates_read(core, field):
    spec = core.tools.get("delete_file")
    before = getattr(spec, field)
    with pytest.raises(Exception):
        setattr(spec, field, "laptop_main" if field.endswith("devices") else
                RiskLevel.READ if field == "risk" else [])
    assert getattr(spec, field) == before, f"{field} changed under the gate"


def test_the_permission_gate_still_asks_after_a_failed_elevation_attempt(core):
    """The measured bypass, pinned from both ends: the write is refused *and*
    the verdict is unchanged."""
    spec = core.tools.get("delete_file")
    action = PlannedAction(step_id=1, tool="delete_file",
                           params={"path": "/tmp/phase26-x"}, device="laptop_main")
    verdict = core.policy.evaluate(action)
    assert verdict.decision is PolicyDecision.CONFIRM, verdict.reason

    with pytest.raises(Exception):
        spec.risk = RiskLevel.READ
    with pytest.raises(Exception):
        spec.requires_confirmation = False

    again = core.policy.evaluate(action)
    assert again.decision is PolicyDecision.CONFIRM, (
        f"the gate moved to {again.decision} ({again.reason}) without any "
        "approved change -- RT-3 was a confirmation bypass")
    assert core.recovery.is_retryable(spec, failed("delete_file", TRANSIENT)) is False


def test_a_safety_tag_cannot_be_dropped_to_escape_the_retry_guard(core):
    """``is_retryable`` reads ``spec.tags``; a list there would be one
    ``remove()`` away from auto-retrying an emergency stop."""
    spec = core.tools.get("emergency_stop")
    assert "safety" in spec.tags
    assert isinstance(spec.tags, tuple)
    with pytest.raises(AttributeError):
        spec.tags.remove("safety")
    assert core.recovery.is_retryable(spec, failed("emergency_stop", TRANSIENT)) is False


def test_the_ledger_row_for_a_step_names_the_tool_that_answered_it(core):
    """The written half of R-14: the step row is keyed on the planned tool, so
    without ``answered_by`` it reads as though that handler had produced the
    fallback's output."""
    action = PlannedAction(step_id=1, tool="search_files", params={"query": "x"},
                           device="laptop_main")
    request = CommandRequest(text="find x", source="phase26")
    result = ActionResult(ok=True, tool="list_directory", message="listed 41 entries",
                          verified=True)
    core._audit_action(action, request, result, "read", False,
                       request_id="REQ-probe", answered_by="list_directory")
    row = next(r for r in core.audit.recent(5) if r.get("request_id") == "REQ-probe")
    assert row["tool"] == "search_files", row
    assert (row.get("detail") or {}).get("answered_by") == "list_directory", row


def test_the_registry_still_serialises_for_the_api(client):
    """Frozen does not mean unreadable: /tools is what an operator checks."""
    row = next(t for t in client.get("/tools").json() if t["name"] == "delete_file")
    assert row["risk"] == int(RiskLevel.DESTRUCTIVE)
    assert row["requires_confirmation"] is True


# ---------------------------------------------------------------------------
# 26-C / R-11 -- a lifecycle verb may not fabricate progress
# ---------------------------------------------------------------------------


def _job(core, steps=("get_time", "get_battery_status"), last_status=ActionStatus.FAILED,
         detail="connection refused by smtp.example"):
    job = core.tasks.create("phase26 lifecycle", list(steps))
    seqs = {s["name"]: s["seq"] for s in core.tasks.get(job.job_id)["steps"]}
    for name in steps[:-1]:
        core.tasks.update_step(job.job_id, name, ActionStatus.SUCCEEDED, "done",
                               seq=seqs[name])
    core.tasks.update_step(job.job_id, steps[-1], last_status, detail,
                           seq=seqs[steps[-1]])
    core.tasks.refresh_status(job.job_id)
    return job.job_id


def test_retry_records_the_request_and_runs_nothing(core):
    job_id = _job(core)
    calls = {"n": 0}
    original = core.tools._handlers["get_time"]

    async def counting(ctx, **kw):
        calls["n"] += 1
        return ActionResult(ok=True, tool="get_time", message="ran", verified=True)

    core.tools._handlers["get_time"] = counting
    try:
        report = core.tasks.retry(job_id)
    finally:
        core.tools._handlers["get_time"] = original

    assert isinstance(report, LifecycleReport)
    assert report.accepted is True and bool(report) is True
    assert report.executed == 0
    assert calls["n"] == 0, "a retry re-executed something"


def test_retry_does_not_erase_why_the_step_failed(core):
    """``SET detail = ''`` deleted the only record of the failure."""
    job_id = _job(core)
    core.tasks.retry(job_id)
    steps = core.tasks.get(job_id)["steps"]
    failed_step = [s for s in steps if s["name"] == "get_battery_status"][0]
    assert "connection refused by smtp.example" in failed_step["detail"], failed_step
    assert "re-attempt requested" in failed_step["detail"], failed_step


def test_retry_does_not_claim_the_job_is_running(core):
    job_id = _job(core)
    report = core.tasks.retry(job_id)
    job = core.tasks.get(job_id)
    assert report.status != "running", "nothing was executing this job"
    assert job["status"] == "failed", job["status"]
    assert [s["status"] for s in job["steps"]] == ["succeeded", "failed"], job["steps"]


def test_the_briefing_does_not_report_a_retried_job_as_in_progress(core, monkeypatch):
    """Where the fabricated state actually reached a human."""
    from jarvis.core import briefing

    job_id = _job(core)
    core.tasks.retry(job_id)
    row = core.tasks.get(job_id)
    monkeypatch.setattr(core.tasks, "list",
                        lambda limit=25: [{**row, "job_id": job_id}])
    lines = asyncio.run(briefing._section_tasks(core))
    assert not any("in progress" in line for line in lines), lines


def test_resume_reports_where_the_steps_really_are(core):
    job_id = _job(core, steps=("get_time", "draft_email"),
                  last_status=ActionStatus.AWAITING_CONFIRMATION, detail="waiting")
    core.tasks.pause(job_id)
    assert core.tasks.get(job_id)["status"] == "paused"
    report = core.tasks.resume(job_id)
    assert report.executed == 0
    assert report.status != "running", (
        "resume() set a job nobody would re-dispatch to 'running'")
    assert report.status == "awaiting_confirmation", report.status


def test_a_lifecycle_verb_says_what_the_caller_has_to_do_next(core):
    job_id = _job(core)
    report = core.tasks.retry(job_id)
    assert report.message, "the response carries no way forward"
    assert "nothing ran" in report.message.lower() or "ran nothing" in report.message.lower(), (
        report.message)


def test_a_retry_that_cannot_run_reports_zero_and_no_fake_state(client, core):
    """Phase 26's claim, kept true after Phase 27 gave the verb a dispatcher.

    A retry *can* run things now, so this test pins the half that must never
    come back: when the re-issued step needs an approval nobody has given, the
    answer is ``executed: 0`` and the job's real state -- never a ``running``
    job invented by the verb itself.
    """
    victim = "/tmp/phase26-gated-retry.txt"
    job = core.tasks.create(
        "delete the file", ["delete_file"],
        planned=[PlannedAction(step_id=1, tool="delete_file",
                               params={"path": victim}, device="laptop_main")])
    seq = core.tasks.get(job.job_id)["steps"][0]["seq"]
    core.tasks.update_step(job.job_id, "delete_file", ActionStatus.FAILED,
                           "device did not answer", seq=seq)
    core.tasks.refresh_status(job.job_id)

    body = client.post(f"/tasks/{job.job_id}/retry").json()
    assert body["executed"] == 0, body
    assert body["job"]["status"] != "running", body["job"]["status"]
    assert body["confirmation_id"], "a gated re-run must offer a fresh approval"
    assert "approval" in body["note"].lower(), body["note"]


def test_an_unknown_job_is_still_a_404(client):
    assert client.post("/tasks/JOB-does-not-exist/retry").status_code == 404


@pytest.fixture(scope="module")
def page_code(tmp_path_factory):
    """The state table actually shipped in dashboard/index.html."""
    if not node:
        pytest.skip("node not available to run the page's own code")
    body = re.search(r"<script>(.*?)</script>", PAGE.read_text(), re.S).group(1)
    extracted = (body[body.index("const STATE_TONE"):body.index("function applyHealth")]
                 .replace("const STATE_TONE", "var STATE_TONE", 1)
                 .replace("const STEP_STATE", "var STEP_STATE", 1)
                 .replace("const JOB_STATE", "var JOB_STATE", 1))

    root = tmp_path_factory.mktemp("p26")
    lib = root / "page.js"

    def _run(expr):
        lib.write_text("var get = async () => { throw new Error('no fetch in test'); };\n"
                       "var escapeHtml = v => String(v ?? '');\n"
                       + "eval(" + json.dumps(extracted) + ");\n"
                       + "console.log(JSON.stringify(" + expr + "));\n")
        proc = subprocess.run([node, str(lib)], capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stderr[:400]
        return json.loads(proc.stdout.strip())

    return _run


@pytest.mark.parametrize("status", ["paused", "cancelled", "incomplete", "failed"])
def test_the_page_has_a_word_for_every_state_a_lifecycle_verb_can_leave(page_code, status):
    """The verbs now leave real states instead of 'running'; the page must name
    them rather than rendering UNKNOWN (state)."""
    label = page_code(f"JOB_STATE[{json.dumps(status)}]")
    assert label and "UNKNOWN" not in label, f"the page cannot name a {status!r} job"


# ---------------------------------------------------------------------------
# 26-D / R-13 -- the failure ledger cannot lose a record in silence
# ---------------------------------------------------------------------------


def _reject_failure_writes(monkeypatch, engine, message="no such table: failures"):
    """Break only the ledger's INSERT, leaving the rest of the DB alone."""
    real = engine.db.execute

    def spy(sql, params=()):
        if "INSERT INTO failures" in sql:
            raise sqlite3.OperationalError(message)
        return real(sql, params)

    monkeypatch.setattr(engine.db, "execute", spy)


def _reject_failure_reads(monkeypatch, engine, message="no such table: failures"):
    """Break only reads of the failures table -- the shared Database singleton
    also serves the audit log, and a blanket patch would test the wrong thing."""
    real = engine.db.query

    def spy(sql, params=()):
        if "FROM failures" in sql:
            raise sqlite3.OperationalError(message)
        return real(sql, params)

    monkeypatch.setattr(engine.db, "query", spy)


def test_a_failure_that_cannot_be_written_is_counted_not_swallowed(core, monkeypatch):
    engine = core.recovery
    _reject_failure_writes(monkeypatch, engine)
    monkeypatch.setattr(engine, "max_retries", 0)
    monkeypatch.setattr(engine, "base_delay", 0.0)
    before = engine.recording_status()

    async def execute(action, ctx):
        return failed(action.tool, "connection reset")

    outcome = asyncio.run(engine.recover(
        PlannedAction(tool="get_time", params={}), None, execute,
        core.tools.get("get_time"), failed("get_time", "connection reset")))

    assert outcome.recovered is False, "the pipeline must still not be broken by this"
    status = engine.recording_status()
    assert status["unwritten"] == before["unwritten"] + 1, (
        f"the record vanished without a trace: {status}")
    assert "failures" in str(status["last_error"]), status


def test_a_written_failure_is_counted_too(core, monkeypatch):
    """The counter is only meaningful if the success path does not also move it."""
    engine = core.recovery
    monkeypatch.setattr(engine, "max_retries", 0)
    monkeypatch.setattr(engine, "base_delay", 0.0)
    before = engine.recording_status()

    async def execute(action, ctx):
        return failed(action.tool, "no such file")

    asyncio.run(engine.recover(
        PlannedAction(tool="read_file", params={"path": "/nope"}), None, execute,
        core.tools.get("read_file"), failed("read_file", "no such file")))
    after = engine.recording_status()
    assert after["unwritten"] == before["unwritten"], after
    assert after["written"] > before["written"], (after, before)


def test_the_stats_report_their_own_unavailability(client, core, monkeypatch):
    _reject_failure_reads(monkeypatch, core.recovery)
    stats = core.recovery.failure_stats()
    assert stats["total_failures"] == 0
    assert "no such table" in stats["ledger_error"], stats
    assert {"total_failures", "total_recovered", "recovery_rate", "by_tool"} <= set(stats)

    body = client.get("/metrics").json()
    assert "no such table" in body["failures"]["ledger_error"], body["failures"]
    assert body["failures"]["unwritten"] >= 0
    assert body["recent_failures"] == [], body["recent_failures"]


def test_the_failures_endpoint_says_it_cannot_read_instead_of_500(client, core, monkeypatch):
    _reject_failure_reads(monkeypatch, core.recovery)
    response = client.get("/failures?limit=5")
    assert response.status_code == 503, response.status_code
    assert "no such table" in response.text


def test_the_briefing_survives_an_unreadable_failure_ledger(core, monkeypatch):
    from jarvis.core.briefing import build_briefing

    _reject_failure_reads(monkeypatch, core.recovery)
    report = asyncio.run(build_briefing(core, sections=["failures"]))
    assert "unavailable" in report["text"].lower(), report["text"][:200]


# ---------------------------------------------------------------------------
# 26-E / R-12 -- the gated half stops being invisible
# ---------------------------------------------------------------------------


def _swapped(core, name, handler):
    original = core.tools._handlers[name]
    core.tools._handlers[name] = handler
    return original


def test_a_failed_confirmed_action_reaches_the_failure_ledger(core, tmp_area):
    """Measured before the fix: the confirmed delete failed and ``failures``
    went 1 -> 1. Every gated tool ran outside the ledger, so ``/metrics``
    described only the ungated half of the system."""
    victim = tmp_area / "phase26-ledger.txt"
    victim.write_text("x")
    n0 = core.db.query("SELECT COUNT(*) n FROM failures")[0]["n"]

    async def handler(ctx, **kw):
        return ActionResult(ok=False, tool="delete_file",
                            error="device did not answer", verified=False)

    original = _swapped(core, "delete_file", handler)
    try:
        verdict = core.policy.evaluate(PlannedAction(
            step_id=1, tool="delete_file", params={"path": str(victim)},
            device="laptop_main"))
        assert verdict.decision is PolicyDecision.CONFIRM, verdict.reason
        response = asyncio.run(core.resolve_confirmation(
            verdict.confirmation_id, True, session_id="phase26"))
    finally:
        core.tools._handlers["delete_file"] = original

    assert response.actions and response.actions[0].ok is False
    rows = core.db.query(
        "SELECT * FROM failures ORDER BY rowid DESC LIMIT 1")
    assert rows, "the gated failure was recorded nowhere"
    row = dict(rows[0])
    assert row["tool"] == "delete_file", row
    assert int(row["recovered"]) == 0, row
    assert int(row["attempts"]) == 0, row
    assert core.db.query("SELECT COUNT(*) n FROM failures")[0]["n"] == n0 + 1


def test_the_confirmed_path_does_not_re_execute_an_approved_action(core, tmp_area):
    """One approval buys one attempt: recovery must not spend it twice."""
    victim = tmp_area / "phase26-once.txt"
    victim.write_text("x")
    calls = {"n": 0}

    async def handler(ctx, **kw):
        calls["n"] += 1
        return ActionResult(ok=False, tool="delete_file",
                            error="device did not answer", verified=False)

    original = _swapped(core, "delete_file", handler)
    try:
        verdict = core.policy.evaluate(PlannedAction(
            step_id=1, tool="delete_file", params={"path": str(victim)},
            device="laptop_main"))
        asyncio.run(core.resolve_confirmation(verdict.confirmation_id, True))
    finally:
        core.tools._handlers["delete_file"] = original

    assert calls["n"] == 1, f"an approved destructive action ran {calls['n']} times"
    row = dict(core.db.query(
        "SELECT * FROM failures ORDER BY rowid DESC LIMIT 1")[0])
    assert "not retried" in row["note"], row
    assert row["strategy"] in ("none", "skipped"), row


def test_recovery_still_recovers_for_a_tool_where_rerunning_is_safe(core):
    """The narrowed policy must not have deleted the useful half of recovery."""
    engine = RecoveryEngine(max_retries=2, base_delay=0.0)
    calls = {"n": 0}

    async def execute(action, ctx):
        calls["n"] += 1
        if calls["n"] < 2:
            return failed(action.tool, TRANSIENT)
        return ActionResult(ok=True, tool=action.tool, message="found 3 files",
                            verified=True)

    outcome = asyncio.run(engine.recover(
        PlannedAction(tool="search_files", params={"query": "phase26"}), None, execute,
        core.tools.get("search_files"), failed("search_files", TRANSIENT)))
    assert outcome.recovered is True and outcome.strategy == "retry", outcome.as_dict()


def test_an_unverified_retry_is_not_reported_as_a_recovery(core):
    """Phase 10 P0-1's claim, on a tool the narrowed policy still re-runs.

    The claim was originally reproduced on ``control_home_device``; R-2 took
    that tool out of the retry set, so the *executed*-retry half of it is held
    here instead of being quietly dropped with the tool change.
    """
    engine = RecoveryEngine(max_retries=2, base_delay=0.0)
    calls = {"n": 0}

    async def execute(action, ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            return failed(action.tool, "gateway connection reset")
        return unverified(action.tool, "index did not answer, nothing was found")

    outcome = asyncio.run(engine.recover(
        PlannedAction(tool="search_files", params={"query": "phase26"}), None, execute,
        core.tools.get("search_files"), failed("search_files", "gateway connection reset")))

    assert calls["n"] >= 2, "the retry did not actually run, so this proves nothing"
    assert outcome.recovered is False, outcome.as_dict()
    assert "succeeded on retry" not in outcome.note, outcome.note


def test_an_unattended_failure_reaches_the_same_ledger(core):
    """R-12's other half, measured before the fix: a scheduled action that
    failed wrote ``failures: 0 -> 0``. ``schedule_runs`` told the truth about the
    run; the recovery ledger had never heard of it, so ``/metrics`` and
    ``explain_failures`` described only the interactive half."""
    from jarvis.scheduler.runner import SchedulerRunner
    from jarvis.scheduler.schedule import Recurrence
    from jarvis.scheduler.store import ScheduledJob

    n0 = core.db.query("SELECT COUNT(*) n FROM failures")[0]["n"]
    job = ScheduledJob(schedule_id="SCH-phase26", name="phase26 probe",
                       recurrence=Recurrence("daily"), action="read_file",
                       params={"path": "/tmp/definitely-absent-phase26"})
    outcome = asyncio.run(SchedulerRunner().run_job(core, job))

    assert outcome["outcome"] == "failed", outcome
    rows = core.db.query("SELECT COUNT(*) n FROM failures")
    assert rows[0]["n"] == n0 + 1, f"failures {n0} -> {rows[0]['n']}"
    row = dict(core.db.query(
        "SELECT * FROM failures ORDER BY rowid DESC LIMIT 1")[0])
    assert row["tool"] == "read_file" and int(row["attempts"]) == 0, row
    assert "unattended" in row["note"], row
    assert row["strategy"] == "none", row


def test_a_fallback_result_is_attributed_to_the_tool_that_produced_it(core):
    """R-14's measurable half: the step for ``search_files`` ends up carrying a
    result produced by ``list_directory``. The ledger row must not imply the
    planned handler ran."""
    engine = RecoveryEngine(max_retries=0, base_delay=0.0)

    async def execute(action, ctx):
        if action.tool == "list_directory":
            return ActionResult(ok=True, tool="list_directory",
                                message="listed 41 entries", verified=True)
        return failed(action.tool, "Spotlight index unavailable")

    outcome = asyncio.run(engine.recover(
        PlannedAction(tool="search_files", params={"query": "phase26"}), None, execute,
        core.tools.get("search_files"), failed("search_files", "Spotlight index unavailable")))

    assert outcome.recovered is True
    assert outcome.result.tool == "list_directory", outcome.result.tool
    # The outcome says which planned tool it answered, so the ledger row for
    # `search_files` can name the tool that actually ran.
    assert outcome.as_dict()["answered_for"] == "search_files", outcome.as_dict()
    assert "list_directory" in outcome.note, outcome.note
