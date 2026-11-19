"""A command can run as a real mission, and the mission records the check.

Before: /command ran the actions inline and wrote the mission as a ledger stub --
step "pending", verdict "unknown" -- so the record could not answer "did it
work?". After (flag on): the same command creates the same typed mission and
hands it to MissionRunner, which records the step and verifies the condition
against the real process table.

These tests cover the parts that must hold whatever the machine is doing: which
conditions get emitted, that an unobservable action gets none rather than a
check nobody can satisfy, and that the flag is what selects the path.
"""

from __future__ import annotations

import json

import pytest

from jarvis.config import Settings
from jarvis.core.models import PlannedAction
from jarvis.core.voice_mission import (attach_conditions, conditions_for,
                                       safe_slug, steps_for)


def _action(tool, **params):
    return PlannedAction(tool=tool, params=params)


# --- which conditions are emitted ----------------------------------------
def test_open_emits_a_presence_check():
    conds = conditions_for(_action("open_application", app="Calculator"))
    assert len(conds) == 1
    assert conds[0]["verifier"]["kind"] == "process_running"
    assert conds[0]["verifier"]["process"] == "Calculator"
    assert conds[0]["verifier"].get("absent") is None


def test_close_emits_an_absence_check_with_a_grace_window():
    conds = conditions_for(_action("close_app", app="Calculator"))
    assert len(conds) == 1
    verifier = conds[0]["verifier"]
    assert verifier["absent"] is True
    assert verifier["grace"] > 0, (
        "a quit is asynchronous; without a grace window the check races it")


def test_app_suffix_is_trimmed_for_the_process_name():
    conds = conditions_for(_action("open_application", app="Calculator.app"))
    assert conds[0]["verifier"]["process"] == "Calculator"


def test_an_unobservable_tool_gets_no_condition():
    """No observer means no condition -- not a condition that can never pass.

    `open_document` takes an ``app`` parameter, so it exercises the *observer*
    guard rather than the no-app guard. A test using a tool with no ``app`` at
    all would still pass with the observer guard removed, which is how the first
    version of this test failed to catch that mutation.
    """
    assert conditions_for(_action("open_document", app="Preview", path="/tmp/x")) == []
    assert conditions_for(_action("list_files")) == []


def test_naming_no_app_gets_no_condition():
    assert conditions_for(_action("open_application")) == []
    assert conditions_for(_action("close_app", app="   ")) == []


def test_only_kinds_the_build_supports_are_emitted():
    """A verifier kind the runner does not know would record UNKNOWN forever."""
    from jarvis.core.outcome_assurance import KINDS

    for action in (_action("open_application", app="X"), _action("close_app", app="X")):
        for cond in conditions_for(action):
            assert cond["verifier"]["kind"] in KINDS


# --- steps and their join to conditions ----------------------------------
def test_steps_carry_the_action_params():
    steps = steps_for([_action("open_application", app="Calculator")])
    assert steps[0]["tool"] == "open_application"
    assert steps[0]["params"] == {"app": "Calculator"}


def test_a_step_is_joined_to_its_condition():
    steps = steps_for([_action("open_application", app="Calculator")])
    attach_conditions(steps, conditions_for(_action("open_application", app="Calculator")))
    assert steps[0]["satisfies"] == "S1", (
        "without the join the condition is recorded but never checked")
    assert "_satisfies" not in steps[0], "the private key must not reach create()"


def test_an_unobservable_step_is_joined_to_nothing():
    steps = steps_for([_action("list_files")])
    attach_conditions(steps, [])
    assert steps[0].get("satisfies", "") == ""


# --- the request string --------------------------------------------------
def test_safe_slug_is_bounded_and_single_line():
    assert safe_slug("  open   Calculator \n now ") == "open Calculator now"
    assert len(safe_slug("x" * 500)) <= 60
    assert safe_slug("") == "voice command"


# --- the flag ------------------------------------------------------------
def test_mission_mode_is_off_by_default():
    assert Settings().command_mission_mode is False


def test_turning_it_off_keeps_the_inline_path(monkeypatch):
    """The flag is read from settings at the call site; the default is inline."""
    import inspect

    from jarvis.core import orchestrator

    source = inspect.getsource(orchestrator.JarvisCore._execute_plan)
    assert "command_mission_mode" in source, (
        "the inline path no longer consults the flag")
    assert "if diverted is not None" in source, (
        "a failed mission must fall back to the inline path, not report success")


def test_stored_condition_verifier_is_json_text():
    """create() stores verifier as text; the runner parses JSON back out."""
    from jarvis.core.missions import MissionService

    conds = conditions_for(_action("open_application", app="Calculator"))
    stored = [{**c, "verifier": json.dumps(c["verifier"])} for c in conds]
    from jarvis.core.mission_runner import _verifier_from_spec
    rebuilt = _verifier_from_spec(stored[0]["verifier"], "S1")
    assert rebuilt is not None, "the stored spec did not round-trip to a verifier"