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


def test_the_swift_automation_kinds_match_the_recorder():
    """The macOS app offers exactly the kinds the server accepts.

    Two hand-written lists in two languages. A kind only in Swift is rejected
    at capture time, after the person has built the step; a kind only in Python
    cannot be authored at all. This is the drift that produces "the studio says
    it saved it but the draft came back empty".
    """
    import re
    from pathlib import Path

    from jarvis.core.recorder import SUPPORTED_KINDS

    swift = (Path(__file__).resolve().parents[1]
             / "macos" / "Vantage" / "Sources" / "APIClient" / "APIModels.swift")
    text = swift.read_text()
    block = re.search(r"static let names = \[(.*?)\]", text, re.S)
    assert block, "AutomationKinds.names is gone from APIModels.swift"
    offered = set(re.findall(r'"([a-z_]+)"', block.group(1)))

    assert offered == set(SUPPORTED_KINDS), (
        f"swift only: {sorted(offered - set(SUPPORTED_KINDS))}; "
        f"python only: {sorted(set(SUPPORTED_KINDS) - offered)}")


def test_every_offered_automation_kind_has_a_parameter_form_in_swift():
    """A selectable kind with no fields produces a step with nothing in it."""
    import re
    from pathlib import Path

    from jarvis.core.recorder import SUPPORTED_KINDS

    swift = (Path(__file__).resolve().parents[1]
             / "macos" / "Vantage" / "Sources" / "APIClient" / "APIModels.swift")
    text = swift.read_text()
    # Each kind must appear in a `case` of `fields(for:)`, either alone or in a
    # comma-separated list.
    # A `case` label list may wrap over several lines, so match the labels up to
    # the colon and flatten whitespace before splitting on commas.
    covered: set = set()
    for match in re.finditer(r"case\s+([^:]+?):\s*\n\s*return \[", text, re.S):
        labels = " ".join(match.group(1).split())
        covered |= {k.strip().strip('"') for k in labels.split(",") if k.strip()}
    missing = sorted(set(SUPPORTED_KINDS) - covered)
    assert not missing, f"these kinds have no field form: {missing}"


# -- a typed value is inspected, not just its parameter name -------------------

@pytest.mark.parametrize("text", [
    "password=hunter2",
    "token: abc123",
    "my api_key = sk-live-whatever",
    "cvv: 123",
])
def test_a_secret_pasted_into_a_typed_field_is_not_stored(recorder, text):
    """`desktop_type_text` types whatever is in `text`.

    The name-only check inspected the parameter *name* ("text"), found it
    innocent, and stored the value -- which would replay the secret on every run
    and leave it in the draft in plain text. Measured before the fix: a capture
    with `{"text": "password=hunter2"}` succeeded.
    """
    recorder.start()
    step = recorder.capture("desktop_type_text", {"text": text}, label="type")
    assert step.skipped is True
    assert step.params == {}, "the value reached the stored step"
    assert "sensitive" in (step.skip_reason or "")


@pytest.mark.parametrize("text", [
    "Hello from Vantage",
    "the token expired, please renew",
    "note: remember the meeting",
])
def test_ordinary_typed_text_is_still_recorded(recorder, text):
    """Mentioning a credential is not pasting one.

    A bare word match would refuse "the token expired" -- a document about a
    problem, not a secret -- and make the recorder useless for notes.
    """
    recorder.start()
    step = recorder.capture("desktop_type_text", {"text": text}, label="type")
    assert step.skipped is False
    assert step.params["text"] == text


def test_the_sensitive_value_never_appears_in_the_skip_reason(recorder):
    """The reason names the parameter, never the value."""
    recorder.start()
    step = recorder.capture("desktop_type_text", {"text": "password=hunter2"},
                            label="type")
    assert "hunter2" not in (step.skip_reason or "")


# -- a recorded point remembers which window it was aimed at ------------------
#
# A recording stores absolute screen coordinates, and nothing re-resolves them.
# Measured with a negative control: a recorded 3-step workflow replayed after its
# window moved clicked the old coordinate, the counter did not change, and every
# step still reported `done`. The executor already refuses a stale target when it
# is given `expect_window_id`; the recorder never captured one, so the guard could
# never fire. These pin the capture, the round trip and the hand-over.

@pytest.fixture()
def window(monkeypatch):
    """A deterministic window identity, so no test reads the real window list."""
    from jarvis.core import recorder as module

    def _set(value):
        monkeypatch.setattr(module, "target_window_id", lambda: value)

    return _set


def test_a_recorded_click_stores_the_window_it_was_aimed_at(recorder, window):
    window(4242)
    recorder.start()
    step = recorder.capture("desktop_click", {"x": 120, "y": 240}, label="press")

    assert step.window_id == 4242
    assert "expect_window_id" not in step.params, (
        "the window id is not a value the person supplied and must not be "
        "stored as a recorded parameter")


def test_a_caller_supplied_window_id_is_not_resolved_again(recorder, window):
    """A caller that already resolved the target passes it in; a second read
    could disagree with the first and record a window the person never aimed at."""
    def _refuse():  # pragma: no cover - failing here is the assertion
        raise AssertionError("the recorder resolved a window it was given")

    window(_refuse)
    recorder.start()
    step = recorder.capture("desktop_click", {"x": 1, "y": 2}, window_id=77)

    assert step.window_id == 77


def test_an_unreadable_window_list_records_the_step_without_a_guard(recorder, window):
    """Capture must survive a window list it cannot read.

    A recorder that refused to record because it could not name the window would
    lose the whole demonstration. Recording without the guard is the honest
    record of what the person did; refusing is not.
    """
    window(None)
    recorder.start()
    step = recorder.capture("desktop_click", {"x": 5, "y": 6}, label="press")
    stopped = recorder.stop()

    assert step.skipped is False
    assert step.window_id is None
    assert stopped.steps[0].params == {"x": 5, "y": 6}
    assert "expect_window_id" not in recorder.derive()["steps"][0]["params"]


def test_a_window_title_never_reaches_the_recording(recorder, window):
    """The id is opaque; a title is whatever the person has open.

    A window title can carry a document name, a subject line or a credential.
    Only the integer id may be stored.
    """
    window(9001)
    recorder.start()
    recorder.capture("desktop_scroll", {"x": 10, "y": 20}, label="scroll")
    dumped = str(recorder.stop().to_dict())

    assert "9001" in dumped
    assert "title" not in dumped


def test_a_window_id_round_trips_through_to_dict_and_load():
    """A stored recording loads with the identity it was captured with."""
    from jarvis.core.recorder import Recording

    stored = {
        "recording_id": "REC-1", "name": "demo",
        "started_at": 100.0, "stopped_at": 200.0, "open": False,
        "steps": [{"index": 0, "kind": "desktop_click",
                   "params": {"x": 1, "y": 2}, "label": "press", "at": 150.0,
                   "window_id": 4242, "skipped": False, "skip_reason": ""}],
    }
    loaded = Recording.from_dict(stored)

    assert loaded.steps[0].window_id == 4242
    assert loaded.to_dict()["steps"][0]["window_id"] == 4242
    assert loaded.steps[0].to_dict() == stored["steps"][0]


def test_an_old_recording_without_a_window_id_still_loads():
    """A recording written before window ids existed is still runnable.

    It must load as one that replays unguarded, not be refused: the whole
    demonstration is otherwise thrown away over a field it could not have had.
    """
    from jarvis.core.recorder import Recording

    loaded = Recording.from_dict({
        "recording_id": "REC-old",
        "steps": [{"index": 0, "kind": "desktop_click",
                   "params": {"x": 3, "y": 4}, "label": "press"}],
        "open": False,
    })

    assert loaded.steps[0].window_id is None
    assert loaded.steps[0].params == {"x": 3, "y": 4}


def test_derive_hands_the_window_id_to_the_tool_as_its_expectation(recorder, window):
    window(4242)
    recorder.start()
    recorder.capture("desktop_click", {"x": 120, "y": 240}, label="press")
    draft = recorder.derive(recorder.stop())

    # The recorder's params are untouched; the guard travels as the executor's
    # own parameter name, which is what `desktop_click` forwards.
    assert draft["steps"][0]["params"] == {
        "x": 120, "y": 240, "expect_window_id": 4242}


def test_derive_omits_the_expectation_when_no_window_was_recorded(recorder, window):
    window(None)
    recorder.start()
    recorder.capture("desktop_scroll", {"x": 10, "y": 20}, label="scroll")
    draft = recorder.derive(recorder.stop())

    assert draft["steps"][0]["params"] == {"x": 10, "y": 20}


def test_the_expectation_only_goes_to_the_tools_that_accept_it(recorder, window):
    """`expect_window_id` on a tool that does not declare it is refused by the
    permission engine, which is a step that cannot run at all -- worse than one
    that runs unguarded."""
    from jarvis.core.recorder import WINDOW_GUARDED_KINDS

    window(4242)
    recorder.start()
    for kind, params in (("desktop_click", {"x": 1, "y": 2}),
                         ("desktop_drag", {"from_x": 1, "from_y": 2,
                                           "to_x": 3, "to_y": 4}),
                         ("desktop_scroll", {"x": 1, "y": 2}),
                         ("desktop_right_click", {"x": 1, "y": 2}),
                         ("desktop_move_pointer", {"x": 1, "y": 2}),
                         ("desktop_double_click", {"x": 1, "y": 2})):
        recorder.capture(kind, params)
    steps = recorder.derive(recorder.stop())["steps"]

    for step in steps:
        guarded = "expect_window_id" in step["params"]
        assert guarded == (step["tool"] in WINDOW_GUARDED_KINDS), (
            f"{step['tool']} received expect_window_id={guarded}, which its tool "
            "does or does not accept")


def test_a_review_does_not_drop_the_window_guard(recorder, window):
    """The editor sends back the fields it displays, not the guard. A review that
    dropped it would re-arm the defect this exists to fix."""
    window(4242)
    recorder.start()
    recorder.capture("desktop_click", {"x": 120, "y": 240}, label="press")
    recorder.stop()

    reviewed = recorder.derive(reviewed=[{
        "index": 0, "title": "press", "tool": "desktop_click",
        "params": {"x": 130, "y": 250}}])

    # A review's values arrive as text (the editor holds text); what matters here
    # is that the guard survived it.
    assert reviewed["steps"][0]["params"] == {
        "x": "130", "y": "250", "expect_window_id": 4242}


def test_the_window_helper_reads_the_executor_window_list(monkeypatch):
    """The id comes from the executor's own window list -- the same source the
    staleness guard compares against, so capture and check cannot disagree."""
    from jarvis.core import recorder as module
    from jarvis.desktop import executor

    calls = []

    def fake_windows(application=""):
        calls.append(application)
        return {"status": "verified", "windows": [
            # A menu is first in the list while a menu is open. It is layer 1 and
            # is not what any document coordinate was aimed at.
            {"window_id": 11, "layer": 1, "on_screen": True, "title": "File"},
            {"window_id": 22, "layer": 0, "on_screen": True, "title": "Report"},
            {"window_id": 33, "layer": 0, "on_screen": True, "title": "Notes"},
        ]}

    monkeypatch.setattr(executor, "windows", fake_windows)
    assert module.target_window_id() == 22
    assert calls == [""]


@pytest.mark.parametrize("answer", [
    {"status": "permission_required", "reason": "no screen recording"},
    {"status": "failed", "reason": "no window list"},
    {"status": "verified", "windows": []},
    {"status": "verified", "windows": [{"window_id": "not-a-number", "layer": 0}]},
])
def test_the_window_helper_returns_none_rather_than_raising(monkeypatch, answer):
    from jarvis.core import recorder as module
    from jarvis.desktop import executor

    monkeypatch.setattr(executor, "windows", lambda application="": answer)
    assert module.target_window_id() is None


def test_the_window_helper_survives_an_executor_that_raises(monkeypatch):
    from jarvis.core import recorder as module
    from jarvis.desktop import executor

    def boom(application=""):
        raise OSError("the executor is not built")

    monkeypatch.setattr(executor, "windows", boom)
    assert module.target_window_id() is None
