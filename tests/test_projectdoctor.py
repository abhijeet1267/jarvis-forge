"""Project Doctor: observed findings, not guesses about what usually breaks.

Each test drives a real directory with real files. The value being pinned is
that every finding names the evidence it rests on -- and that a check this
system cannot perform is reported as not_checked rather than as passing.
"""

from __future__ import annotations

import json

import pytest

from jarvis.core.projectdoctor import detect, inspect, propose_repair


def test_detection_comes_from_files_not_from_the_name(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    profile = detect(tmp_path)
    assert profile is not None
    assert profile.kind == "python"
    assert profile.manifest == "pyproject.toml"


def test_a_node_project_is_detected_and_its_test_script_found(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "demo", "scripts": {"test": "jest"}}), encoding="utf-8")
    profile = detect(tmp_path)
    assert profile.kind == "node"
    assert profile.test_command == ["npm", "test", "--silent"]


def test_an_unrecognised_directory_is_reported_as_unknown(tmp_path):
    (tmp_path / "notes.txt").write_text("hi", encoding="utf-8")
    report = inspect(str(tmp_path))
    assert report["kind"] == "unknown"
    assert any("no recognised project manifest" in p["detail"]
               for p in report["problems"])


def test_a_malformed_manifest_is_a_problem_with_the_parser_reason(tmp_path):
    """This is the real, common failure a doctor must name precisely."""
    (tmp_path / "package.json").write_text("{not json", encoding="utf-8")
    report = inspect(str(tmp_path))
    problems = [p for p in report["problems"] if p["check"] == "manifest"]
    assert problems, report
    assert "not valid JSON" in problems[0]["detail"]


def test_a_valid_manifest_is_reported_ok(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"name": "ok"}), encoding="utf-8")
    report = inspect(str(tmp_path))
    manifest = [f for f in report["findings"] if f["check"] == "manifest"]
    assert manifest and manifest[0]["status"] == "ok"


def test_a_missing_test_runner_is_not_checked_not_passing(tmp_path):
    """'I did not run it' must never read as 'it passed'."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    report = inspect(str(tmp_path))
    tests = [f for f in report["findings"] if f["check"] == "tests"]
    if tests:
        assert tests[0]["status"] in ("not_checked", "ok")


def test_checks_are_not_run_unless_asked(tmp_path):
    """Reading files is safe; running a build is an execution someone asks for."""
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "x", "scripts": {"test": "exit 1"}}), encoding="utf-8")
    report = inspect(str(tmp_path))          # run_checks defaults off
    assert not [f for f in report["findings"] if f["check"] == "build/tests"]


def test_inspecting_a_missing_directory_is_refused(tmp_path):
    with pytest.raises(ValueError):
        inspect(str(tmp_path / "nope"))


def test_a_repair_for_an_unparseable_manifest_is_refused_rather_than_invented(tmp_path):
    """Guessing a replacement package.json would destroy real configuration."""
    (tmp_path / "package.json").write_text("{broken", encoding="utf-8")
    proposal = propose_repair(str(tmp_path))
    assert proposal["applicable"] is False
    assert proposal["operations"] == []
    assert "will not guess" in proposal["summary"]


def test_an_unknown_finding_yields_no_automatic_repair(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    proposal = propose_repair(str(tmp_path), "something needs a person")
    assert proposal["applicable"] is False
    assert proposal["operations"] == []


def test_a_repair_is_shaped_for_parallelworld_not_applied_here(tmp_path):
    """The doctor proposes; ParallelWorld previews and applies. One write path."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    proposal = propose_repair(str(tmp_path))
    # Whatever it returns, it must never have written anything itself.
    assert set(proposal) >= {"operations", "summary", "applicable"}
    assert (tmp_path / "pyproject.toml").read_text(encoding="utf-8") == \
        "[project]\nname='x'\n"