"""The desktop planner: request text in, typed desktop operations out.

The tests pin one contract above all: the planner may leave a target unresolved
and ask, but it may never fill one in. A synthesised coordinate or accessibility
label would be acted on as if the person had said it, so "a semantic click with
no application" and "a corner with no point" are asserted to plan *nothing*.

Every tool name the planner emits is checked against the live registry, so a
typo here fails as a missing tool rather than as a runtime surprise.
"""

from __future__ import annotations

import pytest

from jarvis.core.orchestrator import JarvisCore
from jarvis.desktop.planner import DesktopPlan, PlannedStep, plan_desktop

_CORE = JarvisCore()
_TOOLS = set(_CORE.tools.names())


def plan(request: str, **context) -> DesktopPlan:
    return plan_desktop(request, context=context or None)


def only_step(result: DesktopPlan) -> PlannedStep:
    assert result.clarification is None, result.clarification
    assert len(result.steps) == 1, result.steps
    step = result.steps[0]
    assert step.tool in _TOOLS, f"{step.tool} is not in the registry"
    return step


# -- registry truth -----------------------------------------------------------

UTTERANCES = [
    "move the cursor to 400 300",
    "click at 400 300",
    "double click 400 300",
    "right click 400 300",
    "drag from 100 100 to 300 300",
    "scroll down 5 lines at 400 300",
    "type hello world",
    "press cmd s",
    "press enter",
    "focus Safari",
    "press the Save button in Safari",
    "read the UI in Safari",
]


@pytest.mark.parametrize("utterance", UTTERANCES)
def test_every_planned_tool_exists_in_the_registry(utterance):
    result = plan_desktop(utterance)
    assert result.clarification is None, result.clarification
    assert result.steps, f"{utterance!r} planned nothing"
    for step in result.steps:
        assert step.tool in _TOOLS, f"{step.tool} is not in the registry"
        assert step.description.strip()


# -- pointer ------------------------------------------------------------------

def test_a_named_point_moves_the_pointer():
    step = only_step(plan("move the cursor to 400 300"))
    assert step.tool == "desktop_move_pointer"
    assert step.params == {"x": 400.0, "y": 300.0}


def test_a_corner_is_a_question_not_a_guessed_point():
    """'top left' is not a screen position, and inventing one would move the
    cursor somewhere the person did not name."""
    result = plan("move my mouse to the top left")
    assert result.steps == []
    assert result.clarification and "point" in result.clarification.lower()


@pytest.mark.parametrize("utterance,tool", [
    ("click at 400 300", "desktop_click"),
    ("double click 400 300", "desktop_double_click"),
    ("right click 400 300", "desktop_right_click"),
])
def test_click_variants_map_to_their_own_tool(utterance, tool):
    step = only_step(plan(utterance))
    assert step.tool == tool
    assert (step.params["x"], step.params["y"]) == (400.0, 300.0)


def test_a_drag_is_one_step_with_all_four_points():
    step = only_step(plan("drag from 100 100 to 300 300"))
    assert step.tool == "desktop_drag"
    assert step.params == {"from_x": 100.0, "from_y": 100.0,
                           "to_x": 300.0, "to_y": 300.0}


# -- out-of-range numbers -----------------------------------------------------

@pytest.mark.parametrize("utterance", [
    "move the cursor to 40000 300",
    "click at 400 32001",
    "drag from -1 100 to 300 300",
])
def test_a_point_outside_the_range_is_refused_with_no_steps(utterance):
    result = plan_desktop(utterance)
    assert result.steps == []
    assert result.clarification and "32000" in result.clarification


# -- scrolling ----------------------------------------------------------------

@pytest.mark.parametrize("utterance,lines", [
    ("scroll down 5 lines at 400 300", -5),
    ("scroll up 3 at 400 300", 3),
])
def test_a_vertical_scroll_carries_a_signed_line_count(utterance, lines):
    step = only_step(plan(utterance))
    assert step.tool == "desktop_scroll"
    assert step.params["lines"] == lines
    assert step.params["horizontal"] is False


def test_a_scroll_without_a_point_asks_where():
    result = plan("scroll down 5 lines")
    assert result.steps == []
    assert "where" in (result.clarification or "").lower()


# -- keyboard -----------------------------------------------------------------

@pytest.mark.parametrize("utterance,expected", [
    ("type Hello, world!  two  spaces", "Hello, world!  two  spaces"),
    ("type 'hello world'", "hello world"),
])
def test_typing_preserves_the_exact_text_without_surrounding_quotes(utterance, expected):
    step = only_step(plan(utterance))
    assert step.tool == "desktop_type_text"
    assert step.params["text"] == expected


def test_a_modifier_makes_a_shortcut():
    step = only_step(plan("press cmd s"))
    assert step.tool == "desktop_shortcut"
    assert step.params == {"key": "s", "modifiers": ["cmd"]}


def test_spelled_out_modifiers_are_canonicalised():
    step = only_step(plan("press command shift 4"))
    assert step.tool == "desktop_shortcut"
    assert step.params == {"key": "4", "modifiers": ["cmd", "shift"]}


def test_a_bare_key_is_a_keystroke_not_a_shortcut():
    step = only_step(plan("press enter"))
    assert step.tool == "desktop_press_key"
    assert step.params == {"key": "enter", "modifiers": []}


# -- windows and accessibility ------------------------------------------------

@pytest.mark.parametrize("utterance", ["focus Safari", "switch to TextEdit"])
def test_focusing_names_the_application(utterance):
    step = only_step(plan(utterance))
    assert step.tool == "desktop_focus_window"
    assert step.params["application"] in {"Safari", "TextEdit"}


def test_a_named_element_is_pressed_by_its_label():
    step = only_step(plan("press the Save button in Safari"))
    assert step.tool == "desktop_press_element"
    assert step.params["keyword"] == "Save"
    assert step.params["application"] == "Safari"


def test_a_semantic_click_with_no_application_asks_which_one():
    result = plan("click the Save button")
    assert result.steps == []
    assert "application" in (result.clarification or "").lower()


def test_context_supplies_the_missing_application():
    step = only_step(plan("click the Save button", application="Notes"))
    assert step.tool == "desktop_press_element"
    assert step.params == {"application": "Notes", "keyword": "Save"}


def test_a_semantic_click_never_invents_an_identifier():
    """The element is named by a human label; the executor resolves it."""
    step = only_step(plan("press the Save button in Safari"))
    forbidden = {"identifier", "index", "element_index", "ax_identifier"}
    assert not forbidden & set(step.params)


def test_reading_the_ui_names_the_application():
    step = only_step(plan("read the UI in Safari"))
    assert step.tool == "desktop_inspect_ui"
    assert step.params["application"] == "Safari"


# -- the planner always answers something -------------------------------------

@pytest.mark.parametrize("utterance", ["make me a sandwich", "", "click somewhere"])
def test_unrecognised_input_is_a_question_not_a_silent_empty_plan(utterance):
    result = plan_desktop(utterance)
    assert result.steps == []
    assert result.clarification and result.clarification.strip()

# -- two instructions in one sentence must not become an invented app ---------

@pytest.mark.parametrize("utterance", [
    "focus Safari and click Save",
    "switch to TextEdit then type hello",
    "activate Mail and press the Send button",
])
def test_a_chained_instruction_does_not_become_an_application_name(utterance):
    """The focus regex is non-greedy over `.+`, so it will happily hand the whole
    tail of a compound sentence to the executor as an app name. Measured: "focus
    Safari and click Save" planned a focus of "Safari and click Save" with no
    clarification -- an invented target, which is the one thing this module must
    never do."""
    result = plan_desktop(utterance)
    assert result.steps == [], result.steps
    assert result.clarification


def test_reading_the_ui_also_refuses_a_chained_tail():
    result = plan_desktop("read the UI in Safari and delete everything")
    assert result.steps == []
    assert result.clarification


@pytest.mark.parametrize("name", ["Safari", "TextEdit", "Visual Studio Code",
                                  "Activity Monitor", "Google Chrome"])
def test_a_real_multi_word_application_name_is_still_accepted(name):
    result = plan_desktop(f"focus {name}")
    step = only_step(result)
    assert step.tool == "desktop_focus_window"
    assert step.params["application"] == name
