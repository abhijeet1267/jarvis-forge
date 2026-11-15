"""Smart Files: report what is there, and never write.

The scan is the safety-critical part: it must not follow symlinked directories
(a link is not a subtree the person selected), must bound itself, and must not
report a folder as clean when part of it was unreadable.
"""

from __future__ import annotations

import os

import pytest

from jarvis.core.smartfiles import (
    MAX_DEPTH, category_for, organize_plan, rename_plan, scan,
)


def _write(path, content="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_byte_identical_files_are_reported_as_one_group(tmp_path):
    a = _write(tmp_path / "a.txt", "same content " * 200)
    b = _write(tmp_path / "b.txt", "same content " * 200)
    _write(tmp_path / "c.txt", "different " * 200)

    report = scan(str(tmp_path))

    assert len(report["duplicate_groups"]) == 1
    group = report["duplicate_groups"][0]
    assert group["keep"] in (str(a), str(b))
    assert len(group["copies"]) == 1
    assert report["reclaimable_bytes"] > 0


def test_same_size_different_content_is_not_a_duplicate(tmp_path):
    _write(tmp_path / "a.txt", "AAAAAAAAAA")
    _write(tmp_path / "b.txt", "BBBBBBBBBB")
    report = scan(str(tmp_path))
    assert report["duplicate_groups"] == []


def test_tiny_files_are_not_reported_as_duplicates(tmp_path):
    _write(tmp_path / "a.txt", "x")
    _write(tmp_path / "b.txt", "x")
    report = scan(str(tmp_path))
    assert report["duplicate_groups"] == []


def test_a_symlinked_directory_is_not_followed(tmp_path):
    """A link is not a subtree the person selected."""
    outside = tmp_path / "outside"
    _write(outside / "secret.txt", "outside the scope " * 100)
    scanroot = tmp_path / "scan"
    _write(scanroot / "inside.txt", "inside " * 100)
    os.symlink(outside, scanroot / "link")

    report = scan(str(scanroot))

    names = {entry["name"] for entry in report["largest"]}
    assert "secret.txt" not in names, "the scan followed a symlink out of scope"
    assert report["file_count"] == 1


def test_the_scan_does_not_write_anything(tmp_path):
    _write(tmp_path / "a.txt", "same " * 400)
    _write(tmp_path / "b.txt", "same " * 400)
    before = sorted((p.name, p.stat().st_size) for p in tmp_path.iterdir())
    scan(str(tmp_path))
    after = sorted((p.name, p.stat().st_size) for p in tmp_path.iterdir())
    assert before == after


def test_an_unreadable_file_is_listed_not_silently_dropped(tmp_path, monkeypatch):
    """Telling someone their folder is clean when part of it was not read is lie."""
    a = _write(tmp_path / "a.txt", "same " * 400)
    b = _write(tmp_path / "b.txt", "same " * 400)

    import jarvis.core.smartfiles as sf
    real = sf._digest
    monkeypatch.setattr(sf, "_digest",
                        lambda p: None if p == str(a) else real(p))

    report = scan(str(tmp_path))
    assert str(a) in report["unreadable"]


def test_scanning_a_missing_directory_is_refused(tmp_path):
    with pytest.raises(ValueError):
        scan(str(tmp_path / "nope"))


def test_categories_are_predictable():
    assert category_for("photo.JPG") == "images"
    assert category_for("notes.md") == "documents"
    assert category_for("data.csv") == "spreadsheets"
    assert category_for("script.py") == "code"
    assert category_for("mystery.xyz") == "other"


def test_an_organize_plan_moves_only_mapped_top_level_files(tmp_path):
    _write(tmp_path / "photo.png", "a")
    _write(tmp_path / "notes.md", "b")
    _write(tmp_path / "mystery.xyz", "c")
    nested = tmp_path / "project"
    _write(nested / "inner.py", "d")

    plan = organize_plan(str(tmp_path))

    paths = [op["path"] for op in plan["operations"]]
    assert any(p.endswith("photo.png") for p in paths)
    assert any(p.endswith("notes.md") for p in paths)
    assert not any(p.endswith("mystery.xyz") for p in paths), "unmapped left alone"
    assert not any("project/" in p for p in paths), "nested files untouched"
    assert plan["applicable"] is True
    # The operation name must be one ParallelWorld accepts, or the plan could
    # never be applied: the two modules once disagreed on `move` vs `move_file`.
    assert {op["op"] for op in plan["operations"]} == {"move"}


def test_an_organize_plan_never_writes(tmp_path):
    _write(tmp_path / "photo.png", "a")
    organize_plan(str(tmp_path))
    assert (tmp_path / "photo.png").exists()
    assert not (tmp_path / "images").exists()


def test_an_unknown_organize_mode_is_refused(tmp_path):
    with pytest.raises(ValueError):
        organize_plan(str(tmp_path), by="date")


# -- bulk rename -----------------------------------------------------------

def test_a_prefix_is_added_and_the_extension_is_kept(tmp_path):
    (tmp_path / "report.txt").write_text("x", encoding="utf-8")
    plan = rename_plan(str(tmp_path), prefix="2026_")

    assert plan["applicable"] is True
    preview = plan["preview"][0]
    assert preview["from"] == "report.txt"
    assert preview["to"] == "2026_report.txt", "the extension must survive a rename"


def test_find_and_replace_changes_only_the_name(tmp_path):
    (tmp_path / "draft_v1.md").write_text("x", encoding="utf-8")
    plan = rename_plan(str(tmp_path), find="draft", replace="final")
    assert plan["preview"][0]["to"] == "final_v1.md"


def test_a_sequence_numbers_each_file(tmp_path):
    for name in ("a.txt", "b.txt"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    plan = rename_plan(str(tmp_path), sequence=True, start=1)
    assert [p["to"] for p in plan["preview"]] == ["a_001.txt", "b_002.txt"]


def test_two_files_landing_on_one_name_is_a_conflict(tmp_path):
    """A person decides which name wins; the plan refuses to guess."""
    (tmp_path / "draft_a.txt").write_text("x", encoding="utf-8")
    (tmp_path / "final_a.txt").write_text("y", encoding="utf-8")
    plan = rename_plan(str(tmp_path), find="draft", replace="final")

    assert plan["applicable"] is False
    assert plan["conflicts"], plan
    # It lands on an existing file rather than two renames colliding, and the
    # plan says so precisely -- either wording is a conflict, and the exact
    # reason matters for the person deciding what to do.
    assert any("already exists" in c["reason"] or "same name" in c["reason"]
               for c in plan["conflicts"]), plan["conflicts"]


def test_renaming_onto_an_existing_file_is_a_conflict(tmp_path):
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    (tmp_path / "b_a.txt").write_text("y", encoding="utf-8")
    plan = rename_plan(str(tmp_path), prefix="b_")
    assert plan["applicable"] is False
    assert any("already exists" in c["reason"] for c in plan["conflicts"])


def test_a_rename_needs_at_least_one_rule(tmp_path):
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError):
        rename_plan(str(tmp_path))


def test_a_rename_plan_writes_nothing(tmp_path):
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    rename_plan(str(tmp_path), prefix="new_")
    assert (tmp_path / "a.txt").exists()
    assert not (tmp_path / "new_a.txt").exists()
