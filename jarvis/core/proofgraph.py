"""ProofGraph: what a mission's claims actually rest on, and how to read it.

Every mission makes claims -- "the README documents the flag", "the endpoint
answers 200". This module turns those claims into a small evidence graph and a
readable report, so a person can ask *why* Vantage believes something and get
an answer that names observations rather than assurances.

**Evidence is the existing hash-chained audit log**, not a second store. Each
verification already writes a record with ``mission_id`` and ``condition_id`` in
its detail; this module reads those records back and links them to the condition
they support. Two consequences worth stating plainly:

* The chain's job is to make silent *editing* detectable, because each record
  carries the previous record's hash. It is not a signature and not an external
  anchor: someone who can rewrite the whole chain can produce a consistent one.
  The report says this in words rather than implying tamper-proofness.
* A condition with no evidence record is reported as unsupported. That is the
  common case for a mission whose verifier could not observe anything, and
  showing it as "verified" would be the exact failure the contract exists to
  prevent.

The node and edge vocabulary is deliberately small; it is the part the UI and
the export both need, not a general graph engine.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

#: Node kinds. Each corresponds to something a person can point at.
MISSION = "mission"
STEP = "step"
CONDITION = "condition"
OBSERVATION = "observation"
CONTRADICTION = "contradiction"

#: Edge kinds -- how one node relates to another.
VERIFIES = "VERIFIES"
CONTRADICTS = "CONTRADICTS"
PRODUCED = "PRODUCED"
OBSERVED_BY = "OBSERVED_BY"


@dataclass
class Node:
    node_id: str
    kind: str
    label: str
    detail: Dict[str, Any] = field(default_factory=dict)
    #: For an observation, the audit record it came from.
    evidence_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "kind": self.kind,
            "label": self.label,
            "detail": dict(self.detail),
            "evidence_id": self.evidence_id,
        }


@dataclass
class Edge:
    source: str
    target: str
    relation: str

    def to_dict(self) -> Dict[str, Any]:
        return {"source": self.source, "target": self.target, "relation": self.relation}


class ProofGraph:
    """The evidence for one mission, assembled from the mission and the audit log."""

    def __init__(self, mission: Any, audit_rows: Optional[List[Dict[str, Any]]] = None) -> None:
        self.mission = mission
        self.nodes: List[Node] = []
        self.edges: List[Edge] = []
        self._by_id: Dict[str, Node] = {}
        self._build(audit_rows or [])

    # -- construction -------------------------------------------------------
    def _add(self, node: Node) -> Node:
        self.nodes.append(node)
        self._by_id[node.node_id] = node
        return node

    def _link(self, source: str, target: str, relation: str) -> None:
        self.edges.append(Edge(source, target, relation))

    def _build(self, audit_rows: List[Dict[str, Any]]) -> None:
        mission_id = self.mission.mission_id
        self._add(Node(mission_id, MISSION, self.mission.outcome or self.mission.request,
                       detail={"state": self.mission.state,
                               "verdict": self.mission.verdict()}))

        for step in self.mission.steps:
            step_node = self._add(Node(
                f"{mission_id}:{step.step_id}", STEP, step.title,
                detail={"status": step.status, "tool": step.tool,
                        "detail": step.detail}))
            self._link(mission_id, step_node.node_id, PRODUCED)

        # Audit rows for this mission, oldest first so the report reads forward.
        rows = [r for r in audit_rows
                if _mission_of(r) == mission_id]
        rows.sort(key=lambda r: str(r.get("created_at") or ""))

        for condition in self.mission.conditions:
            condition_node = self._add(Node(
                f"{mission_id}:{condition.condition_id}", CONDITION,
                condition.statement,
                detail={"status": condition.status,
                        "verifier": condition.verifier}))
            self._link(mission_id, condition_node.node_id, VERIFIES)

            observations = [r for r in rows
                            if str((_detail(r) or {}).get("condition_id") or "")
                            == condition.condition_id]

            for row in observations:
                log_id = str(row.get("log_id") or "")
                observation = self._add(Node(
                    log_id or f"OBS-{len(self.nodes)}",
                    OBSERVATION,
                    str((_detail(row) or {}).get("observation") or "")[:160],
                    detail={"at": row.get("created_at"), "result": row.get("result")},
                    evidence_id=log_id))
                self._link(condition_node.node_id, observation.node_id, OBSERVED_BY)
                if str(row.get("result")) == "failed":
                    contradiction = self._add(Node(
                        f"CON-{log_id}", CONTRADICTION,
                        "the observation contradicted this claim",
                        detail={"evidence_id": log_id}))
                    self._link(condition_node.node_id, contradiction.node_id,
                               CONTRADICTS)

            if not observations:
                # No evidence is itself information: the claim rests on nothing.
                self._add(Node(
                    f"{condition.condition_id}:unsupported", OBSERVATION,
                    "no evidence was recorded for this claim",
                    detail={"unsupported": True}))

    # -- views --------------------------------------------------------------
    def unsupported_claims(self) -> List[str]:
        """Condition ids with no observation behind them."""
        out = []
        for node in self.nodes:
            if node.kind is CONDITION and not any(
                    edge.source == node.node_id and edge.relation == OBSERVED_BY
                    for edge in self.edges):
                out.append(node.node_id)
        return out

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mission_id": self.mission.mission_id,
            "verdict": self.mission.verdict(),
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
            "unsupported": self.unsupported_claims(),
        }

    def report(self, *, chain: Optional[Dict[str, Any]] = None,
               include_evidence_ids: bool = True) -> Dict[str, Any]:
        """The ProofLens payload: a claim-by-claim answer to "why do you believe this?".

        ``chain`` is the audit log's own ``verify_chain`` result, when the caller
        has it, so the report can state whether the evidence is intact -- and
        say what that does and does not guarantee.
        """
        claims = []
        for node in self.nodes:
            if node.kind is not CONDITION:
                continue
            observations = [self._by_id[e.target] for e in self.edges
                            if e.source == node.node_id and e.relation == OBSERVED_BY
                            and e.target in self._by_id]
            contradictions = [self._by_id[e.target] for e in self.edges
                              if e.source == node.node_id
                              and e.relation == CONTRADICTS
                              and e.target in self._by_id]
            claims.append({
                "condition_id": node.node_id,
                "claim": node.label,
                "status": (node.detail or {}).get("status"),
                "supported": bool(observations),
                "observations": [
                    {"observation": o.label,
                     **({"evidence_id": o.evidence_id} if include_evidence_ids else {})}
                    for o in observations],
                "contradictions": [
                    {"reason": c.label,
                     **({"evidence_id": c.detail.get("evidence_id")}
                        if include_evidence_ids else {})}
                    for c in contradictions],
            })

        payload: Dict[str, Any] = {
            "mission_id": self.mission.mission_id,
            "request": self.mission.request,
            "outcome": self.mission.outcome,
            "state": self.mission.state,
            "verdict": self.mission.verdict(),
            "claims": claims,
            "steps": [{"step_id": s.step_id, "title": s.title,
                       "status": s.status, "detail": s.detail}
                      for s in self.mission.steps],
            "unsupported_claims": self.unsupported_claims(),
            "evidence_integrity": _integrity_sentence(chain),
        }
        return payload


def outcome_receipt(mission: Any, audit_rows: Optional[List[Dict[str, Any]]] = None,
                    *, chain: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """A portable record of what a mission did and what proves it.

    Every evidence reference it emits is checked against the audit rows it was
    given. A reference that resolves to no record is listed under
    ``unresolvable_evidence`` rather than quietly dropped: a receipt whose
    citations cannot be followed is worse than one that admits it.
    """
    rows = list(audit_rows or [])
    known_ids = {str(r.get("log_id") or "") for r in rows}
    graph = ProofGraph(mission, rows)
    report = graph.report(chain=chain)

    references: List[Dict[str, Any]] = []
    unresolvable: List[str] = []
    for claim in report["claims"]:
        for observation in claim["observations"]:
            evidence_id = str(observation.get("evidence_id") or "")
            references.append({"condition_id": claim["condition_id"],
                               "evidence_id": evidence_id,
                               "observation": observation["observation"]})
            if evidence_id and evidence_id not in known_ids:
                unresolvable.append(evidence_id)

    return {
        "receipt_version": 1,
        "mission_id": mission.mission_id,
        "objective": mission.outcome or mission.request,
        "request": mission.request,
        "state": mission.state,
        "verdict": mission.verdict(),
        # Which plan this receipt describes. `version` is the row counter that
        # moves on every write (each step marked done), so it is not the plan's
        # revision and a receipt reporting it would claim a plan changed when it
        # never did. `plan_revision` moves only when the plan is steered.
        "plan_revision": getattr(mission, "plan_revision", 0),
        "record_version": mission.version,
        "conditions": [
            {"condition_id": c.condition_id, "statement": c.statement,
             "status": c.status, "observation": c.observation,
             "evidence": list(c.evidence)}
            for c in mission.conditions],
        "steps": [
            {"step_id": s.step_id, "title": s.title, "tool": s.tool,
             "status": s.status, "detail": s.detail}
            for s in mission.steps],
        "recovery_attempts": mission.recovery_attempts,
        "evidence_references": references,
        "unresolvable_evidence": unresolvable,
        "evidence_integrity": report["evidence_integrity"],
        "honesty": (
            "A verified receipt means an independent observer checked each stated "
            "condition. It does not mean every possible problem was checked, and "
            "the hash chain it cites detects editing inside the log rather than "
            "proving the log to a third party."),
    }


def _integrity_sentence(chain: Optional[Dict[str, Any]]) -> str:
    """What the hash chain does and does not establish, in one honest sentence."""
    if not chain:
        return ("The evidence chain was not read for this report, so nothing here "
                "is claimed about its integrity.")
    if not chain.get("ok", chain.get("intact", False)):
        return ("The evidence chain is BROKEN: a record does not match the hash it "
                "was written with, so entries may have been edited or removed.")
    checked = chain.get("verified_of", chain.get("checked", 0))
    return (f"The evidence chain is intact across {checked} records: each one still "
            "matches the hash of the entry before it. That detects an edit inside "
            "the chain; it does not make the records externally provable, because "
            "this log is not signed and not anchored anywhere outside this machine.")


# -- helpers ---------------------------------------------------------------

def _detail(row: Dict[str, Any]) -> Dict[str, Any]:
    raw = row.get("detail")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def _mission_of(row: Dict[str, Any]) -> str:
    return str((_detail(row) or {}).get("mission_id") or "")