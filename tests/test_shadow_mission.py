"""A shadow predicts; it does not act.

The tests that matter here are not "does the preview look right" but the two
claims the feature makes: a shadow performs no effect at all, and it says
"unknown" rather than guessing where it cannot see.
"""

from __future__ import annotations

import hashlib

import pytest

from jarvis.core.shadow import ShadowMission


def _allow(tool, params, device, session_id):
    return {"allowed": True, "decision": "EXECUTE", "reason": "allowlisted"}


def _confirm(tool, params, device, session_id):
    return {"allowed": False, "decision": "CONFIRM",
            "reason": "level 2 (external) action requires confirmation"}


def _block(tool, params, device, session_id):
    return {"allowed": False, "decision": "BLOCK", "reason": "protected path"}


def test_a_shadow_writes_nothing(workspace):
    """The central claim: previewing a plan must leave the disk untouched."""
    target = workspace / "out.txt"
    shadow = ShadowMission()
    report = shadow.run(
        [{"step_id": "S1", "tool": "write_file",
          "params": {"path": str(target), "content": "hello"}}],
        gate=_allow)

    assert report["steps"][0]["verdict"] == "would_run"
    assert report["steps"][0]["after"] == "hello"
    assert not target.exists(), "the shadow wrote the file it was only previewing"


def test_the_before_and_after_come_from_the_real_file(workspace):
    target = workspace / "note.txt"
    target.write_text("original", encoding="utf-8")

    report = ShadowMission().run(
        [{"step_id": "S1", "tool": "write_file",
          "params": {"path": str(target), "content": "replacement"}}],
        gate=_allow)

    step = report["steps"][0]
    assert step["before"] == "original"
    assert step["after"] == "replacement"
    assert target.read_text(encoding="utf-8") == "original"


def test_a_second_write_sees_the_first_writes_result(workspace):
    """Steps are rolled forward, so step 2's "before" is step 1's "after"."""
    target = workspace / "note.txt"
    target.write_text("v0", encoding="utf-8")

    report = ShadowMission().run(
        [{"step_id": "S1", "tool": "write_file",
          "params": {"path": str(target), "content": "v1"}},
         {"step_id": "S2", "tool": "write_file",
          "params": {"path": str(target), "content": "v2", "mode": "append"}}],
        gate=_allow)

    assert report["steps"][0]["after"] == "v1"
    assert report["steps"][1]["before"] == "v1"
    assert report["steps"][1]["after"] == "v1v2"
    # And the roll-forward is a prediction, said plainly.
    assert report["steps"][1]["source_is_shadowed"] is True
    assert target.read_text(encoding="utf-8") == "v0"


def test_a_refused_step_is_reported_and_changes_nothing(workspace):
    target = workspace / "note.txt"
    target.write_text("v0", encoding="utf-8")

    report = ShadowMission().run(
        [{"step_id": "S1", "tool": "write_file",
          "params": {"path": str(target), "content": "v1"}}],
        gate=_block)

    assert report["steps"][0]["verdict"] == "would_be_refused"
    assert report["summary"]["would_be_refused"] == 1
    assert "would be refused" in report["conclusion"]


def test_a_step_needing_approval_is_distinguished_from_a_refusal(workspace):
    report = ShadowMission().run(
        [{"step_id": "S1", "tool": "write_file",
          "params": {"path": str(workspace / "a.txt"), "content": "x"}}],
        gate=_confirm)

    assert report["steps"][0]["verdict"] == "would_need_approval"
    assert report["summary"]["would_need_approval"] == 1
    assert report["summary"]["would_be_refused"] == 0


def test_an_unobservable_tool_is_not_simulated_rather_than_guessed():
    """`open_url` has an effect outside this process. A shadow must say so."""
    report = ShadowMission().run(
        [{"step_id": "S1", "tool": "open_url",
          "params": {"url": "https://example.com"}}],
        gate=_allow)

    step = report["steps"][0]
    assert step["verdict"] == "not_simulated"
    assert "unknown" in step["detail"]
    assert report["summary"]["not_simulated"] == 1


def test_a_partly_simulated_plan_says_which_part_it_cannot_see():
    report = ShadowMission().run(
        [{"step_id": "S1", "tool": "run_allowlisted_command",
          "params": {"command": "echo hi"}},
         {"step_id": "S2", "tool": "open_application", "params": {"app": "finder"}}],
        gate=_allow)

    assert report["summary"]["not_simulated"] == 2
    assert "None of these steps can be simulated" in report["conclusion"]


def test_copying_a_missing_source_is_refused_not_previewed(workspace):
    report = ShadowMission().run(
        [{"step_id": "S1", "tool": "copy_file",
          "params": {"source": str(workspace / "nope.txt"),
                     "destination": str(workspace / "out.txt")}}],
        gate=_allow)

    assert report["steps"][0]["verdict"] == "would_be_refused"
    assert "nothing to copy" in report["steps"][0]["detail"]


def test_a_copy_previews_the_destinations_future_contents(workspace):
    source = workspace / "source.txt"
    source.write_text("payload", encoding="utf-8")

    report = ShadowMission().run(
        [{"step_id": "S1", "tool": "copy_file",
          "params": {"source": str(source),
                     "destination": str(workspace / "copy.txt")}}],
        gate=_allow)

    step = report["steps"][0]
    assert step["verdict"] == "would_run"
    assert step["after"] == "payload"
    assert not (workspace / "copy.txt").exists()


def test_deleting_previews_absence_without_deleting(workspace):
    target = workspace / "doomed.txt"
    target.write_text("bye", encoding="utf-8")

    report = ShadowMission().run(
        [{"step_id": "S1", "tool": "delete_file", "params": {"path": str(target)}}],
        gate=_allow)

    assert report["steps"][0]["after"] == ""
    assert target.exists(), "the shadow deleted the file it was only previewing"


def test_the_guards_refusal_is_honoured(workspace):
    """A path the file tools refuse must not preview as runnable."""

    def guard(path):
        return "this path is protected" if "protected" in path else None

    report = ShadowMission(guard=guard).run(
        [{"step_id": "S1", "tool": "write_file",
          "params": {"path": "/tmp/protected.txt", "content": "x"}}],
        gate=_allow)

    assert report["steps"][0]["verdict"] == "would_be_refused"
    assert "protected" in report["steps"][0]["detail"]


def test_a_read_of_an_earlier_write_is_marked_a_prediction(workspace):
    target = workspace / "note.txt"
    target.write_text("old", encoding="utf-8")

    report = ShadowMission().run(
        [{"step_id": "S1", "tool": "write_file",
          "params": {"path": str(target), "content": "new"}},
         {"step_id": "S2", "tool": "read_file", "params": {"path": str(target)}}],
        gate=_allow)

    assert report["steps"][1]["verdict"] == "predicted"
    assert report["steps"][1]["after"] == "new"
    assert "prediction" in report["steps"][1]["detail"]


def test_a_raising_gate_does_not_crash_the_shadow(workspace):
    def broken(tool, params, device, session_id):
        raise RuntimeError("engine exploded")

    report = ShadowMission().run(
        [{"step_id": "S1", "tool": "write_file",
          "params": {"path": str(workspace / "a.txt"), "content": "x"}}],
        gate=broken)

    assert report["steps"][0]["verdict"] == "would_be_refused"
    assert "could not be asked" in report["steps"][0]["detail"]


@pytest.fixture()
def workspace(tmp_path):
    return tmp_path
def test_a_gate_blocked_step_is_not_counted_as_previewed(workspace):
    """A step stopped at the gate was never previewed, so it is not claimed.

    The conclusion used to say "2 of 3 step(s) can be previewed" about steps
    that the permission engine had just refused to let run; nothing about their
    effects had been computed at all.
    """
    report = ShadowMission().run(
        [{"step_id": "S1", "tool": "write_file",
          "params": {"path": str(workspace / "a.txt"), "content": "x"}},
         {"step_id": "S2", "tool": "copy_file",
          "params": {"source": str(workspace / "a.txt"),
                     "destination": str(workspace / "b.txt")}},
         {"step_id": "S3", "tool": "open_url", "params": {"url": "https://x.test"}}],
        gate=_confirm)

    assert report["summary"]["would_run"] == 0
    assert "No step's effect could be previewed" in report["conclusion"]
    assert "can be previewed" not in report["conclusion"]


def test_one_previewable_step_among_unknowns_states_both(workspace):
    report = ShadowMission().run(
        [{"step_id": "S1", "tool": "write_file",
          "params": {"path": str(workspace / "a.txt"), "content": "x"}},
         {"step_id": "S2", "tool": "open_url", "params": {"url": "https://x.test"}}],
        gate=_allow)

    assert report["summary"]["would_run"] == 1
    assert "1 of 2 step(s) can be previewed" in report["conclusion"]
    assert "unknown until they run" in report["conclusion"]
