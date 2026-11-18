"""V26 VO-05: bare follow-up controls route without stealing named commands.

"cancel that" and "did it work" name no app and no file, so before this they
fell to ``unknown``. They now map onto the existing read-only task tools. The
risk this file guards against is over-capture: a rule that answers "cancel that"
must not also swallow "cancel my Design Review event" or a literal "stop".
"""

from __future__ import annotations

import pytest

from jarvis.core.planner import RulePlanner
from jarvis.registry.apps import AppRegistry
from jarvis.tools import build_default_registry


@pytest.fixture(scope="module")
def planner():
    return RulePlanner(build_default_registry(), AppRegistry())


@pytest.mark.parametrize("text", ["cancel that", "stop that", "abort this", "please cancel it"])
def test_bare_controls_stop_the_current_task(planner, text):
    plan = planner.parse(text)
    assert plan.intent == "stop_task"
    assert [a.tool for a in plan.actions] == ["stop_current_task"]


@pytest.mark.parametrize("text", [
    "did it work", "did that work", "was it done",
    "what happened in my last task", "show the result", "show me the receipt",
])
def test_result_questions_ask_the_task_tool(planner, text):
    plan = planner.parse(text)
    assert plan.intent == "current_task"
    assert [a.tool for a in plan.actions] == ["get_current_task"]


# --- over-capture guards: the rules above must not claim these ------------
@pytest.mark.parametrize("text,expected", [
    ("cancel my Design Review event", "cancel_event"),
    ("open Calculator", "open_app"),
    ("delete report.pdf", "delete"),
])
def test_named_commands_are_not_captured(planner, text, expected):
    assert planner.parse(text).intent == expected


def test_bare_stop_is_still_emergency_stop(planner):
    """The one-word form keeps its emergency meaning."""
    plan = planner.parse("stop")
    assert plan.intent == "emergency_stop"
    assert [a.tool for a in plan.actions] == ["emergency_stop"]


def test_stop_task_does_not_become_emergency_stop(planner):
    """'stop that' is a task control, not a physical emergency stop."""
    assert planner.parse("stop that").intent == "stop_task"


def test_control_routes_execute_nothing_by_themselves(planner):
    """These intents carry a read or control tool, never a writer."""
    for text in ("cancel that", "did it work"):
        tools = [a.tool for a in planner.parse(text).actions]
        assert tools in (["stop_current_task"], ["get_current_task"])