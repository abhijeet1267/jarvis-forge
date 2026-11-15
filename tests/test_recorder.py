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


def test_the_recorder_knows_every_required_parameter_the_registry_does():
    """The recorder's required-parameter mirror must match the tool registry.

    `RECORDER.REQUIRED_PARAMETERS` is written out rather than imported so the
    recorder stays constructible without building the whole tool catalog. A
    mirror that drifts is worse than no mirror: the draft would report a step as
    complete, and the executor would refuse it with "missing required parameter".
    This test is what keeps the two in step.
    """
    from jarvis.core.recorder import REQUIRED_PARAMETERS, SUPPORTED_KINDS

    from jarvis.api.app import get_core

    tools = get_core().tools
    for kind in SUPPORTED_KINDS:
        spec = tools.get(kind)
        assert spec is not None, f"{kind} is not a registered tool"
        assert set(spec.required_parameters) == set(REQUIRED_PARAMETERS[kind]), (
            f"{kind}: registry requires {sorted(spec.required_parameters)}, "
            f"recorder believes {sorted(REQUIRED_PARAMETERS[kind])}")


def test_a_step_missing_a_required_parameter_is_reported_as_needing_input(recorder):
    """A captured step that cannot replay must not review as complete.

    Capturing `copy_file` with only a source produced a draft that said
    "0 needing input" and then failed at replay with "missing required
    parameter 'destination'". The person reviews the draft to decide whether it
    is safe to run; that review is worthless if the draft hides an unfillable
    step.
    """
    recorder.start()
    recorder.capture("copy_file", {"source": "/tmp/a.txt"}, label="Copy the archive")
    stopped = recorder.stop()
    draft = recorder.derive(stopped)

    assert draft["needs_input"] == 1
    step = draft["steps"][0]
    assert step["needs_input"], "the missing parameter is not named"
    assert "destination" in step["needs_input"]
    assert "0 needing input" not in draft["notes"]


def test_missing_parameters_reports_exactly_the_absent_ones():
    """The check itself, without a recording: absent and empty both count."""
    from jarvis.core.recorder import missing_parameters

    assert missing_parameters("copy_file", {"source": "/a"}) == ["destination"]
    assert missing_parameters("copy_file", {"source": "/a", "destination": ""}) == ["destination"]
    assert missing_parameters("copy_file", {"source": "/a", "destination": "/b"}) == []
    assert missing_parameters("write_file", {"path": "/a", "content": "x"}) == []
    assert missing_parameters("not_a_tool", {}) == []

def test_an_optional_tool_default_does_not_make_a_step_incomplete(recorder):
    """A step the tool can run as captured must review as runnable.

    `write_file` declares `mode` in its parameter schema but does not require
    it: the tool applies `overwrite` when it is absent. The native editor read
    the schema's declared fields as required, so a demonstration that never
    chose a mode showed "needs mode" beside a picker already displaying
    "overwrite" -- asking for a value that existed. Whether a field is required
    is the registry's answer, and the draft must give the same one.
    """
    from jarvis.api.app import get_core

    recorder.start()
    recorder.capture("write_file", {"path": "/tmp/report.md", "content": "# hi\n"},
                     label="Write the report")
    draft = recorder.derive(recorder.stop())

    assert draft["needs_input"] == 0
    assert "needs_input" not in draft["steps"][0]

    # And the tool really does accept it: the optional parameter is genuinely
    # optional, so this is a fact about the tool rather than a lenient check.
    spec = get_core().tools.get("write_file")
    assert "mode" in spec.parameters, "mode should still be offered"
    assert "mode" not in spec.required_parameters, "mode must not be required"

def test_a_write_and_copy_produce_conditions_that_can_actually_be_checked(recorder):
    """The draft must name outcomes an observer can re-read, not just steps.

    A recorded automation used to reach the runner with no acceptance
    conditions, so `Mission.verdict()` had nothing to check and every replay
    finished `partially_completed` no matter what it did. The recorder knows
    what each step was asked to produce, so it can state that as a condition --
    as long as the condition is one an independent observer can check.
    """
    recorder.start()
    recorder.capture("write_file", {"path": "/tmp/report.md", "content": "# hi\n"},
                     label="Write the report")
    recorder.capture("copy_file", {"source": "/tmp/report.md",
                                   "destination": "/tmp/report-copy.md"},
                     label="Copy it")
    draft = recorder.derive(recorder.stop())

    by_kind = {c["verifier"]["kind"]: c for c in draft["conditions"]}
    assert set(by_kind) == {"file_contains", "file_identical"}
    assert by_kind["file_contains"]["verifier"]["path"] == "/tmp/report.md"
    assert by_kind["file_contains"]["verifier"]["needle"] == "# hi\n"
    assert by_kind["file_identical"]["verifier"]["source"] == "/tmp/report.md"

    # Every condition is attached to the step that claims to establish it, and
    # no step claims more than one.
    claimed = [s["satisfies"] for s in draft["steps"] if s.get("satisfies")]
    assert sorted(claimed) == sorted(c["condition_id"] for c in draft["conditions"])


def test_no_condition_is_invented_for_a_step_nothing_can_check(recorder):
    """A step whose effect does not persist gets no condition.

    `open_url` opens something; nothing on disk afterwards says whether it
    worked. Claiming a condition for it would either pass vacuously or fail the
    mission for something nobody can observe.
    """
    recorder.start()
    recorder.capture("open_url", {"url": "https://example.com"}, label="Open it")
    draft = recorder.derive(recorder.stop())

    assert draft["conditions"] == []
    assert "satisfies" not in draft["steps"][0]
    assert "0 checkable outcome(s)" in draft["notes"]


def test_an_incomplete_step_offers_no_condition(recorder):
    """A step that cannot run establishes nothing, so it claims nothing."""
    recorder.start()
    recorder.capture("copy_file", {"source": "/tmp/a.txt"}, label="Copy it")
    draft = recorder.derive(recorder.stop())

    assert draft["conditions"] == []
    assert draft["needs_input"] == 1


def test_empty_content_offers_no_condition(recorder):
    """Empty content would make "contains" vacuously true, proving nothing."""
    from jarvis.core.recorder import _acceptance_for

    assert _acceptance_for("write_file", {"path": "/tmp/a", "content": ""}) is None
    assert _acceptance_for("write_file", {"path": "", "content": "x"}) is None
    assert _acceptance_for("move_file", {"source": "/a", "destination": "/b"}) is None

def test_a_value_supplied_in_review_can_create_a_checkable_outcome(recorder):
    """Filling in a missing parameter must also establish what to verify.

    A `copy_file` captured without a destination has no checkable outcome, so the
    first draft offers no condition. Once the person supplies the destination,
    there *is* something to check -- the destination holds the source's bytes --
    and the replay must be verified against it. Re-deriving from the raw
    recording instead left the step with no condition, so every such replay
    finished unverified however well it worked.
    """
    recorder.start()
    recorder.capture("copy_file", {"source": "/tmp/a.txt"}, label="Copy it")
    recorder.stop()

    before = recorder.derive()
    assert before["conditions"] == []
    assert before["needs_input"] == 1

    after = recorder.derive(reviewed=[{
        "index": 0, "tool": "copy_file", "title": "Copy it",
        "params": {"source": "/tmp/a.txt", "destination": "/tmp/b.txt"}}])

    assert after["needs_input"] == 0
    assert [c["verifier"]["kind"] for c in after["conditions"]] == ["file_identical"]
    assert after["steps"][0]["satisfies"] == after["conditions"][0]["condition_id"]


def test_review_keeps_recorded_values_it_does_not_mention(recorder):
    """A correction is a correction, not a replacement of the whole step."""
    recorder.start()
    recorder.capture("write_file", {"path": "/tmp/a", "content": "original"},
                     label="Write it")
    recorder.stop()

    draft = recorder.derive(reviewed=[{"index": 0, "params": {"content": "edited"}}])
    assert draft["steps"][0]["params"] == {"path": "/tmp/a", "content": "edited"}
    # The condition follows the value, so it checks what will actually be written.
    assert draft["conditions"][0]["verifier"]["needle"] == "edited"


def test_review_honours_the_order_the_person_chose(recorder):
    """Reordering in the editor must be the order the replay runs in."""
    recorder.start()
    recorder.capture("read_file", {"path": "/tmp/a"}, label="First")
    recorder.capture("read_file", {"path": "/tmp/b"}, label="Second")
    recorder.stop()

    draft = recorder.derive(reviewed=[{"index": 1, "params": {"path": "/tmp/b"}},
                                      {"index": 0, "params": {"path": "/tmp/a"}}])
    assert [s["title"] for s in draft["steps"]] == ["Second", "First"]
    assert [s["params"]["path"] for s in draft["steps"]] == ["/tmp/b", "/tmp/a"]


def test_a_review_that_does_not_cover_every_step_is_refused(recorder):
    """Partial reviews are refused rather than guessed at."""
    from jarvis.core.recorder import RecordingError

    recorder.start()
    recorder.capture("read_file", {"path": "/tmp/a"}, label="First")
    recorder.capture("read_file", {"path": "/tmp/b"}, label="Second")
    recorder.stop()

    with pytest.raises(RecordingError) as exc:
        recorder.derive(reviewed=[{"index": 0, "params": {"path": "/tmp/a"}}])
    assert "cover every step" in str(exc.value)


def test_a_review_cannot_change_what_was_demonstrated(recorder):
    """A review supplies values and order, not a different action."""
    from jarvis.core.recorder import RecordingError

    recorder.start()
    recorder.capture("read_file", {"path": "/tmp/a"}, label="Read it")
    recorder.stop()

    with pytest.raises(RecordingError) as exc:
        recorder.derive(reviewed=[{"index": 0, "tool": "delete_file",
                                   "params": {"path": "/tmp/a"}}])
    assert "not replace what was demonstrated" in str(exc.value)


def test_a_step_that_becomes_incomplete_in_review_loses_its_claim(recorder):
    """Clearing a reviewed value must not leave a condition behind."""
    recorder.start()
    recorder.capture("copy_file", {"source": "/tmp/a", "destination": "/tmp/b"},
                     label="Copy it")
    recorder.stop()

    draft = recorder.derive(reviewed=[{
        "index": 0, "params": {"source": "/tmp/a", "destination": ""}}])
    assert draft["conditions"] == []
    assert "satisfies" not in draft["steps"][0]
    assert draft["needs_input"] == 1
