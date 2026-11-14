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
        ("open VS Code", "open_application"),
        ("jarvis, open chrome", "open_application"),
        ("close terminal", "close_app"),
        ("what is my battery", "get_battery_status"),
        ("what time is it", "get_time"),
        ("take a screenshot", "take_screenshot"),
        ("find my report", "search_files"),
        ("check my car fuel", "get_car_telemetry"),
        ("give me today's location summary", "get_location_summary"),
        ("stop everything", "emergency_stop"),
        ("emergency stop", "emergency_stop"),
        ("list apps", "get_running_applications"),
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
    assert first_tool(plan) == "open_application"


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
        ("what is my battery", "get_battery_status"),
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


def test_general_question_reaches_model_when_llm_enabled(planner, monkeypatch):
    """When a model is configured, a general factual question must NOT be swallowed by
    the Wikipedia `ask_question` tool (which may be permission-revoked); it should fall
    through to the conversational model path (unrecognised → model)."""
    import jarvis.core.planner as planner_mod

    class _Settings:
        llm_enabled = True

    monkeypatch.setattr(planner_mod, "get_settings", lambda: _Settings())

    plan = planner.parse("what is the capital of France")
    # No ask_question action: the question is left for the model.
    assert [a.tool for a in plan.actions] == [], plan.actions
    assert plan.intent == "unknown", plan.intent


def test_general_question_uses_lookup_when_no_model(planner, monkeypatch):
    """Without a model, a general factual question still reaches the offline lookup."""
    import jarvis.core.planner as planner_mod

    class _Settings:
        llm_enabled = False

    monkeypatch.setattr(planner_mod, "get_settings", lambda: _Settings())

    plan = planner.parse("what is a train")
    assert [a.tool for a in plan.actions] == ["ask_question"], plan.actions


def test_schedule_meeting_routes_to_create_event_not_scheduler(planner):
    """Schedule a meeting/event/appointment must NOT be caught by the scheduler
    route. Without this gate, "schedule a meeting with Alice at 3pm" produced
    schedule_task or list_schedules instead of reaching _calendar."""
    for text in (
        "schedule a meeting with Alice tomorrow at 3pm",
        "book an appointment with the dentist",
    ):
        plan = planner.parse(text)
        tools = [a.tool for a in plan.actions]
        # Must NOT be schedule_task or list_schedules.
        assert "schedule_task" not in tools, f"{text!r} → {tools}"
        assert "list_schedules" not in tools, f"{text!r} → {tools}"
        # The intent must be calendar, not scheduler.
        assert plan.intent not in ("schedule_task", "list_schedules", "cancel_schedule",
                                    "pause_schedule", "resume_schedule"), f"{text!r} intent={plan.intent}"


def test_a_calendar_title_is_the_name_not_the_connective(planner):
    """The event title is what the person called it, not "called X" / "with Alice".

    The create-branch's first strip required the noun phrase to be present
    ("create a calendar event called Review"); the capture regex consumes an
    *optional* noun, so "schedule a meeting called LoopCheck" left "called
    LoopCheck", and "schedule a meeting with Alice" left "with Alice". The title
    must be just the name.
    """
    cases = {
        "schedule a meeting called LoopCheck tomorrow at 5pm": "LoopCheck",
        "book a meeting named Standup tomorrow at 9am": "Standup",
        "create a calendar event called Review on 2026-12-05 09:00": "Review",
        "create an event titled Sprint planning tomorrow at 3pm": "Sprint planning",
        # A person named in the request is the title, without the connective.
        "schedule a meeting with Alice tomorrow at 5pm": "Alice",
    }
    for text, expected in cases.items():
        plan = planner.parse(text)
        actions = [a for a in plan.actions if a.tool == "create_event"]
        assert actions, f"{text!r} did not create an event: {[a.tool for a in plan.actions]}"
        assert actions[0].params.get("title") == expected, (
            f"{text!r} -> title={actions[0].params.get('title')!r}, expected {expected!r}")


def test_a_calendar_request_with_no_name_still_asks(planner):
    """The connector-strip must not invent a title for a nameless request."""
    plan = planner.parse("schedule a meeting tomorrow at 5pm")
    assert not [a for a in plan.actions if a.tool == "create_event"], plan.actions
    assert "?" in (plan.clarification_question or ""), plan


def test_email_search_is_not_captured_by_file_search(planner):
    """Find/search emails must route to email tools, not search_files."""
    for text in (
        "Find emails about project proposal",
        "search email for the invoice",
        "search my email for the budget",
    ):
        plan = planner.parse(text)
        tools = [a.tool for a in plan.actions]
        assert "search_files" not in tools, f"{text!r} → {tools}"
        assert plan.intent not in ("search_files",), f"{text!r} intent={plan.intent}"


def test_planner_collision_matrix_no_domain_steals_from_another(planner):
    """No domain may capture a sentence that belongs to another domain.

    This guard pins the routing for the ambiguous sentences that previously
    collided (schedule+meeting → scheduler, find+emails → files). Each test
    sentence must route to the correct tool; every pair must produce different
    outcomes. A test that shows both routes yielding the SAME tool indicates
    a collision, not a fix.
    """
    MATRIX = {
        # --- calendar / scheduler boundary ---
        "schedule a meeting with Alice at 3pm": "create_event",   # was schedule_task
        "schedule a daily briefing":           "daily_briefing",  # briefing is scheduler
        "remind me every morning at 8":        "schedule_task",
        "what are my schedules":               "list_schedules",
        # --- email / file-search boundary ---
        "find emails about project proposal":  "search_email",    # was search_files
        "search email for the budget":         "search_email",
        "find my report":                      "search_files",
        "search for budget.xlsx":              "search_web",      # "search for" → web
        # --- email verbs that must reach email ---
        "read my email from Alice":            "search_email",
        "summarize my inbox":                  "summarize_email",
        "check my unread emails":              "search_email",
        # --- calendar verbs that must reach calendar ---
        "what do I have tomorrow":             "list_events",
        "create an event tomorrow at 5pm":     "create_event",
    }
    for text, expected in MATRIX.items():
        plan = planner.parse(text)
        tools = [a.tool for a in plan.actions]
        actual = tools[0] if tools else plan.intent
        assert actual == expected, f"{text!r} → {actual!r}, expected {expected!r}"

# ---------------------------------------------------------------------------
# "what's on my X" asks about X, not about the screen
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text,expected_tool",
    [
        # The regression: `_vision`'s gate read the bare phrase "what's on my"
        # as a screen reference, and it runs before the calendar, scheduler,
        # planner and clipboard rules -- so every one of these took a
        # screenshot and answered with screen text instead.
        ("what's on my calendar", "list_events"),
        ("what's on my schedule", "list_schedules"),
        ("what's on my plate", "plan_day"),
        ("what's on my clipboard", "read_clipboard"),
        ("what is on my clipboard", "read_clipboard"),
        ("whats on my clipboard", "read_clipboard"),
        ("what's in my clipboard", "read_clipboard"),
    ],
)
def test_whats_on_my_x_asks_about_x_not_the_screen(
    planner: RulePlanner, text: str, expected_tool: str
) -> None:
    plan = planner.parse(text)
    assert first_tool(plan) == expected_tool, f"{text!r} -> {first_tool(plan)!r}"


@pytest.mark.parametrize(
    "text",
    [
        "what's on my screen",
        "whats on my screen",
        "what's on the screen",
        "what's on my display",
        "what's on my desktop",
        "what's on my monitor",
        "what is on my screen right now",
    ],
)
def test_a_question_that_names_the_screen_still_reaches_vision(
    planner: RulePlanner, text: str
) -> None:
    """The guard must not have become "never look at the screen"."""
    plan = planner.parse(text)
    assert first_tool(plan) == "capture_screen", f"{text!r} -> {first_tool(plan)!r}"


def test_copying_to_my_clipboard_is_not_a_file_copy(planner: RulePlanner) -> None:
    """"my clipboard" is the same object as "the clipboard"."""
    plan = planner.parse("copy hello to my clipboard")
    assert first_tool(plan) == "write_clipboard", first_tool(plan)

    # A real file copy still reaches copy_file.
    assert first_tool(planner.parse("copy report.txt to /tmp")) == "copy_file"
