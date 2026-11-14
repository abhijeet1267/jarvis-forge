"""ParallelWorld: a preview that never writes, and an apply that refuses stale work.

The two properties worth testing are the ones a user would be harmed by losing:
a preview that mutates the target is not a preview, and an apply that overwrites
a file changed since the preview destroys work nobody saw.
"""

from __future__ import annotations

import os
import time

import pytest

from jarvis.core.parallelworld import ParallelWorld


def _guard_refusing(*blocked):
    def guard(path: str):
        for prefix in blocked:
            if str(path).startswith(prefix):
                return f"{path} is a protected path"
        return None
    return guard


# -- preview ---------------------------------------------------------------

def test_a_preview_reports_the_change_without_touching_the_file(tmp_path):
    target = tmp_path / "README.md"
    target.write_text("# Old\n", encoding="utf-8")
    before = target.read_bytes()

    world = ParallelWorld()
    plan = world.plan([{"op": "replace", "path": str(target),
                        "content": "# New\n\nwith a flag\n"}])
    preview = world.preview(plan)

    assert target.read_bytes() == before, "preview must not write to the target"
    assert preview["applicable"] is True
    change = preview["changes"][0]
    assert change["before"] == "# Old\n"
    assert change["after"].startswith("# New")
    assert preview["summary"]["modified"] == 1


def test_creating_over_an_existing_file_is_a_conflict(tmp_path):
    target = tmp_path / "exists.txt"
    target.write_text("already here", encoding="utf-8")

    world = ParallelWorld()
    plan = world.plan([{"op": "create", "path": str(target), "content": "new"}])
    preview = world.preview(plan)

    assert preview["applicable"] is False
    assert any("already exists" in c["reason"] for c in preview["conflicts"])


def test_replacing_a_missing_file_is_a_conflict(tmp_path):
    world = ParallelWorld()
    plan = world.plan([{"op": "replace", "path": str(tmp_path / "nope.txt"),
                        "content": "x"}])
    preview = world.preview(plan)

    assert preview["applicable"] is False
    assert any("does not exist" in c["reason"] for c in preview["conflicts"])


def test_a_protected_path_is_refused_at_preview_time(tmp_path):
    target = tmp_path / "secret" / "key.txt"
    target.parent.mkdir()
    target.write_text("s", encoding="utf-8")

    world = ParallelWorld(guard=_guard_refusing(str(tmp_path / "secret")))
    plan = world.plan([{"op": "delete", "path": str(target)}])
    preview = world.preview(plan)

    assert preview["applicable"] is False
    assert any("protected" in c["reason"] for c in preview["conflicts"])


def test_an_unsupported_operation_is_refused_not_guessed(tmp_path):
    world = ParallelWorld()
    with pytest.raises(ValueError) as excinfo:
        world.plan([{"op": "chmod", "path": str(tmp_path / "f"), "content": ""}])
    assert "not a supported file operation" in str(excinfo.value)


def test_the_preview_states_what_it_cannot_do(tmp_path):
    world = ParallelWorld()
    preview = world.preview(world.plan([]))
    assert "not" in preview["note"] and "atomic" in preview["note"]


# -- apply -----------------------------------------------------------------

def test_apply_without_approval_changes_nothing(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("original", encoding="utf-8")

    world = ParallelWorld()
    plan = world.plan([{"op": "replace", "path": str(target), "content": "changed"}])
    result = world.apply(plan, approved=False)

    assert result["applied"] is False
    assert "approval" in result["refused"]
    assert target.read_text(encoding="utf-8") == "original"


def test_an_approved_apply_writes_and_verifies(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("original", encoding="utf-8")

    world = ParallelWorld()
    plan = world.plan([{"op": "replace", "path": str(target), "content": "approved"}])
    result = world.apply(plan, approved=True)

    assert result["applied"] is True
    assert target.read_text(encoding="utf-8") == "approved"
    assert result["results"][0]["ok"] is True


def test_a_file_changed_since_the_preview_is_refused_and_left_alone(tmp_path):
    """The work nobody saw must survive."""
    target = tmp_path / "f.txt"
    target.write_text("as previewed", encoding="utf-8")

    world = ParallelWorld()
    plan = world.plan([{"op": "replace", "path": str(target), "content": "new"}])

    # Someone edits the file between preview and apply.
    time.sleep(0.01)
    target.write_text("edited by someone else", encoding="utf-8")

    result = world.apply(plan, approved=True)

    assert result["applied"] is False
    assert target.read_text(encoding="utf-8") == "edited by someone else"
    assert "changed after the preview" in result["results"][0]["detail"]


def test_a_delete_is_applied_only_with_approval(tmp_path):
    target = tmp_path / "gone.txt"
    target.write_text("bye", encoding="utf-8")
    world = ParallelWorld()
    plan = world.plan([{"op": "delete", "path": str(target)}])

    assert world.apply(plan, approved=False)["applied"] is False
    assert target.exists()

    assert world.apply(plan, approved=True)["applied"] is True
    assert not target.exists()


def test_an_append_verifies_the_resulting_content(tmp_path):
    target = tmp_path / "log.txt"
    target.write_text("line one\n", encoding="utf-8")
    world = ParallelWorld()
    plan = world.plan([{"op": "append", "path": str(target), "content": "line two\n"}])
    result = world.apply(plan, approved=True)

    assert result["applied"] is True
    assert target.read_text(encoding="utf-8") == "line one\nline two\n"


def test_a_protected_path_is_refused_at_apply_time_too(tmp_path):
    """Enforcing only at preview would let a plan be replayed around the guard."""
    target = tmp_path / "protected" / "f.txt"
    target.parent.mkdir()
    target.write_text("keep", encoding="utf-8")

    world = ParallelWorld(guard=_guard_refusing(str(tmp_path / "protected")))
    plan = world.plan([{"op": "delete", "path": str(target)}])
    result = world.apply(plan, approved=True)

    assert result["applied"] is False
    assert target.exists()


def test_creating_a_file_in_a_missing_directory_succeeds(tmp_path):
    target = tmp_path / "nested" / "dir" / "new.txt"
    world = ParallelWorld()
    plan = world.plan([{"op": "create", "path": str(target), "content": "hi"}])
    result = world.apply(plan, approved=True)

    assert result["applied"] is True
    assert target.read_text(encoding="utf-8") == "hi"

# -- Demo B/E shapes, through the same class the API uses ----------------

def test_a_multi_file_plan_applies_every_operation_or_reports_which_did(tmp_path):
    """Partial failure must be identifiable, never reported as whole success."""
    good = tmp_path / "good.txt"
    good.write_text("first\n", encoding="utf-8")
    blocked = tmp_path / "blocked.txt"
    blocked.write_text("keep\n", encoding="utf-8")

    world = ParallelWorld(guard=lambda p: ("refused" if p.endswith("blocked.txt")
                                           else None))
    plan = world.plan([
        {"op": "replace", "path": str(good), "content": "changed\n"},
        {"op": "replace", "path": str(blocked), "content": "should not apply"},
    ])
    result = world.apply(plan, approved=True)

    assert result["applied"] is False, "one refused operation means not applied"
    outcomes = {r["path"]: r for r in result["results"]}
    assert outcomes[str(good.resolve())]["ok"] is True
    assert outcomes[str(blocked.resolve())]["ok"] is False
    # The refused file kept its content.
    assert blocked.read_text(encoding="utf-8") == "keep\n"
    assert str(blocked.resolve()) in result["rolled_back"]


def test_a_preview_taken_before_an_external_edit_is_then_refused(tmp_path):
    """Demo B: the newer content wins, and the refusal explains itself."""
    target = tmp_path / "doc.txt"
    target.write_text("v1\n", encoding="utf-8")

    world = ParallelWorld()
    plan = world.plan([{"op": "replace", "path": str(target), "content": "v2\n"}])
    preview = world.preview(plan)
    assert preview["applicable"] is True

    target.write_text("edited by the person\n", encoding="utf-8")
    result = world.apply(plan, approved=True)

    assert result["applied"] is False
    assert target.read_text(encoding="utf-8") == "edited by the person\n"
    assert "changed after the preview" in result["results"][0]["detail"]


def test_a_directory_is_a_conflict_not_a_crash(tmp_path):
    """Previewing a directory used to raise IsADirectoryError out of the route."""
    directory = tmp_path / "a-folder"
    directory.mkdir()

    world = ParallelWorld()
    plan = world.plan([{"op": "create", "path": str(directory), "content": "x"}])
    preview = world.preview(plan)

    assert preview["applicable"] is False
    assert any("directory" in c["reason"] for c in preview["conflicts"])


def test_applying_to_a_directory_is_refused_without_raising(tmp_path):
    directory = tmp_path / "a-folder"
    directory.mkdir()
    world = ParallelWorld()
    plan = world.plan([{"op": "delete", "path": str(directory)}])
    result = world.apply(plan, approved=True)

    assert result["applied"] is False
    assert any("directory" in r["detail"] for r in result["results"])
    assert directory.is_dir()
