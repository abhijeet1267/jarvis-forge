"""V2 end-to-end: a pronoun resolves only from a VERIFIED observation.

Drives the real ``JarvisCore`` and its real entity memory. The flag is exercised
by setting it on the instance's settings, which is the same object the
orchestrator reads at the two call sites.
"""

from __future__ import annotations

import pytest

from jarvis.config import Settings
from jarvis.core.models import CommandRequest, PlannedAction
from jarvis.core.orchestrator import JarvisCore
from jarvis.tools.base import fail, ok, unverified


@pytest.fixture
def core():
    return JarvisCore()


def _request(text: str = "close it", session: str = "s1") -> CommandRequest:
    return CommandRequest(text=text, session_id=session)


def test_flag_defaults_off():
    """The shipped default must be the old behaviour."""
    assert Settings().voice_operator_v2_enabled is False


def test_memory_exists_on_the_core(core):
    assert core.entity_memory is not None


def test_verified_open_is_remembered(core):
    core.settings.voice_operator_v2_enabled = True
    action = PlannedAction(tool="open_application", params={"app": "Calculator"})
    result = ok("open_application", "Calculator is open on your mac machine.",
                display_name="Calculator", verified=True)
    core._remember_verified_entity(action, result, _request("open Calculator"))

    from jarvis.voice.resolve import describe_resolution

    info = describe_resolution("close it", core.entity_memory, "s1", kind="app")
    assert info["resolved"] == "close Calculator"


def test_unverified_open_is_not_remembered(core):
    """A handler that could not confirm the app must not seed a later target."""
    core.settings.voice_operator_v2_enabled = True
    action = PlannedAction(tool="open_application", params={"app": "Calculator"})
    result = unverified("open_application", "told to open, could not confirm",
                        display_name="Calculator")
    core._remember_verified_entity(action, result, _request("open Calculator"))

    from jarvis.voice.resolve import describe_resolution

    assert describe_resolution("close it", core.entity_memory, "s1", kind="app")["resolved"] is None


def test_failed_open_is_not_remembered(core):
    core.settings.voice_operator_v2_enabled = True
    action = PlannedAction(tool="open_application", params={"app": "Calculator"})
    result = fail("open_application", "not installed", display_name="Calculator")
    core._remember_verified_entity(action, result, _request("open Calculator"))

    from jarvis.voice.resolve import describe_resolution

    assert describe_resolution("close it", core.entity_memory, "s1", kind="app")["resolved"] is None


def test_other_tools_are_not_recorded_as_apps(core):
    core.settings.voice_operator_v2_enabled = True
    action = PlannedAction(tool="list_files", params={})
    result = ok("list_files", "listed", display_name="Whatever", verified=True)
    core._remember_verified_entity(action, result, _request())

    from jarvis.voice.resolve import describe_resolution

    assert describe_resolution("close it", core.entity_memory, "s1", kind="app")["resolved"] is None


def test_session_isolation(core):
    core.settings.voice_operator_v2_enabled = True
    action = PlannedAction(tool="open_application", params={"app": "Calculator"})
    result = ok("open_application", "open", display_name="Calculator", verified=True)
    core._remember_verified_entity(action, result, _request(session="s1"))

    from jarvis.voice.resolve import describe_resolution

    assert describe_resolution("close it", core.entity_memory, "s2", kind="app")["resolved"] is None


def test_memory_fault_never_breaks_a_command(core):
    """A memory failure must not turn a successful command into a failure."""
    core.settings.voice_operator_v2_enabled = True

    class Boom:
        def record(self, *a, **k):
            raise RuntimeError("memory is broken")

    core.entity_memory = Boom()
    action = PlannedAction(tool="open_application", params={"app": "Calculator"})
    result = ok("open_application", "opened", display_name="Calculator", verified=True)
    core._remember_verified_entity(action, result, _request())  # must not raise


def test_pronoun_refusal_text_is_still_present_in_planner():
    """The refusal this feature extends must not have been deleted."""
    from jarvis.core import planner as planner_module

    with open(planner_module.__file__, "r", encoding="utf-8") as handle:
        text = handle.read()
    assert "Never infer a broad target from a vague pronoun" in text