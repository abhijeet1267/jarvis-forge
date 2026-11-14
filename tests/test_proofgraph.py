"""ProofGraph / ProofLens: every claim is tied to an observation, or says it is not.

The failure this guards against is a report that *looks* like proof. A mission
panel that lists "3 conditions, all green" without saying what was observed is
indistinguishable from one that never checked anything. These tests pin that the
graph distinguishes supported from unsupported claims, and that the integrity
sentence does not overstate what a hash chain proves.
"""

from __future__ import annotations

import pytest

from jarvis.core.missions import ConditionStatus
from jarvis.core.proofgraph import ProofGraph


@pytest.fixture()
def service():
    from jarvis.core.missions import MissionService
    from jarvis.db.database import get_db

    return MissionService(get_db())


def _audit_row(mission_id: str, condition_id: str, observation: str,
               result: str = "verified", log_id: str = "LOG-1") -> dict:
    return {
        "log_id": log_id,
        "created_at": "2026-11-14T10:00:00+00:00",
        "result": result,
        "detail": {"mission_id": mission_id, "condition_id": condition_id,
                   "observation": observation},
    }


def test_a_verified_claim_carries_the_observation_that_supports_it(service):
    mission = service.create("check the README", conditions=[
        {"statement": "README documents --fast", "verifier": "file content"}])
    service.record_condition(mission.mission_id, "C1",
                             status=ConditionStatus.VERIFIED,
                             observation="README.md contains '--fast' (sha256:abc123…)")

    graph = ProofGraph(service.get(mission.mission_id), [
        _audit_row(mission.mission_id, "C1",
                   "README.md contains '--fast' (sha256:abc123…)")])

    report = graph.report()

    assert report["claims"], report
    claim = report["claims"][0]
    assert claim["supported"] is True
    assert claim["observations"][0]["observation"].startswith("README.md contains")
    assert claim["contradictions"] == []
    assert report["unsupported_claims"] == []


def test_a_claim_with_no_evidence_is_reported_as_unsupported(service):
    """No observation is not a pass, and the report must say so."""
    mission = service.create("check something", conditions=[
        {"statement": "the service answers 200", "verifier": "http"}])

    report = ProofGraph(service.get(mission.mission_id), []).report()

    claim = report["claims"][0]
    assert claim["supported"] is False
    assert claim["observations"] == []
    assert report["unsupported_claims"] == [claim["condition_id"]]


def test_a_contradiction_is_linked_not_hidden(service):
    mission = service.create("check the file", conditions=[
        {"statement": "the flag is documented", "verifier": "file content"}])
    service.record_condition(mission.mission_id, "C1",
                             status=ConditionStatus.FAILED,
                             observation="the flag is absent")

    report = ProofGraph(service.get(mission.mission_id), [
        _audit_row(mission.mission_id, "C1", "the flag is absent", result="failed")]).report()

    claim = report["claims"][0]
    assert claim["status"] == "failed"
    assert claim["contradictions"], "a failed observation must appear as a contradiction"
    assert report["verdict"] == "failed"


def test_evidence_from_another_mission_is_not_attributed_to_this_one(service):
    """Audit rows are global; attribution has to be by mission id."""
    mission = service.create("mine", conditions=[
        {"statement": "x", "verifier": "y"}])

    report = ProofGraph(service.get(mission.mission_id), [
        _audit_row("MIS-somebody-else", "C1", "unrelated observation")]).report()

    assert report["claims"][0]["observations"] == []
    assert report["unsupported_claims"]


def test_the_integrity_sentence_does_not_overclaim(service):
    mission = service.create("m", conditions=[{"statement": "x"}])
    graph = ProofGraph(service.get(mission.mission_id), [])

    no_chain = graph.report()["evidence_integrity"]
    assert "not read" in no_chain

    intact = graph.report(chain={"ok": True, "verified_of": 12})["evidence_integrity"]
    assert "intact across 12 records" in intact
    # It must not imply the log is externally provable.
    assert "not signed" in intact and "anchored" in intact

    broken = graph.report(chain={"ok": False})["evidence_integrity"]
    assert "BROKEN" in broken


def test_the_graph_exposes_nodes_and_edges_for_the_view(service):
    mission = service.create("m", conditions=[{"statement": "x"}],
                             steps=[{"title": "do it", "tool": "read_file"}])
    graph = ProofGraph(service.get(mission.mission_id), [])
    payload = graph.to_dict()

    kinds = {n["kind"] for n in payload["nodes"]}
    assert {"mission", "step", "condition"} <= kinds
    relations = {e["relation"] for e in payload["edges"]}
    assert relations, "the graph must express how nodes relate"
    assert all(n["node_id"] for n in payload["nodes"])

# -- outcome receipts ------------------------------------------------------

def test_a_receipt_records_the_verdict_and_resolvable_evidence(service):
    from jarvis.core.proofgraph import outcome_receipt

    mission = service.create("document the flag", conditions=[
        {"statement": "README mentions --fast", "verifier": "file content"}])
    service.record_condition(mission.mission_id, "C1",
                             status=ConditionStatus.VERIFIED,
                             observation="README.md contains '--fast'")

    rows = [_audit_row(mission.mission_id, "C1", "README.md contains '--fast'",
                       log_id="LOG-real")]
    receipt = outcome_receipt(service.get(mission.mission_id), rows,
                              chain={"ok": True, "verified_of": 1})

    assert receipt["receipt_version"] == 1
    assert receipt["mission_id"] == mission.mission_id
    assert receipt["evidence_references"], receipt
    assert receipt["unresolvable_evidence"] == []
    assert "independent observer" in receipt["honesty"]


def test_a_receipt_names_evidence_it_cannot_resolve(service):
    """A citation nobody can follow must be visible, not dropped."""
    from jarvis.core.proofgraph import outcome_receipt

    mission = service.create("m", conditions=[{"statement": "x"}])
    receipt = outcome_receipt(service.get(mission.mission_id),
                              [_audit_row(mission.mission_id, "C1", "observed",
                                          log_id="LOG-a")])

    # Force a reference to an id that is not in the rows the receipt was given.
    mission.conditions[0].evidence = ["LOG-missing"]
    service._persist_conditions(mission, actor="test")
    receipt = outcome_receipt(service.get(mission.mission_id),
                              [_audit_row(mission.mission_id, "C1", "observed",
                                          log_id="LOG-a")])
    assert "unresolvable_evidence" in receipt
