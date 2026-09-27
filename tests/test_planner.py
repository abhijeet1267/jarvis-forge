"""Tests for intent detection and planning."""

from __future__ import annotations

import pytest

from jarvis.core.planner import RulePlanner
from jarvis.tools import build_default_registry


@pytest.fixture(scope="module")
def planner() -> RulePlanner:
    return RulePlanner(build_default_registry())


def first_tool(plan) -> str:
    return plan.actions[0].tool if plan.actions else ""


@pytest.mark.parametrize(
    "text,expected_tool",
    [
        ("open VS Code", "open_app"),
        ("jarvis, open chrome", "open_app"),
        ("close terminal", "close_app"),
        ("what is my battery", "battery_status"),
        ("what time is it", "get_time"),
        ("take a screenshot", "take_screenshot"),
        ("find my report", "search_files"),
        ("check my car fuel", "get_car_telemetry"),
        ("give me today's location summary", "get_location_summary"),
        ("stop everything", "emergency_stop"),
        ("emergency stop", "emergency_stop"),
        ("list apps", "list_apps"),
        ("remember that my supervisor is Dr Rao", "remember_fact"),
        ("what did I work on yesterday", "search_memory"),
        ("compress /tmp/x", "compress_path"),
    ],
)
def test_intent_mapping(planner: RulePlanner, text: str, expected_tool: str) -> None:
    plan = planner.parse(text)
    assert first_tool(plan) == expected_tool, f"{text!r} -> {first_tool(plan)!r}"


def test_wake_word_is_stripped(planner: RulePlanner) -> None:
    plan = planner.parse("Hey Jarvis, open VS Code")
    assert first_tool(plan) == "open_app"


def test_path_case_is_preserved(planner: RulePlanner) -> None:
    plan = planner.parse("delete /tmp/JarvisDemo/AMR_Report.pdf")
    assert plan.actions[0].params["path"] == "/tmp/JarvisDemo/AMR_Report.pdf"


def test_search_by_filename_with_extension(planner: RulePlanner) -> None:
    """'find nested.txt' must work, not only 'find my report'."""
    plan = planner.parse("find nested.txt in /tmp/somewhere")
    assert first_tool(plan) == "search_files"
    assert plan.actions[0].params["query"] == "nested.txt"
    assert plan.actions[0].params["directory"] == "/tmp/somewhere"


def test_search_preserves_filename_case(planner: RulePlanner) -> None:
    plan = planner.parse("find AMR_Report.pdf in /tmp/somewhere")
    assert plan.actions[0].params["query"] == "AMR_Report.pdf"


def test_search_treats_only_real_paths_as_directories(planner: RulePlanner) -> None:
    """'... in the AMR project' is a scope phrase, not a path.

    When the caller named no place, the plan carries no ``directory`` at all and
    the tool applies its own default. Putting ``~`` in the parameters instead
    made every ordinary search look like a request aimed at the home directory.
    """
    plan = planner.parse("find my report in the AMR project")
    assert "directory" not in plan.actions[0].params
    assert plan.actions[0].params["query"] == "report"


@pytest.mark.parametrize(
    "text,expected_tool",
    [
        ("what time is it", "get_time"),
        ("what is my battery", "battery_status"),
        ("take a screenshot", "take_screenshot"),
    ],
)
def test_search_rule_does_not_hijack_other_intents(
    planner: RulePlanner, text: str, expected_tool: str
) -> None:
    assert first_tool(planner.parse(text)) == expected_tool


def test_vague_delete_asks_for_clarification(planner: RulePlanner) -> None:
    plan = planner.parse("delete this")
    assert plan.actions == []
    assert plan.direct_reply


def test_unimplemented_capabilities_reply_honestly(planner: RulePlanner) -> None:
    # Email, calendar and transfer became real in v0.6/v0.7, home in v1.5, the car
    # in v1.7 and the watch in v1.8; what remains unimplemented must still say so
    # rather than pretending.
    for text in ("what is my phone battery",):
        plan = planner.parse(text)
        assert plan.actions == [], f"{text!r} must not produce actions"
        assert plan.direct_reply
        assert "not" in plan.direct_reply.lower()


def test_unknown_command_gets_guidance(planner: RulePlanner) -> None:
    plan = planner.parse("flibbertigibbet the wozzle")
    assert plan.intent == "unknown"
    assert plan.direct_reply


def test_planner_never_invents_tools(planner: RulePlanner) -> None:
    """Every emitted tool must exist in the registry."""
    samples = [
        "open VS Code",
        "find my report",
        "what is my battery",
        "check my car fuel",
        "remember that X",
        "stop everything",
    ]
    for text in samples:
        plan = planner.parse(text)
        for action in plan.actions:
            assert planner.tools.has(action.tool)
