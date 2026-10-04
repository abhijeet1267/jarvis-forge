"""Phase 53 -- Vantage answering questions about itself.

Measured before writing anything, against the real registry (136 tools, 50 routes). Four of
the six §6 self-state utterances reached nothing, and three reached something **worse** than
nothing:

    are you running            -> check_agent_status(app="You")
    what is the current task   -> ask_question(topic="current task")
    what are you doing now     -> ask_question(topic="you doing right now")
    what is your status        -> ask_question(topic="your status")

A sentence about Vantage answered by looking for an installed application literally named
"You", or by treating it as general knowledge, is not a missing feature -- it is a confident
wrong answer, and it is the class this project keeps having to kill.

The authoritative state already exists. This phase adds no store:

    pending approvals   -> core.policy.pending()      (expires stale rows first)
    health / mode       -> the same objects /health reads
    recent actions      -> core.audit.recent(limit)
    current task        -> core.tasks.list() + the step-status rollup
    stopping it         -> core.tasks.cancel(job_id)

and it keeps `cancel_schedule` out of this. A schedule is a thing that will run later; a task
is one execution happening now. Routing "stop the current task" at the schedule store would
cancel the wrong object and report success doing it.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from jarvis.core.models import ActionStatus, PlannedAction

NEW_TOOLS = (
    "get_pending_confirmations",
    "get_vantage_status",
    "get_recent_activity",
    "get_current_task",
    "stop_current_task",
)


@pytest.fixture(scope="module")
def registry():
    from jarvis.tools import build_default_registry

    return build_default_registry()


@pytest.fixture(scope="module")
def planner(registry):
    from jarvis.core.planner import RulePlanner

    return RulePlanner(registry)


@pytest.fixture()
def fresh():
    """Its own core. The session `core` is shared, and this phase writes rows."""
    from jarvis.core.orchestrator import JarvisCore

    return JarvisCore()


def _tools(plan):
    return [a.tool for a in plan.actions]


def _ctx_for(core, session: str = "p53"):
    """A context carrying the live core.

    These tools read Vantage's own state, so they are given the same core the pipeline gives
    them at runtime. Without it they refuse -- a self-report built from a second, freshly
    constructed store would be a second source of truth wearing the first one's clothes.
    """
    from jarvis.tools.base import ToolContext

    return ToolContext(session_id=session, core=core)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 1. the five capabilities exist, registered and gated
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", NEW_TOOLS)
def test_the_self_state_capability_is_registered(registry, name):
    assert registry.get(name) is not None, f"{name} is not a registered tool"


@pytest.mark.parametrize("name", NEW_TOOLS[:4])
def test_reading_ones_own_state_is_a_read(registry, name):
    """Self-observation must not inherit a confirmation gate it does not need -- but it also
    must not be able to change anything."""
    spec = registry.get(name)
    assert int(spec.risk) == 0, f"{name} is not READ tier"
    assert spec.requires_confirmation is False


def test_stopping_a_task_is_executable_immediately(registry):
    """§30 of the build prompt: a stop must not be gated behind an approval. PHYSICAL stays
    reserved for the emergency stop; this is a normal control on Vantage's own work."""
    spec = registry.get("stop_current_task")
    assert int(spec.risk) == 1, "stop_current_task left the LOW tier it was reviewed at"
    assert spec.requires_confirmation is False, "a stop that needs approval is not a stop"


# ---------------------------------------------------------------------------
# 2. pending approvals -- from the policy engine, never synthesized
# ---------------------------------------------------------------------------


def test_nothing_waiting_says_so_without_inventing_anything(fresh):
    _clear_pending(fresh)
    assert fresh.policy.pending() == [], "the fixture expected an empty queue"
    result = _run(fresh.tools.handler("get_pending_confirmations")(
        _ctx_for(fresh, session="p53-empty")))
    assert result.ok is True, result.error
    assert result.output.get("count") == 0
    assert "nothing" in result.message.lower()


def test_a_real_pending_approval_is_reported(fresh):
    _clear_pending(fresh)
    outcome = fresh.policy.evaluate(PlannedAction(
        step_id=1, tool="delete_file", params={"path": "/tmp/p53-target.txt"},
        device="laptop_main"))
    assert outcome.confirmation_id, f"expected CONFIRM, got {outcome.decision}"
    try:
        result = _run(fresh.tools.handler("get_pending_confirmations")(
            _ctx_for(fresh, session="p53-one")))
        assert result.output.get("count") == 1, result.output
        assert outcome.confirmation_id in result.message or any(
            outcome.confirmation_id in str(row) for row in result.output["confirmations"])
        assert "delete_file" in str(result.output["confirmations"])
    finally:
        fresh.db.execute("DELETE FROM confirmations WHERE confirmation_id = ?",
                         (outcome.confirmation_id,))


def test_an_expired_approval_is_not_offered_as_pending(fresh):
    _clear_pending(fresh)
    """The TTL semantics belong to the policy engine. A tool that queried the table directly
    would show a stale row and invite the user to approve something already dead."""
    outcome = fresh.policy.evaluate(PlannedAction(
        step_id=1, tool="delete_file", params={"path": "/tmp/p53-expire.txt"},
        device="laptop_main"))
    cid = outcome.confirmation_id
    assert cid
    try:
        fresh.db.execute("UPDATE confirmations SET created_at = ? WHERE confirmation_id = ?",
                         ("2000-01-01T00:00:00+00:00", cid))
        result = _run(fresh.tools.handler("get_pending_confirmations")(
            _ctx_for(fresh, session="p53-expired")))
        assert result.output.get("count") == 0, (
            f"a confirmation older than the TTL was still offered: {result.output}")
    finally:
        fresh.db.execute("DELETE FROM confirmations WHERE confirmation_id = ?", (cid,))


def test_a_resolved_approval_is_not_pending(fresh):
    _clear_pending(fresh)
    outcome = fresh.policy.evaluate(PlannedAction(
        step_id=1, tool="delete_file", params={"path": "/tmp/p53-resolved.txt"},
        device="laptop_main"))
    cid = outcome.confirmation_id
    try:
        fresh.db.execute("UPDATE confirmations SET state = ? WHERE confirmation_id = ?",
                         ("approved", cid))
        result = _run(fresh.tools.handler("get_pending_confirmations")(
            _ctx_for(fresh, session="p53-resolved")))
        assert result.output.get("count") == 0, result.output
    finally:
        fresh.db.execute("DELETE FROM confirmations WHERE confirmation_id = ?", (cid,))


# ---------------------------------------------------------------------------
# 3. health and mode -- the same facts /health publishes, not a second story
# ---------------------------------------------------------------------------


def test_the_status_answer_agrees_with_the_health_surface(fresh):
    """Two projections of one state are allowed; two answers are not. If this tool ever
    counts its own tools or invents its own mode, this test is where it is caught."""
    result = _run(fresh.tools.handler("get_vantage_status")(
        _ctx_for(fresh, session="p53-status")))
    assert result.ok is True, result.error
    assert result.output.get("tools") == len(fresh.tools.names())
    assert result.output.get("llm_enabled") is fresh.settings.llm_enabled
    assert result.output.get("scheduler_running") is fresh.scheduler.describe()["running"]


def test_basic_mode_is_named_as_the_mode_when_no_model_is_configured(fresh, monkeypatch):
    #: `llm_enabled` is a property of (llm_base_url and llm_api_key); the field is what can be
    #: set. Patching the property would raise, and silently skipping would hide the case.
    monkeypatch.setattr(fresh.settings, "llm_api_key", "")
    assert fresh.settings.llm_enabled is False, "the fixture no longer models Basic Mode"
    result = _run(fresh.tools.handler("get_vantage_status")(
        _ctx_for(fresh, session="p53-mode")))
    assert "basic" in result.message.lower(), result.message
    assert "no model" in result.message.lower(), result.message


def test_the_status_answer_never_claims_a_model_it_does_not_have(fresh, monkeypatch):
    #: `llm_enabled` is a property of (llm_base_url and llm_api_key); the field is what can be
    #: set. Patching the property would raise, and silently skipping would hide the case.
    monkeypatch.setattr(fresh.settings, "llm_api_key", "")
    assert fresh.settings.llm_enabled is False, "the fixture no longer models Basic Mode"
    result = _run(fresh.tools.handler("get_vantage_status")(
        _ctx_for(fresh, session="p53-nollm")))
    lowered = (result.message + str(result.output)).lower()
    for claim in ("openai", "gemini", "gpt"):
        assert claim not in lowered, f"the status answer named {claim!r} with no provider set"


# ---------------------------------------------------------------------------
# 4. recent actions -- what the ledger holds, reported as the ledger holds it
# ---------------------------------------------------------------------------


def test_an_empty_history_says_so(fresh):
    fresh.db.execute("DELETE FROM action_logs")
    result = _run(fresh.tools.handler("get_recent_activity")(
        _ctx_for(fresh, session="p53-nohistory")))
    assert result.ok is True, result.error
    assert result.output.get("count") == 0
    assert "no recorded" in result.message.lower(), result.message


def test_recorded_results_are_reported_as_recorded(fresh):
    """A denied row must not come back looking like a success, and an unverified one must
    not quietly become either."""
    fresh.db.execute("DELETE FROM action_logs")
    for tool, result_state in (("delete_file", "failure"), ("send_email", "denied"),
                               ("take_screenshot", "unverified")):
        fresh.db.execute(
            "INSERT INTO action_logs (log_id, request_id, session_id, user_id, device, "
            "intent, tool, target, risk, result, detail, created_at, prev_hash, record_hash) "
            "VALUES (?, ?, 'p53', 'user', 'laptop_main', 'x', ?, '', 3, ?, '', ?, '', '')",
            (f"LOG-{tool}-{int(time.time()*1000)}", f"REQ-{tool}", tool, result_state,
             time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())),
        )
    result = _run(fresh.tools.handler("get_recent_activity")(
        _ctx_for(fresh, session="p53-history")))
    assert result.output.get("count") == 3, result.output
    text = str(result.output.get("actions")).lower()
    for word in ("failure", "denied", "unverified"):
        assert word in text, f"{word!r} was smoothed out of the answer: {text}"


def test_the_activity_answer_does_not_claim_the_chain_is_intact(fresh):
    """This machine's history deliberately carries forged rows, and the verifier reports
    BROKEN. An activity answer that asserted integrity would be falsifying the audit."""
    result = _run(fresh.tools.handler("get_recent_activity")(
        _ctx_for(fresh, session="p53-chain")))
    assert "intact" not in result.message.lower() or fresh.policy is None


# ---------------------------------------------------------------------------
# 5. the current task, and stopping it
# ---------------------------------------------------------------------------


def _clear_pending(core) -> None:
    """Empty the approval queue for tests that assert on it.

    The session database is shared across modules, so a confirmation left by an earlier test
    would make "nothing is waiting" false -- and a false failure here is exactly the leak this
    project has already been bitten by twice.
    """
    core.db.execute("DELETE FROM confirmations")


def _clear_active_jobs(core) -> None:
    """End any job another test left in flight.

    The session database is shared, and `stop_current_task` acts on the newest active job --
    so without this a stop test could cancel a row it did not create and still pass.
    """
    core.db.execute("UPDATE tasks SET status = 'done' WHERE job_id != ''")


def _running_job(fresh, goal: str = "p53 open the thing") -> str:
    job = fresh.tasks.create(goal, ["open_application", "take_screenshot"])
    fresh.tasks.update_step(job.job_id, "open_application", ActionStatus.RUNNING)
    return job.job_id


def test_nothing_running_says_nothing_is_running(fresh):
    _clear_active_jobs(fresh)
    result = _run(fresh.tools.handler("get_current_task")(
        _ctx_for(fresh, session="p53-notask")))
    assert result.ok is True, result.error
    assert result.output.get("running") is False, result.output
    assert "nothing" in result.message.lower()


def test_a_running_job_is_named_with_its_real_step_states(fresh):
    job_id = _running_job(fresh)
    try:
        result = _run(fresh.tools.handler("get_current_task")(
            _ctx_for(fresh, session="p53-task")))
        assert result.output.get("running") is True, result.output
        assert result.output.get("job_id") == job_id
        assert "p53 open the thing" in result.message
        steps = str(result.output.get("steps"))
        assert "running" in steps.lower() and "pending" in steps.lower(), steps
    finally:
        fresh.db.execute("DELETE FROM task_steps WHERE job_id = ?", (job_id,))
        fresh.db.execute("DELETE FROM tasks WHERE job_id = ?", (job_id,))


def test_a_waiting_approval_is_reported_as_waiting(fresh):
    job = fresh.tasks.create("p53 needs approval", ["delete_file"])
    fresh.tasks.update_step(job.job_id, "delete_file", ActionStatus.AWAITING_CONFIRMATION)
    try:
        result = _run(fresh.tools.handler("get_current_task")(
            _ctx_for(fresh, session="p53-await")))
        assert "await" in str(result.output).lower() or "approval" in result.message.lower()
    finally:
        fresh.db.execute("DELETE FROM task_steps WHERE job_id = ?", (job.job_id,))
        fresh.db.execute("DELETE FROM tasks WHERE job_id = ?", (job.job_id,))


def test_nothing_executing_while_an_approval_waits_says_both(fresh):
    """Measured live: a command stopped at the permission gate writes no task row at all, so
    the truthful answer to "what is the current task" is "nothing is running" -- and a user
    who just watched an approval appear would read that as the system forgetting their
    request. Both facts are true, so both get said."""
    _clear_pending(fresh)
    _clear_active_jobs(fresh)
    outcome = fresh.policy.evaluate(PlannedAction(
        step_id=1, tool="delete_file", params={"path": "/tmp/p53-waiting.txt"},
        device="laptop_main"))
    cid = outcome.confirmation_id
    assert cid
    try:
        result = _run(fresh.tools.handler("get_current_task")(
            _ctx_for(fresh, session="p53-waiting")))
        assert result.output.get("running") is False, result.output
        assert "approval" in result.message.lower() or "waiting" in result.message.lower(), (
            result.message)
    finally:
        fresh.db.execute("DELETE FROM confirmations WHERE confirmation_id = ?", (cid,))


def test_stopping_a_task_reads_the_state_back_before_saying_it_stopped(fresh):
    _clear_active_jobs(fresh)
    job_id = _running_job(fresh, goal="p53 stop me")
    try:
        result = _run(fresh.tools.handler("stop_current_task")(
            _ctx_for(fresh, session="p53-stop")))
        assert result.ok is True, result.error
        assert fresh.tasks.get(job_id)["status"] == "cancelled"
        assert result.verified is True, "the claim must come from the state read back"
        assert "cancelled" in result.message.lower()
        # §30: what was stopped, what could not be, and the current state.
        assert "not undone" in result.message.lower() or "already" in result.message.lower()
    finally:
        fresh.db.execute("DELETE FROM task_steps WHERE job_id = ?", (job_id,))
        fresh.db.execute("DELETE FROM tasks WHERE job_id = ?", (job_id,))


def test_stopping_when_nothing_is_running_stops_nothing_and_says_so(fresh):
    _clear_active_jobs(fresh)
    fresh.db.execute(
        "UPDATE tasks SET status = 'done' WHERE status IN ('running','incomplete',"
        "'awaiting_confirmation','paused')")
    result = _run(fresh.tools.handler("stop_current_task")(
        _ctx_for(fresh, session="p53-nostop")))
    assert result.output.get("cancelled") is False, result.output
    message = (result.message or result.error or "").lower()
    assert "nothing" in message, message
    assert "is cancelled" not in message and "stopped \"" not in message, (
        f"the answer claimed a stop that did not happen: {message}")


def test_stopping_a_task_does_not_touch_a_schedule(fresh):
    _clear_active_jobs(fresh)
    """A schedule is a thing that runs later; a task is one execution now. Conflating them
    would cancel the wrong object and report a success that never happened."""
    job_id = _running_job(fresh, goal="p53 schedule separation")
    before = fresh.db.query("SELECT schedule_id FROM scheduled_jobs")
    try:
        _run(fresh.tools.handler("stop_current_task")(_ctx_for(fresh, session="p53-sep")))
    finally:
        fresh.db.execute("DELETE FROM task_steps WHERE job_id = ?", (job_id,))
        fresh.db.execute("DELETE FROM tasks WHERE job_id = ?", (job_id,))
    assert fresh.db.query("SELECT schedule_id FROM scheduled_jobs") == before


# ---------------------------------------------------------------------------
# 6. the routes, including the three that were answering the wrong question
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("utterance,tool", [
    ("show pending confirmations", "get_pending_confirmations"),
    ("do I have any pending approvals", "get_pending_confirmations"),
    ("what approvals are waiting", "get_pending_confirmations"),
    ("what actions need my confirmation", "get_pending_confirmations"),
    ("are you running", "get_vantage_status"),
    ("what is your status", "get_vantage_status"),
    ("is Vantage healthy", "get_vantage_status"),
    ("what mode are you in", "get_vantage_status"),
    ("what did you just do", "get_recent_activity"),
    ("show recent actions", "get_recent_activity"),
    ("what happened recently", "get_recent_activity"),
    ("what was the last command", "get_recent_activity"),
    ("what are you doing", "get_current_task"),
    ("what is the current task", "get_current_task"),
    ("stop the current task", "stop_current_task"),
    ("cancel the current task", "stop_current_task"),
])
def test_a_self_state_sentence_reaches_the_right_capability(planner, utterance, tool):
    plan = planner.parse(utterance)
    assert tool in _tools(plan), (
        f"{utterance!r} -> {_tools(plan)} kind={plan.reply_kind!r}; "
        "an answer about Vantage must not be routed to another capability")


@pytest.mark.parametrize("utterance", [
    "are you running", "what is your status", "what are you doing", "what is the current task",
])
def test_a_question_about_vantage_is_never_asked_of_the_knowledge_tool(planner, utterance):
    """Measured before the fix: these four went to ask_question, and "are you running" went
    to an application probe for an app called "You"."""
    plan = planner.parse(utterance)
    tools = _tools(plan)
    assert "ask_question" not in tools, f"{utterance!r} was treated as general knowledge"
    for params in _params_of(plan, "check_agent_status"):
        assert params.get("app", "").lower() != "you", f"{utterance!r} probed an app named You"


def test_is_vantage_running_still_gets_the_verified_application_probe(planner):
    """The boundary, kept on purpose: this §6 sentence asks whether the Vantage application
    is up, and the probe answers with a real bundle check rather than a self-report."""
    plan = planner.parse("is Vantage running?")
    assert "check_agent_status" in _tools(plan), _tools(plan)


def test_a_question_about_the_laptop_is_not_answered_as_vantage_status(planner):
    plan = planner.parse("what is my system status?")
    assert "system_status" in _tools(plan), _tools(plan)
    assert "get_vantage_status" not in _tools(plan)


def test_search_google_for_x_is_still_not_pretended_at(planner):
    """§17: a missing capability must not be aliased into an existing one. "search Google
    for X" is not "open Google"."""
    plan = planner.parse("search Google for FastAPI")
    for params in _params_of(plan, "open_url"):
        assert "search" in params.get("url", "").lower() or "q=" in params.get("url", ""), (
            "a search request was answered by navigating to a home page")


def _params_of(plan, tool):
    return [a.params for a in plan.actions if a.tool == tool]


# ---------------------------------------------------------------------------
# 7. governance and no regressions
# ---------------------------------------------------------------------------


def test_self_state_reads_execute_without_a_confirmation(fresh):
    """Reading one's own state is not an action on the world; the gate must say EXECUTE."""
    outcome = fresh.policy.evaluate(PlannedAction(
        step_id=1, tool="get_pending_confirmations", params={}, device="laptop_main"))
    from jarvis.core.models import PolicyDecision

    assert outcome.decision is PolicyDecision.EXECUTE, outcome.reason


def test_the_new_surface_is_published_to_clients(registry):
    names = set(registry.names())
    assert set(NEW_TOOLS) <= names
    for spec in registry.all():
        assert spec.name in names


def test_the_registry_still_holds_no_duplicate_names(registry):
    specs = registry.all()
    assert len(specs) == len({s.name for s in specs})


@pytest.mark.parametrize("utterance,tool", [
    ("open Mail", "open_application"),
    ("open Downloads", "open_user_folder"),
    ("what date is it?", "get_time"),
    ("what is my hostname?", "get_machine_snapshot"),
    ("show the current application", "get_frontmost_application"),
    ("what is my battery", "get_battery_status"),
    ("show connected phones", "list_devices"),
    ("emergency stop", "emergency_stop"),
    ("what do you remember", "memory_overview"),
    ("check my unread emails", "search_email"),
])
def test_the_sentences_that_worked_before_still_work(planner, utterance, tool):
    assert tool in _tools(planner.parse(utterance)), f"{utterance!r} regressed"


def test_explain_still_reaches_the_knowledge_tool(planner):
    """The new self-state routes must not swallow ordinary questions about other things."""
    plan = planner.parse("explain how wifi works")
    assert "get_vantage_status" not in _tools(plan), _tools(plan)
