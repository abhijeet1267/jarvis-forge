"""Skill Evolution Lab: a proposal derived from runs, that can only narrow.

The properties that matter are negative ones, because the failure mode of a
"learn from history" feature is a learned permission. So most of these tests
assert what the lab *cannot* produce: no added step, no added capability, no
added scope prefix, no lowered risk, no write. A proposal that survived one of
those would be an autonomy feature that widened the surface it was meant to
understand.
"""

from __future__ import annotations

import json

import pytest

from jarvis.core.skill_evolution import (
    FAILURE_WEIGHT_FOR_REMOVAL,
    MIN_RUNS_FOR_EVOLUTION,
    NOT_ENOUGH_RUNS,
    OUT_OF_SCOPE,
    UNDERSTATED_RISK,
    UNSATISFIED_STEP,
    UNCHECKABLE_CONDITION,
    UNKNOWN_RUNS,
    EvolutionError,
    interpret_run,
    propose_evolution,
)
from jarvis.core.skills import SkillManifest, SkillStore


@pytest.fixture()
def store(tmp_path):
    return SkillStore(path=tmp_path / "skills.json")


def _base(**overrides):
    body = {
        "skill_id": "weekly-report",
        "name": "Weekly report",
        "steps": [{"tool": "write_file",
                   "params": {"path": "/tmp/rep.txt", "content": "x"},
                   "satisfies": "C1"}],
        "required_scope": ["/tmp"],
        "verification": [{"condition_id": "C1", "kind": "file_contains",
                          "path": "/tmp/rep.txt", "needle": "x"}],
    }
    body.update(overrides)
    return SkillManifest.from_dict(body)


def _run(store, skill_id, *, ok, verdict, times=1):
    for _ in range(times):
        store.record_run(skill_id, ok=ok, verdict=verdict, duration_s=0.1)


# -- the run floor ----------------------------------------------------------

def test_no_proposal_below_the_run_floor(store):
    store.save(_base())
    _run(store, "weekly-report", ok=True, verdict="verified", times=2)

    proposal = propose_evolution("weekly-report", store=store)
    assert proposal.changes_anything is False
    assert NOT_ENOUGH_RUNS in proposal.reasons
    assert str(MIN_RUNS_FOR_EVOLUTION) in proposal.notes[0]


def test_an_unknown_skill_is_an_error_not_an_empty_proposal(store):
    with pytest.raises(EvolutionError):
        propose_evolution("nope", store=store)


def test_a_clean_skill_is_left_alone(store):
    store.save(_base())
    _run(store, "weekly-report", ok=True, verdict="verified", times=4)

    proposal = propose_evolution("weekly-report", store=store)
    assert proposal.changes_anything is False
    assert "no change is proposed" in proposal.notes[0]
    assert proposal.evidence["runs"] == 4
    assert proposal.evidence["verified_successes"] == 4


def test_no_runs_means_no_confidence_either_way(store):
    store.save(_base())
    proposal = propose_evolution("weekly-report", store=store)
    assert proposal.evidence["success_rate"] is None
    assert proposal.changes_anything is False


# -- the one structural repair ---------------------------------------------

def test_a_condition_nobody_can_check_is_proposed_for_removal(store):
    store.save(_base(verification=[
        {"condition_id": "C1", "kind": "file_contains",
         "path": "/tmp/rep.txt", "needle": "x"},
        {"condition_id": "C2", "kind": "vibes_are_good"},
    ]))
    _run(store, "weekly-report", ok=True, verdict="unknown", times=3)

    proposal = propose_evolution("weekly-report", store=store)
    assert proposal.changes_anything is True
    assert UNCHECKABLE_CONDITION in proposal.reasons
    kinds = [v["kind"] for v in proposal.manifest["verification"]]
    assert kinds == ["file_contains"]
    assert any("vibes_are_good" in change for change in proposal.changes)


def test_a_step_that_only_served_an_uncheckable_condition_is_removed(store):
    store.save(_base(
        steps=[{"tool": "write_file",
                "params": {"path": "/tmp/rep.txt", "content": "x"},
                "satisfies": "C1"},
               {"tool": "copy_file",
                "params": {"source": "/tmp/a", "destination": "/tmp/b"},
                "satisfies": "C2"}],
        verification=[{"condition_id": "C1", "kind": "file_contains",
                       "path": "/tmp/rep.txt", "needle": "x"},
                      {"condition_id": "C2", "kind": "not_a_real_kind"}]))
    _run(store, "weekly-report", ok=True, verdict="unknown", times=3)

    proposal = propose_evolution("weekly-report", store=store)
    tools = [s["tool"] for s in proposal.manifest["steps"]]
    assert tools == ["write_file"]
    assert UNSATISFIED_STEP in proposal.reasons


def test_a_step_that_serves_a_surviving_condition_is_kept(store):
    """Removing a check must not take a step that still satisfies another."""
    store.save(_base(
        steps=[{"tool": "write_file",
                "params": {"path": "/tmp/rep.txt", "content": "x"},
                "satisfies": "C1"}],
        verification=[{"condition_id": "C1", "kind": "file_contains",
                       "path": "/tmp/rep.txt", "needle": "x"},
                      {"condition_id": "C2", "kind": "not_a_real_kind"}]))
    _run(store, "weekly-report", ok=True, verdict="unknown", times=3)

    proposal = propose_evolution("weekly-report", store=store)
    assert [s["tool"] for s in proposal.manifest["steps"]] == ["write_file"]
    assert UNSATISFIED_STEP not in proposal.reasons


# -- it cannot widen --------------------------------------------------------

def test_a_proposal_never_adds_a_step_capability_or_scope(store):
    store.save(_base(required_capabilities=["files.write"],
                     required_scope=["/tmp"]))
    _run(store, "weekly-report", ok=True, verdict="unknown", times=3)

    proposal = propose_evolution("weekly-report", store=store)
    proposed = proposal.manifest or _base().to_dict()
    assert len(proposed["steps"]) <= len(_base().to_dict()["steps"])
    assert set(proposed["required_capabilities"]) <= {"files.write"}
    assert set(proposed["required_scope"]) <= {"/tmp"}


def test_a_scope_gap_is_reported_never_filled(store):
    store.save(_base(steps=[{"tool": "write_file",
                             "params": {"path": "/etc/elsewhere.txt",
                                        "content": "x"},
                             "satisfies": "C1"}]))
    _run(store, "weekly-report", ok=True, verdict="verified", times=3)

    proposal = propose_evolution("weekly-report", store=store)
    assert OUT_OF_SCOPE in proposal.reasons
    assert any("/etc/elsewhere.txt" in note for note in proposal.notes)
    # The manifest was not touched to accommodate it.
    if proposal.manifest:
        assert "/etc/elsewhere.txt" not in " ".join(
            proposal.manifest.get("required_scope") or [])


def test_the_risk_tier_is_only_ever_raised(store):
    class _Spec:
        def __init__(self, risk):
            self.risk = risk

    class _Registry:
        def get(self, name):
            return _Spec(2) if name == "write_file" else None

    store.save(_base(risk=0))
    _run(store, "weekly-report", ok=True, verdict="verified", times=3)

    proposal = propose_evolution("weekly-report", store=store,
                                 registry=_Registry())
    assert UNDERSTATED_RISK in proposal.reasons
    assert proposal.manifest["risk"] == 2


def test_a_risk_above_the_manifest_cap_is_capped_and_called_out(store):
    """A PHYSICAL tool (4) cannot be written into a skill manifest (cap 3)."""
    class _Spec:
        risk = 4

    class _Registry:
        def get(self, name):
            return _Spec()

    store.save(_base(risk=0))
    _run(store, "weekly-report", ok=True, verdict="verified", times=3)

    proposal = propose_evolution("weekly-report", store=store,
                                 registry=_Registry())
    assert proposal.manifest["risk"] == 3
    # And the stored form is still a manifest the store will accept.
    SkillManifest.from_dict(proposal.manifest)
    assert any("PHYSICAL" in note for note in proposal.notes)


def test_an_understated_risk_is_never_lowered(store):
    class _Spec:
        risk = 0

    class _Registry:
        def get(self, name):
            return _Spec()

    store.save(_base(risk=3))
    _run(store, "weekly-report", ok=True, verdict="verified", times=3)

    proposal = propose_evolution("weekly-report", store=store,
                                 registry=_Registry())
    assert UNDERSTATED_RISK not in proposal.reasons
    assert proposal.changes_anything is False


def test_a_registry_that_cannot_be_read_leaves_the_claim_alone(store):
    store.save(_base(risk=0))
    _run(store, "weekly-report", ok=True, verdict="verified", times=3)

    proposal = propose_evolution("weekly-report", store=store, registry=None)
    assert UNDERSTATED_RISK not in proposal.reasons


# -- honest notes -----------------------------------------------------------

def test_unknown_runs_are_reported_without_a_fake_fix(store):
    store.save(_base())
    _run(store, "weekly-report", ok=True, verdict="partial", times=3)

    proposal = propose_evolution("weekly-report", store=store)
    assert UNKNOWN_RUNS in proposal.reasons
    assert any("not positively verified" in note for note in proposal.notes)


def test_a_mostly_failing_skill_is_called_what_it_is(store):
    store.save(_base())
    _run(store, "weekly-report", ok=False, verdict="failed", times=2)
    _run(store, "weekly-report", ok=True, verdict="verified", times=1)

    proposal = propose_evolution("weekly-report", store=store)
    weight = 2 / 3
    assert weight >= FAILURE_WEIGHT_FOR_REMOVAL
    assert any("does not work" in note for note in proposal.notes)


def test_a_run_that_stopped_at_a_gate_is_not_called_a_broken_plan(store):
    """`record_run(ok=False)` is also how a run parked awaiting approval lands.

    A live run made this exact distinction necessary: three runs stopped for
    approval and the note claimed "the runs' own notes say what broke" -- but a
    parked run's note says only that it is parked. The note may not imply a
    cause it did not read.
    """
    store.save(_base())
    _run(store, "weekly-report", ok=False, verdict="unknown", times=3)

    proposal = propose_evolution("weekly-report", store=store)
    joined = " ".join(proposal.notes)
    assert "did not reach a verified verdict" in joined
    # It must not claim the runs explained why when they only recorded a state.
    assert "notes say what broke" not in joined


def test_partial_runs_are_not_blamed_on_a_plan_the_fix_repairs(store):
    """The store counts `partial` as a failure, because only a verified run is a
    success. A live run showed 3/3 "failed" for a plan whose one real condition
    passed every time and whose other condition nobody could check -- the very
    thing the proposal removes. The note must not then say the plan is broken.
    """
    store.save(_base(verification=[
        {"condition_id": "C1", "kind": "file_contains",
         "path": "/tmp/rep.txt", "needle": "x"},
        {"condition_id": "C2", "kind": "the_vibes_are_right"}]))
    _run(store, "weekly-report", ok=False, verdict="partial", times=3)

    proposal = propose_evolution("weekly-report", store=store)
    assert UNCHECKABLE_CONDITION in proposal.reasons
    joined = " ".join(proposal.notes)
    assert "run the new version" in joined
    assert "skill that does not work yet" not in joined


# -- nothing is written until somebody accepts ------------------------------

def test_proposing_does_not_change_the_stored_skill(store):
    store.save(_base(verification=[
        {"condition_id": "C1", "kind": "file_contains",
         "path": "/tmp/rep.txt", "needle": "x"},
        {"condition_id": "C2", "kind": "not_a_real_kind"}]))
    _run(store, "weekly-report", ok=True, verdict="unknown", times=3)

    before = store.get("weekly-report").to_dict()
    propose_evolution("weekly-report", store=store)
    after = store.get("weekly-report").to_dict()
    assert before == after
    assert len([v for v in after["verification"]]) == 2


def test_accepting_stores_a_new_version_and_keeps_the_old(store):
    store.save(_base(verification=[
        {"condition_id": "C1", "kind": "file_contains",
         "path": "/tmp/rep.txt", "needle": "x"},
        {"condition_id": "C2", "kind": "not_a_real_kind"}]))
    _run(store, "weekly-report", ok=True, verdict="unknown", times=3)

    proposal = propose_evolution("weekly-report", store=store)
    stored = store.save(SkillManifest.from_dict(proposal.manifest))

    assert stored.version == 2
    assert len(stored.verification) == 1
    # v1 is still there, unchanged.
    old = store.get("weekly-report", version=1)
    assert len(old.verification) == 2


# -- through the tool calls a person makes ----------------------------------

def test_interpret_run_reports_never_checked_conditions(store):
    store.save(_base())
    _run(store, "weekly-report", ok=True, verdict="unknown", times=3)

    body = interpret_run("weekly-report", store=store)
    assert body["runs"] == 3
    assert "note" in body


# -- over the wire -----------------------------------------------------------

def test_the_lab_is_served_over_http():
    from tests.conftest import TEST_TOKEN
    from fastapi.testclient import TestClient
    from jarvis.api.app import app

    client = TestClient(app, base_url="http://127.0.0.1:8765")
    headers = {"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"}
    body = client.get("/skill-evolution", headers=headers).json()
    assert body["min_runs"] == MIN_RUNS_FOR_EVOLUTION
    assert "add a capability or a scope prefix" in body["will_not_change"]


def test_an_unknown_skill_is_a_404_over_http(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    from tests.conftest import TEST_TOKEN
    from fastapi.testclient import TestClient
    from jarvis.api.app import app

    client = TestClient(app, base_url="http://127.0.0.1:8765")
    headers = {"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"}
    assert client.get("/skill-evolution/nope", headers=headers).status_code == 404


def test_accepting_a_manifest_for_another_skill_is_a_422(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    from tests.conftest import TEST_TOKEN
    from fastapi.testclient import TestClient
    from jarvis.api.app import app

    client = TestClient(app, base_url="http://127.0.0.1:8765")
    headers = {"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"}
    body = {"manifest": _base().to_dict()}
    response = client.post("/skill-evolution/other-skill/accept",
                           headers=headers, json=body)
    assert response.status_code == 422
    assert "different skill" in response.json()["detail"]


def test_accepting_stores_the_version_over_http(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    from tests.conftest import TEST_TOKEN
    from fastapi.testclient import TestClient
    from jarvis.api.app import app

    client = TestClient(app, base_url="http://127.0.0.1:8765")
    headers = {"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"}
    response = client.post("/skill-evolution/weekly-report/accept",
                           headers=headers,
                           json={"manifest": _base().to_dict()})
    assert response.status_code == 200, response.text
    assert response.json()["version"] == 1


def test_accepting_with_no_body_stores_what_the_lab_proposed(tmp_path, monkeypatch):
    """A client that sends nothing gets the proposal re-derived server-side, so
    what lands on disk is exactly what the lab showed -- not a lossy echo."""
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    from jarvis.core.skills import SkillStore

    store = SkillStore()
    store.save(_base(verification=[
        {"condition_id": "C1", "kind": "file_contains",
         "path": "/tmp/rep.txt", "needle": "x"},
        {"condition_id": "C2", "kind": "not_a_real_kind"}]))
    _run(store, "weekly-report", ok=True, verdict="verified", times=3)

    from tests.conftest import TEST_TOKEN
    from fastapi.testclient import TestClient
    from jarvis.api.app import app

    client = TestClient(app, base_url="http://127.0.0.1:8765")
    headers = {"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"}
    response = client.post("/skill-evolution/weekly-report/accept", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["version"] == 2
    stored = SkillStore().get("weekly-report")
    assert [v["kind"] for v in stored.verification] == ["file_contains"]


def test_accepting_when_nothing_is_proposed_is_a_409(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    from jarvis.core.skills import SkillStore

    SkillStore().save(_base())
    from tests.conftest import TEST_TOKEN
    from fastapi.testclient import TestClient
    from jarvis.api.app import app

    client = TestClient(app, base_url="http://127.0.0.1:8765")
    headers = {"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"}
    # No runs yet, so the lab proposes nothing.
    response = client.post("/skill-evolution/weekly-report/accept", headers=headers)
    assert response.status_code == 409
    assert "no new version" in response.json()["detail"]