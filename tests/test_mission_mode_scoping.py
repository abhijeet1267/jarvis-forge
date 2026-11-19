"""Mission mode diverts actions, never reads.

Measured regression this closes: with `JARVIS_COMMAND_MISSION_MODE` on,
"what app is in front" (list_apps, READ-tier) answered *"I ran that, but could
not confirm it: steps ran, but the outcome is unknown"* where the inline path
answers *"I know 16 applications on this macos machine."* The read produced its
data; mission mode replaced it with a caveat about an outcome that was never at
stake.

Mission mode exists to make a *side effect* verifiable. A plan of reads changes
nothing, so diverting it adds no verification and destroys the answer.
"""

from __future__ import annotations

import pytest

from jarvis.core.models import PlannedAction, RiskLevel
from jarvis.core.orchestrator import JarvisCore


@pytest.fixture(scope="module")
def core():
    return JarvisCore()


class _Plan:
    def __init__(self, *actions):
        self.actions = list(actions)


def _a(tool):
    return PlannedAction(tool=tool, params={})


def _tier(core, tool):
    return core.tools.get(tool).risk


def test_the_example_tools_have_the_tiers_this_test_assumes(core):
    """Guard against the test silently becoming about the wrong tools."""
    assert _tier(core, "list_apps") == RiskLevel.READ
    assert _tier(core, "open_application") != RiskLevel.READ


def test_a_read_only_plan_is_not_diverted(core):
    assert core._plan_changes_the_world(_Plan(_a("list_apps"))) is False


def test_an_action_plan_is_diverted(core):
    assert core._plan_changes_the_world(_Plan(_a("open_application"))) is True


def test_a_mixed_plan_is_diverted(core):
    """One world-changing action is enough to need the mission record."""
    assert core._plan_changes_the_world(
        _Plan(_a("list_apps"), _a("open_application"))) is True


def test_an_unregistered_tool_is_treated_as_world_changing(core):
    """Conservative: unknown means 'let the engine and verifier decide'.

    Guessing that an unregistered tool is harmless would be the wrong direction
    for a flag that exists to make side effects visible.
    """
    assert core._plan_changes_the_world(_Plan(_a("no_such_tool_at_all"))) is True


def test_an_empty_plan_is_not_diverted(core):
    assert core._plan_changes_the_world(_Plan()) is False


def test_the_diversion_condition_consults_the_read_check(core):
    """The call site must include the read check, not just the flag."""
    import inspect

    source = inspect.getsource(JarvisCore._execute_plan)
    assert "_plan_changes_the_world" in source, (
        "the diversion no longer checks whether the plan changes anything, "
        "so a read-only command would lose its answer again")