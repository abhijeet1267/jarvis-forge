"""Demonstrate-to-Automate: explicit capture, refused secrets, reviewable draft.

The values here are the ones that make the feature trustworthy rather than a
click recorder: nothing is captured without an open recording, a sensitive value
is never stored, a draft is unverified until replayed, and a discarded recording
leaves nothing behind.
"""

from __future__ import annotations

import pytest

from jarvis.core.recorder import Recorder, RecordingError


@pytest.fixture()
def recorder(tmp_path):
    return Recorder(path=tmp_path / "recordings.json")


# -- explicit capture -------------------------------------------------------

def test_capturing_without_a_recording_is_refused(recorder):
    with pytest.raises(RecordingError) as excinfo:
        recorder.capture("write_file", {"path": "/tmp/x", "content": "y"})
    assert "no recording is open" in str(excinfo.value)


def test_a_recording_captures_typed_steps_and_stops(recorder):
    recorder.start("tidy the notes")
    recorder.capture("write_file", {"path": "/tmp/a.md", "content": "one"}, label="write a")
    recorder.capture("write_file", {"path": "/tmp/a.md", "content": "two",
                                    "mode": "append"}, label="append")

    stopped = recorder.stop()
    assert stopped.open is False
    assert [s.kind for s in stopped.steps] == ["write_file", "write_file"]
    assert stopped.stopped_at >= stopped.started_at


def test_capturing_after_stop_is_refused(recorder):
    recorder.start()
    recorder.stop()
    with pytest.raises(RecordingError) as excinfo:
        recorder.capture("write_file", {"path": "/tmp/x", "content": "y"})
    assert "has stopped" in str(excinfo.value)


def test_only_one_recording_may_be_open(recorder):
    recorder.start("first")
    with pytest.raises(RecordingError) as excinfo:
        recorder.start("second")
    assert "already open" in str(excinfo.value)


def test_an_unknown_action_kind_is_refused(recorder):
    """A step that cannot be replayed must not be recorded as if it could."""
    recorder.start()
    with pytest.raises(RecordingError) as excinfo:
        recorder.capture("move_mouse", {"x": 100, "y": 200})
    assert "not an action this recorder can replay" in str(excinfo.value)


# -- secrets are never captured --------------------------------------------

def test_a_password_parameter_is_refused_and_its_value_never_stored(recorder):
    recorder.start("sign in")
    step = recorder.capture("run_allowlisted_command",
                            {"command": "login", "password": "hunter2"})

    assert step.skipped is True
    assert "password" in step.skip_reason
    # The value appears nowhere in the recording.
    dumped = str(recorder.stop().to_dict())
    assert "hunter2" not in dumped, "a sensitive value reached the recording"


def test_every_sensitive_hint_refuses_the_step(recorder):
    recorder.start()
    for key in ("api_key", "otp", "card_number", "cvv", "token"):
        step = recorder.capture("open_url", {"url": "https://x", key: "v"})
        assert step.skipped is True, key


def test_a_skipped_step_still_appears_for_the_reviewer(recorder):
    """The person must know something happened there, without seeing the value."""
    recorder.start()
    recorder.capture("run_allowlisted_command",
                     {"command": "login", "password": "secret"})
    recorder.stop()

    draft = recorder.derive()
    assert draft["needs_input"] == 1
    assert any("needs input" in s["title"] for s in draft["steps"])
    assert "secret" not in str(draft)


# -- deriving a reviewable draft -------------------------------------------

def test_a_draft_is_unverified_until_it_is_replayed(recorder):
    recorder.start("demo")
    recorder.capture("write_file", {"path": "/tmp/a.md", "content": "x"})
    recorder.stop()

    draft = recorder.derive()
    assert draft["verdict"] == "unverified", (
        "a demonstration proves what was done, not that doing it again works")
    assert draft["review_required"] is True


def test_capabilities_and_scope_come_from_what_was_actually_captured(recorder):
    recorder.start()
    recorder.capture("write_file", {"path": "/tmp/proj/a.md", "content": "x"})
    recorder.capture("read_file", {"path": "/tmp/proj/b.md"})
    recorder.stop()

    draft = recorder.derive()
    assert sorted(draft["required_capabilities"]) == ["read_file", "write_file"]
    assert sorted(draft["required_scope"]) == ["/tmp/proj/a.md", "/tmp/proj/b.md"]


def test_deriving_an_open_recording_is_refused(recorder):
    recorder.start()
    with pytest.raises(RecordingError) as excinfo:
        recorder.derive()
    assert "still open" in str(excinfo.value)


def test_deriving_with_no_recording_is_refused(recorder):
    with pytest.raises(RecordingError) as excinfo:
        recorder.derive()
    assert "no recording" in str(excinfo.value)


def test_discard_leaves_nothing_behind(recorder, tmp_path):
    recorder.start("throwaway")
    recorder.capture("write_file", {"path": "/tmp/x", "content": "y"})
    assert recorder.discard() is True
    assert recorder.current() is None
    with pytest.raises(RecordingError):
        recorder.derive()
    assert not (tmp_path / "recordings.json").exists(), (
        "a discarded recording must not leave a file")


def test_the_draft_shape_matches_a_mission_step(recorder):
    """The draft must be replayable by the mission runner, not a parallel format."""
    recorder.start()
    recorder.capture("write_file", {"path": "/tmp/a.md", "content": "x"})
    recorder.stop()

    draft = recorder.derive()
    step = draft["steps"][0]
    assert set(step) >= {"title", "tool", "params"}
    assert isinstance(step["params"], dict)

# -- every kind must be a tool that actually exists ------------------------

def test_every_supported_kind_is_a_registered_tool():
    """A kind the recorder accepts must be dispatchable, or its draft cannot run.

    This is the check the recorder lacked: it listed `append_file`,
    `replace_text`, `open_app`, `run_command` and `http_request`, none of which
    is a tool. A draft built from them looked complete and failed on its first
    replay step with "no agent owns ...". Recording a step that cannot be
    replayed is worse than refusing it, because the person reviews and approves
    something that was never going to work.
    """
    from jarvis.core.recorder import SUPPORTED_KINDS

    from jarvis.api.app import get_core

    core = get_core()
    names = set(core.tools.names())
    missing = [kind for kind in SUPPORTED_KINDS if kind not in names]
    assert not missing, (
        f"these recorded kinds have no registered tool, so a draft using them "
        f"cannot be replayed: {missing}")


def test_every_supported_kind_has_an_owning_agent():
    """Registration is not enough -- the mission executor dispatches by agent."""
    from jarvis.agents.base import find_agent
    from jarvis.core.recorder import SUPPORTED_KINDS

    from jarvis.api.app import get_core

    agents = get_core().agents
    orphans = [kind for kind in SUPPORTED_KINDS if find_agent(agents, kind) is None]
    assert not orphans, (
        f"these recorded kinds resolve to no agent, so a mission step using "
        f"them fails with 'no agent owns ...': {orphans}")
