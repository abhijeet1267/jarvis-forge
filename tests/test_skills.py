"""Skills: manifests validate strictly, stores version honestly, reliability is measured.

The tests fall into two groups, matching the two things that can go wrong:

* **A manifest is untrusted input.** ``from_dict`` must reject a missing
  required field and an out-of-range risk loudly, because a permission-relevant
  typo that is silently ignored becomes a claim nobody checks.
* **Reliability must be measured, not implied.** A skill with no runs reports
  no success rate; a verification that did not positively confirm the outcome
  is counted as unknown, not as a success. "It ran once" is never rounded up.

Every test gets its own ``tmp_path`` file, so no test can read another's store.
"""

from __future__ import annotations

import json

import pytest

from jarvis.core.skills import (
    SkillManifest,
    SkillStore,
    validate_skill,
)


def _manifest(**overrides) -> SkillManifest:
    base = dict(
        skill_id="SKL-weekly-report",
        name="Weekly report",
        description="Collect the week's numbers and write a report file.",
        inputs={"week": "ISO week, e.g. 2026-W45"},
        outputs=["/tmp/reports/weekly.md"],
        steps=[{"tool": "write_file",
                "params": {"path": "/tmp/reports/weekly.md"},
                "satisfies": "C1"}],
        required_capabilities=["write_file"],
        required_scope=["/tmp/reports"],
        risk=1,
        preconditions=["the reports directory exists"],
        verification=[{"condition_id": "C1", "kind": "file_contains",
                       "path": "/tmp/reports/weekly.md", "needle": "Week"}],
        supported_platforms=["darwin"],
        owner="user",
    )
    base.update(overrides)
    return SkillManifest(**base)


@pytest.fixture()
def store(tmp_path):
    return SkillStore(tmp_path / "skills.json")


# -- save / get / versioning ------------------------------------------------

def test_save_then_get_returns_the_manifest(store):
    saved = store.save(_manifest())
    got = store.get("SKL-weekly-report")
    assert got is not None
    assert got.skill_id == saved.skill_id
    assert got.name == "Weekly report"
    assert got.steps[0]["tool"] == "write_file"
    assert got.verification[0]["kind"] == "file_contains"


def test_updating_a_skill_bumps_its_version_and_keeps_history(store):
    first = store.save(_manifest())
    assert first.version == 1

    updated = store.save(_manifest(description="Now with charts."))
    assert updated.version == 2
    # The old version is still retrievable -- history is not rewritten.
    assert store.get("SKL-weekly-report", 1).description.startswith("Collect")
    assert store.get("SKL-weekly-report", 2).description == "Now with charts."
    assert store.get("SKL-weekly-report").version == 2

    # A third save is version 3, not a renumbered 2.
    assert store.save(_manifest()).version == 3

    # list() shows one row per skill: the latest.
    listed = store.list()
    assert [(m.skill_id, m.version) for m in listed] == [("SKL-weekly-report", 3)]


def test_history_survives_a_second_store_instance(store, tmp_path):
    """The file is the state; an instance holds nothing that must survive a restart."""
    store.save(_manifest())
    store.save(_manifest(name="Weekly report v2"))
    reopened = SkillStore(tmp_path / "skills.json")
    assert reopened.get("SKL-weekly-report").version == 2
    assert reopened.get("SKL-weekly-report", 1).version == 1


def test_a_saved_manifest_cannot_alias_the_callers_dict(store):
    params = {"path": "/tmp/reports/weekly.md"}
    manifest = _manifest(steps=[{"tool": "write_file", "params": params,
                                 "satisfies": "C1"}])
    store.save(manifest)
    params["path"] = "/etc/passwd"
    assert store.get("SKL-weekly-report").steps[0]["params"]["path"] == \
        "/tmp/reports/weekly.md"


# -- validation -------------------------------------------------------------

def test_from_dict_rejects_a_manifest_missing_steps():
    raw = _manifest().to_dict()
    del raw["steps"]
    with pytest.raises(ValueError) as excinfo:
        SkillManifest.from_dict(raw)
    assert "missing required field" in str(excinfo.value)
    assert "steps" in str(excinfo.value)


def test_from_dict_rejects_a_risk_of_nine():
    raw = _manifest(risk=1).to_dict()
    raw["risk"] = 9
    with pytest.raises(ValueError) as excinfo:
        SkillManifest.from_dict(raw)
    assert "risk" in str(excinfo.value)
    assert "0..3" in str(excinfo.value)


def test_from_dict_rejects_an_unknown_field():
    """A field no code reads is a claim no code checks; refuse it instead."""
    raw = _manifest().to_dict()
    raw["auto_approve"] = True
    with pytest.raises(ValueError) as excinfo:
        SkillManifest.from_dict(raw)
    assert "unknown field" in str(excinfo.value)
    assert "auto_approve" in str(excinfo.value)


def test_from_dict_rejects_a_non_positive_version():
    raw = _manifest().to_dict()
    raw["version"] = 0
    with pytest.raises(ValueError) as excinfo:
        SkillManifest.from_dict(raw)
    assert "positive integer" in str(excinfo.value)


def test_save_refuses_an_invalid_manifest_rather_than_writing_it(store):
    """The store is a boundary too: a hand-built dataclass is still validated."""
    with pytest.raises(ValueError):
        store.save(_manifest(risk=7))
    assert store.list() == []


def test_a_corrupt_file_raises_instead_of_reading_as_empty(store, tmp_path):
    """Degrading to empty would make the next save overwrite every saved skill."""
    path = tmp_path / "skills.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError) as excinfo:
        store.list()
    assert "not valid JSON" in str(excinfo.value)


# -- enable / disable -------------------------------------------------------

def test_a_disabled_skill_is_not_runnable_but_still_inspectable(store):
    store.save(_manifest())
    assert store.is_enabled("SKL-weekly-report") is True
    assert store.get_runnable("SKL-weekly-report") is not None

    assert store.disable("SKL-weekly-report") is True
    assert store.is_enabled("SKL-weekly-report") is False
    # Not runnable...
    assert store.get_runnable("SKL-weekly-report") is None
    # ...but still visible, so a person can find it and turn it back on.
    assert store.get("SKL-weekly-report") is not None
    assert store.list()[0].skill_id == "SKL-weekly-report"

    assert store.enable("SKL-weekly-report") is True
    assert store.get_runnable("SKL-weekly-report") is not None


def test_disable_and_enable_report_unknown_skills(store):
    assert store.disable("SKL-nope") is False
    assert store.enable("SKL-nope") is False
    assert store.get_runnable("SKL-nope") is None


def test_delete_removes_the_skill_and_its_record(store):
    store.save(_manifest())
    store.record_run("SKL-weekly-report", ok=True, verdict="verified",
                     duration_s=1.5)
    assert store.delete("SKL-weekly-report") is True
    assert store.get("SKL-weekly-report") is None
    assert store.delete("SKL-weekly-report") is False
    with pytest.raises(ValueError):
        store.reliability("SKL-weekly-report")


# -- reliability ------------------------------------------------------------

def test_reliability_with_no_runs_reports_no_success_rate(store):
    """No runs means unknown, never a default 0% or 100%."""
    store.save(_manifest())
    report = store.reliability("SKL-weekly-report")
    assert report["runs"] == 0
    assert report["success_rate"] is None
    assert report["verified_successes"] == 0
    assert report["failures"] == 0
    assert report["unknowns"] == 0
    assert report["last_duration_s"] is None
    assert "unverified" in report["note"]


def test_record_run_counts_verified_failed_and_unknown_across_three_calls(store):
    store.save(_manifest())
    store.record_run("SKL-weekly-report", ok=True, verdict="verified",
                     duration_s=1.0, note="the file contained the week")
    store.record_run("SKL-weekly-report", ok=False, verdict="failed",
                     duration_s=0.5, note="write refused")
    # Executed, but the verifier could not observe the outcome: that is unknown,
    # not a success. This is the exact case that must not round up.
    store.record_run("SKL-weekly-report", ok=True, verdict="unknown",
                     duration_s=2.0, note="no verifier could read the file")

    report = store.reliability("SKL-weekly-report")
    assert report["runs"] == 3
    assert report["verified_successes"] == 1
    assert report["failures"] == 1
    assert report["unknowns"] == 1
    assert report["success_rate"] == pytest.approx(1 / 3)
    assert report["last_duration_s"] == 2.0
    assert report["last_verdict"] == "unknown"


def test_a_run_marked_ok_with_a_failed_verdict_counts_as_a_failure(store):
    """`ok` says it executed; the verdict says whether it worked. Both must agree."""
    store.save(_manifest())
    store.record_run("SKL-weekly-report", ok=True, verdict="failed",
                     duration_s=1.0)
    report = store.reliability("SKL-weekly-report")
    assert report["verified_successes"] == 0
    assert report["failures"] == 1
    assert report["success_rate"] == 0.0


def test_record_run_for_an_unknown_skill_raises(store):
    with pytest.raises(ValueError) as excinfo:
        store.record_run("SKL-nope", ok=True, verdict="verified", duration_s=1.0)
    assert "no skill" in str(excinfo.value)


def test_disabling_does_not_erase_reliability(store):
    store.save(_manifest())
    store.record_run("SKL-weekly-report", ok=True, verdict="verified",
                     duration_s=1.0)
    store.disable("SKL-weekly-report")
    report = store.reliability("SKL-weekly-report")
    assert report["verified_successes"] == 1
    assert report["enabled"] is False


# -- validate_skill ---------------------------------------------------------

def test_validate_skill_passes_for_a_covered_manifest():
    problems = validate_skill(
        _manifest(),
        available_capabilities={"write_file", "read_file"},
        allowed_scope=["/tmp/reports", "/tmp/other"],
        max_risk=2)
    assert problems == []


def test_validate_skill_flags_a_capability_that_is_not_available():
    problems = validate_skill(
        _manifest(required_capabilities=["write_file", "send_email"]),
        available_capabilities={"write_file"},
        allowed_scope=["/tmp/reports"])
    assert len(problems) == 1
    assert "send_email" in problems[0]
    assert "not available" in problems[0]


def test_validate_skill_flags_a_scope_prefix_that_is_not_covered():
    problems = validate_skill(
        _manifest(required_scope=["/etc"]),
        available_capabilities={"write_file"},
        allowed_scope=["/tmp/reports"])
    assert len(problems) == 1
    assert "/etc" in problems[0]
    assert "not covered" in problems[0]


def test_validate_skill_flags_the_sibling_prefix_trap():
    """`/a/bc` must NOT be covered by an allowed scope of `/a/b`.

    String-prefix matching without a path boundary would accept it, which is how
    a skill scoped to one project directory reaches a neighbour.
    """
    problems = validate_skill(
        _manifest(required_scope=["/a/bc"]),
        available_capabilities={"write_file"},
        allowed_scope=["/a/b"])
    assert len(problems) == 1
    assert "/a/bc" in problems[0]
    assert "not covered" in problems[0]


def test_validate_skill_accepts_a_prefix_beneath_an_allowed_one():
    problems = validate_skill(
        _manifest(required_scope=["/a/b/c"]),
        available_capabilities={"write_file"},
        allowed_scope=["/a/b"])
    assert problems == []


def test_validate_skill_flags_a_risk_above_the_ceiling():
    problems = validate_skill(
        _manifest(risk=3),
        available_capabilities={"write_file"},
        allowed_scope=["/tmp/reports"],
        max_risk=1)
    assert any("risk 3" in problem and "ceiling of 1" in problem
               for problem in problems)


def test_validate_skill_flags_an_empty_skill():
    problems = validate_skill(
        _manifest(steps=[]),
        available_capabilities={"write_file"},
        allowed_scope=["/tmp/reports"])
    assert any("no steps" in problem for problem in problems)


def test_validate_skill_refuses_unvalidated_input():
    with pytest.raises(TypeError):
        validate_skill({"skill_id": "x"}, available_capabilities=set(),
                       allowed_scope=[])


# -- the store is metadata only ---------------------------------------------

def test_the_store_exposes_no_way_to_execute_a_skill(store):
    """Security invariant: a skill never grants itself permission.

    Execution goes through the mission runner (permission engine + leases).
    If a `run`/`execute` method ever appears here, it is a bypass.
    """
    for name in ("run", "execute", "run_skill", "execute_skill"):
        assert not hasattr(store, name), f"SkillStore.{name} must not exist"


def test_the_saved_file_is_owner_only(store, tmp_path):
    store.save(_manifest())
    mode = (tmp_path / "skills.json").stat().st_mode & 0o777
    assert mode == 0o600


def test_the_file_is_a_readable_document(store, tmp_path):
    """A skill is meant to be inspected and shared -- the file must stay legible."""
    store.save(_manifest())
    data = json.loads((tmp_path / "skills.json").read_text(encoding="utf-8"))
    assert data["schema"] == 1
    entry = data["skills"]["SKL-weekly-report"]
    assert entry["disabled"] is False
    assert entry["versions"][0]["steps"][0]["tool"] == "write_file"
    assert entry["runs"]["verified_successes"] == 0