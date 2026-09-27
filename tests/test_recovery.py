"""Recovery system tests (section 47).

The central safety property: a *destructive* action is never retried
automatically, because a retry assumes the first attempt changed nothing.
"""

from __future__ import annotations

import pytest

from jarvis.core.models import ActionResult, PlannedAction, RiskLevel
from jarvis.core.recovery import RecoveryEngine, looks_transient


def _failure(error: str = "something went wrong") -> ActionResult:
    return ActionResult(ok=False, tool="t", error=error, message=error, verified=False)


def _success(tool: str = "t") -> ActionResult:
    return ActionResult(ok=True, tool=tool, message="done", verified=True)


class _Spec:
    """Minimal stand-in for ToolSpec."""

    def __init__(self, name: str, risk: RiskLevel, tags=None) -> None:
        self.name = name
        self.risk = risk
        self.tags = tags or []


# ---------------------------------------------------------------------------
# Transient detection
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "error",
    ["connection refused", "operation timed out", "device not ready", "resource busy", "ETIMEDOUT"],
)
def test_transient_errors_are_recognised(error: str) -> None:
    assert looks_transient(_failure(error))


@pytest.mark.parametrize("error", ["file not found", "permission denied", "invalid argument"])
def test_deterministic_errors_are_not_transient(error: str) -> None:
    assert not looks_transient(_failure(error))


# ---------------------------------------------------------------------------
# Retry policy -- the safety-critical part
# ---------------------------------------------------------------------------
def test_read_only_tools_are_retryable() -> None:
    engine = RecoveryEngine()
    spec = _Spec("get_time", RiskLevel.READ)
    assert engine.is_retryable(spec, _failure())


@pytest.mark.parametrize(
    "name,risk",
    [
        ("delete_file", RiskLevel.DESTRUCTIVE),
        ("move_file", RiskLevel.DESTRUCTIVE),
        ("emergency_stop", RiskLevel.PHYSICAL),
    ],
)
def test_destructive_and_physical_actions_are_never_retried(name: str, risk: RiskLevel) -> None:
    """Retrying these could act on a world the user did not approve."""
    engine = RecoveryEngine()
    assert not engine.is_retryable(_Spec(name, risk), _failure("timed out"))


def test_unknown_tool_is_not_retryable() -> None:
    assert not RecoveryEngine().is_retryable(None, _failure())


def test_external_tool_retries_only_on_transient_errors() -> None:
    engine = RecoveryEngine()
    spec = _Spec("run_allowlisted_command", RiskLevel.EXTERNAL)
    assert engine.is_retryable(spec, _failure("connection refused"))
    assert not engine.is_retryable(spec, _failure("invalid argument"))


# ---------------------------------------------------------------------------
# Recovery flow
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_recovery_succeeds_on_retry() -> None:
    engine = RecoveryEngine(max_retries=2, base_delay=0.0)
    calls = {"n": 0}

    async def execute(action, ctx):
        calls["n"] += 1
        return _success("search_files") if calls["n"] >= 2 else _failure("timed out")

    outcome = await engine.recover(
        PlannedAction(tool="search_files", params={"query": "x"}),
        None,
        execute,
        _Spec("search_files", RiskLevel.READ),
        _failure("timed out"),
    )

    assert outcome.recovered is True
    assert outcome.strategy == "retry"
    assert outcome.result.ok


@pytest.mark.asyncio
async def test_recovery_stops_retrying_a_deterministic_error() -> None:
    """A 'file not found' will not fix itself; do not exhaust the retry budget."""
    engine = RecoveryEngine(max_retries=3, base_delay=0.0)
    tried: list[str] = []

    async def execute(action, ctx):
        tried.append(action.tool)
        return _failure("file not found")

    outcome = await engine.recover(
        PlannedAction(tool="read_file", params={"path": "/nope"}),
        None,
        execute,
        _Spec("read_file", RiskLevel.READ),
        _failure("file not found"),
    )

    assert outcome.recovered is False
    # One retry, then it gives up on the retry path rather than burning all 3.
    assert tried.count("read_file") <= 1, tried
    # It may still try the declared alternative (search_files) before escalating.
    assert set(tried) <= {"read_file", "search_files"}
    assert "not transient" in outcome.note


@pytest.mark.asyncio
async def test_deterministic_error_does_not_exhaust_retry_budget() -> None:
    """A tool with no alternative must not be retried max_retries times."""
    engine = RecoveryEngine(max_retries=5, base_delay=0.0)
    calls = {"n": 0}

    async def execute(action, ctx):
        calls["n"] += 1
        return _failure("permission denied")

    # get_time has no declared alternative.
    await engine.recover(
        PlannedAction(tool="get_time", params={}),
        None,
        execute,
        _Spec("get_time", RiskLevel.READ),
        _failure("permission denied"),
    )

    assert calls["n"] == 1, f"expected a single attempt, made {calls['n']}"


@pytest.mark.asyncio
async def test_destructive_failure_is_not_executed_again() -> None:
    """The critical safety test: a failed delete must not be retried."""
    engine = RecoveryEngine(max_retries=3, base_delay=0.0)
    calls = {"n": 0}

    async def execute(action, ctx):
        calls["n"] += 1
        return _failure("timed out")

    outcome = await engine.recover(
        PlannedAction(tool="delete_file", params={"path": "/tmp/x"}),
        None,
        execute,
        _Spec("delete_file", RiskLevel.DESTRUCTIVE),
        _failure("timed out"),
    )

    assert calls["n"] == 0, "a destructive action must never be re-executed"
    assert outcome.recovered is False
    assert "not retried" in outcome.note


@pytest.mark.asyncio
async def test_alternative_tool_is_tried() -> None:
    engine = RecoveryEngine(max_retries=0, base_delay=0.0)
    tried = []

    async def execute(action, ctx):
        tried.append(action.tool)
        # The primary keeps failing; the alternative works.
        return _success("list_directory") if action.tool == "list_directory" else _failure("boom")

    outcome = await engine.recover(
        PlannedAction(tool="search_files", params={"directory": "/tmp"}),
        None,
        execute,
        _Spec("search_files", RiskLevel.READ),
        _failure("boom"),
    )

    assert "list_directory" in tried
    assert outcome.recovered is True
    assert outcome.strategy == "alternative"


@pytest.mark.asyncio
async def test_unrecoverable_failure_escalates() -> None:
    engine = RecoveryEngine(max_retries=1, base_delay=0.0)

    async def execute(action, ctx):
        return _failure("permission denied")

    outcome = await engine.recover(
        PlannedAction(tool="read_file", params={"path": "/root/x"}),
        None,
        execute,
        _Spec("read_file", RiskLevel.READ),
        _failure("permission denied"),
    )

    assert outcome.recovered is False
    assert outcome.strategy == "escalated"
    assert outcome.note


# ---------------------------------------------------------------------------
# Failure recording
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_failures_are_recorded_for_later_planning(core) -> None:
    engine = core.recovery
    before = len(engine.recent_failures(limit=200))

    async def execute(action, ctx):
        return _failure("permanent problem")

    await engine.recover(
        PlannedAction(tool="read_file", params={"path": "/nope"}),
        None,
        execute,
        core.tools.get("read_file"),
        _failure("permanent problem"),
    )

    assert len(engine.recent_failures(limit=200)) > before


def test_failure_stats_report_recovery_rate(core) -> None:
    stats = core.recovery.failure_stats()
    assert {"total_failures", "total_recovered", "recovery_rate", "by_tool"} <= set(stats)
    assert 0.0 <= stats["recovery_rate"] <= 1.0
