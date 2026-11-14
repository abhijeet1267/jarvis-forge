"""Mission contracts: a durable objective, not a chat command.

A mission is the unit of *intent* in Vantage. The user states an outcome; this
module records it as a typed contract with explicit success conditions, then
tracks it through a validated state machine until every condition is either
**verified** by an independent observer or honestly reported as failed or
unknown.

Why a contract rather than a plan: a plan says *how* something will be done and
is disposable. A contract says *what must be true when we are done* and survives
the plan changing, the app restarting, and the model being unavailable. Without
it, "done" means "the last step did not raise", which is the precise failure mode
this project has been fixing (a successful subprocess exit is not proof of a real
outcome).

Design notes that are load-bearing:

* **States are validated.** A mission cannot reach ``COMPLETED`` without at least
  one verified condition, and cannot skip ``AWAITING_APPROVAL`` to ``RUNNING``
  when a condition needs approval. Illegal transitions raise, rather than being
  silently ignored, so a bug in a caller surfaces instead of corrupting state.
* **Transitions are idempotent and attributable.** Every change is written in one
  transaction with the actor and the reason, so a crash mid-transition leaves
  either the old state or the new one -- never half of each.
* **A step is not verified because it ran.** ``mark_step`` records the outcome of
  an independent observation, and ``VERIFIED`` requires evidence that names what
  was observed. The verifier is separate from the executor by construction.
* **Restart safety.** ``incomplete_missions`` finds work that was mid-flight when
  the process died, so a relaunch can resume rather than replay.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from ..logging_setup import get_logger

log = get_logger("core.missions")

#: Every state a mission can occupy. The three outcomes at the end are the ones
#: that mean "finished": the person reading the list must be able to tell a
#: verified success from a partial one at a glance.
class MissionState(str, Enum):
    DRAFT = "draft"
    PLANNED = "planned"
    AWAITING_APPROVAL = "awaiting_approval"
    RUNNING = "running"
    VERIFYING = "verifying"
    PAUSED = "paused"
    BLOCKED = "blocked"
    RECOVERING = "recovering"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PARTIALLY_COMPLETED = "partially_completed"


#: Terminal states. Nothing transitions out of these.
TERMINAL: frozenset = frozenset({
    MissionState.COMPLETED, MissionState.FAILED,
    MissionState.CANCELLED, MissionState.PARTIALLY_COMPLETED,
})

#: The legal transitions. Declared as data so the rules are readable and a test
#: can enumerate them; a transition missing from this map is refused.
TRANSITIONS: Dict[MissionState, frozenset] = {
    MissionState.DRAFT: frozenset({MissionState.PLANNED, MissionState.CANCELLED}),
    MissionState.PLANNED: frozenset({
        MissionState.AWAITING_APPROVAL, MissionState.RUNNING,
        MissionState.CANCELLED, MissionState.BLOCKED}),
    MissionState.AWAITING_APPROVAL: frozenset({
        MissionState.RUNNING, MissionState.CANCELLED, MissionState.BLOCKED}),
    # An approval discovered *during* execution (a step the gate marks CONFIRM)
    # parks the mission without unwinding it; the steps that already ran keep
    # their results and resume after the person answers.
    MissionState.RUNNING: frozenset({
        MissionState.VERIFYING, MissionState.RECOVERING, MissionState.PAUSED,
        MissionState.AWAITING_APPROVAL, MissionState.BLOCKED,
        MissionState.FAILED, MissionState.CANCELLED,
        MissionState.PARTIALLY_COMPLETED}),
    MissionState.VERIFYING: frozenset({
        MissionState.COMPLETED, MissionState.PARTIALLY_COMPLETED,
        MissionState.RECOVERING, MissionState.FAILED, MissionState.PAUSED,
        MissionState.CANCELLED}),
    MissionState.RECOVERING: frozenset({
        MissionState.RUNNING, MissionState.FAILED, MissionState.BLOCKED,
        MissionState.PAUSED, MissionState.CANCELLED}),
    MissionState.PAUSED: frozenset({
        MissionState.RUNNING, MissionState.CANCELLED, MissionState.BLOCKED}),
    # BLOCKED is recoverable by design: a precondition can become true later
    # (a device reconnects, a provider key is added) without a new mission.
    MissionState.BLOCKED: frozenset({
        MissionState.PLANNED, MissionState.RUNNING, MissionState.CANCELLED}),
    MissionState.COMPLETED: frozenset(),
    MissionState.FAILED: frozenset(),
    MissionState.CANCELLED: frozenset(),
    MissionState.PARTIALLY_COMPLETED: frozenset(),
}

#: The three outcomes a success condition can reach. There is deliberately no
#: "probably": an unverifiable condition stays UNKNOWN rather than drifting up.
class ConditionStatus(str, Enum):
    PENDING = "pending"
    VERIFIED = "verified"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass
class SuccessCondition:
    """One thing that must be true, and how an independent observer checks it."""

    condition_id: str
    statement: str
    #: A free-form description of who checks it ("file hash", "http body", ...).
    #: The mission service never trusts this alone; it pairs with a verifier.
    verifier: str = ""
    status: str = ConditionStatus.PENDING.value
    #: What the observer actually saw, when it ran.
    observation: str = ""
    #: The evidence node ids that support or contradict this condition.
    evidence: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "condition_id": self.condition_id,
            "statement": self.statement,
            "verifier": self.verifier,
            "status": self.status,
            "observation": self.observation,
            "evidence": list(self.evidence),
        }


@dataclass
class MissionStep:
    """A step in the plan. A step running is not a step succeeding."""

    step_id: str
    title: str
    tool: str = ""
    params: Dict[str, Any] = field(default_factory=dict)
    status: str = "pending"          # pending|running|done|failed|skipped
    detail: str = ""
    #: Which success condition this step is meant to satisfy, if any. This is the
    #: join between "what I did" and "what must be true".
    satisfies: str = ""
    #: An idempotency key so a resumed mission does not repeat a side effect.
    idempotency_key: str = ""
    #: Steps that must finish before this one may start. An empty list means the
    #: step is immediately eligible, which is what lets independent work run at
    #: the same time.
    depends_on: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_id": self.step_id,
            "title": self.title,
            "tool": self.tool,
            "params": dict(self.params),
            "status": self.status,
            "detail": self.detail,
            "satisfies": self.satisfies,
            "idempotency_key": self.idempotency_key,
            "depends_on": list(self.depends_on),
        }


@dataclass
class Mission:
    """The contract itself."""

    mission_id: str
    request: str
    outcome: str
    state: str = MissionState.DRAFT.value
    #: What the mission is allowed to touch. A step outside this is refused by the
    #: permission gate, not merely discouraged.
    scope: Dict[str, Any] = field(default_factory=dict)
    conditions: List[SuccessCondition] = field(default_factory=list)
    steps: List[MissionStep] = field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0
    #: Audit ids, in order, of everything that touched this mission.
    evidence: List[str] = field(default_factory=list)
    #: Why it is in its current state, in words a person can read.
    reason: str = ""
    #: How many recovery attempts have been made; bounded by the runner.
    recovery_attempts: int = 0
    version: int = 1

    # -- derived views ------------------------------------------------------
    @property
    def is_terminal(self) -> bool:
        return MissionState(self.state) in TERMINAL

    def condition_summary(self) -> Dict[str, int]:
        counts = {"verified": 0, "failed": 0, "unknown": 0, "pending": 0}
        for condition in self.conditions:
            counts[condition.status] = counts.get(condition.status, 0) + 1
        return counts

    def verdict(self) -> str:
        """The honest one-word answer to "did this mission do what it promised?".

        This is the whole point of the contract: the app must be able to say
        ``verified`` only when every condition was independently confirmed, and
        ``partial`` when some were not. Anything else -- notably a mission whose
        steps all "completed" but whose conditions were never checked -- is
        unknown, never success.
        """
        if not self.conditions:
            return "unknown"
        counts = self.condition_summary()
        if counts["failed"]:
            return "failed"
        if counts["pending"] or counts["unknown"]:
            return "partial" if counts["verified"] else "unknown"
        return "verified"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mission_id": self.mission_id,
            "request": self.request,
            "outcome": self.outcome,
            "state": self.state,
            "scope": dict(self.scope),
            "conditions": [c.to_dict() for c in self.conditions],
            "steps": [s.to_dict() for s in self.steps],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "evidence": list(self.evidence),
            "reason": self.reason,
            "recovery_attempts": self.recovery_attempts,
            "version": self.version,
            "is_terminal": self.is_terminal,
            "condition_summary": self.condition_summary(),
            "verdict": self.verdict(),
        }


class MissionTransitionError(ValueError):
    """An illegal state transition. Raised, never silently ignored."""


class MissionService:
    """Creates missions, moves them through the state machine, and reads them back.

    All state lives in SQLite so a mission survives the app restarting. The
    service holds no in-memory mission state that could disagree with the row.
    """

    def __init__(self, db: Any) -> None:
        self.db = db

    # -- creation -----------------------------------------------------------
    def create(
        self,
        request: str,
        outcome: str = "",
        *,
        scope: Optional[Dict[str, Any]] = None,
        conditions: Optional[List[Dict[str, Any]]] = None,
        steps: Optional[List[Dict[str, Any]]] = None,
        actor: str = "user",
    ) -> Mission:
        """Open a mission in DRAFT.

        Creation is separate from planning on purpose: a draft is the record of
        what was asked, even if planning fails. Losing the request because the
        planner errored is how a system ends up with no explanation for what it
        did.
        """
        from .models import new_id

        now = time.time()
        mission = Mission(
            mission_id=new_id("MIS"),
            request=str(request or "").strip(),
            outcome=str(outcome or request or "").strip(),
            scope=dict(scope or {}),
            created_at=now,
            updated_at=now,
        )
        for raw in (conditions or []):
            mission.conditions.append(SuccessCondition(
                condition_id=str(raw.get("condition_id") or f"C{len(mission.conditions) + 1}"),
                statement=str(raw.get("statement") or ""),
                verifier=str(raw.get("verifier") or ""),
            ))
        for raw in (steps or []):
            mission.steps.append(MissionStep(
                step_id=str(raw.get("step_id") or f"S{len(mission.steps) + 1}"),
                title=str(raw.get("title") or ""),
                tool=str(raw.get("tool") or ""),
                params=dict(raw.get("params") or {}),
                satisfies=str(raw.get("satisfies") or ""),
                idempotency_key=str(raw.get("idempotency_key") or ""),
                depends_on=[str(d) for d in (raw.get("depends_on") or [])],
            ))

        if not mission.request:
            raise ValueError("a mission needs the request that created it")

        self._insert(mission, actor=actor)
        log.info("mission created", extra_fields={
            "mission_id": mission.mission_id,
            "conditions": len(mission.conditions),
            "steps": len(mission.steps),
        })
        return mission

    def _insert(self, mission: Mission, *, actor: str) -> None:
        with self.db.transaction() if hasattr(self.db, "transaction") else _nullcontext():
            self.db.execute(
                """INSERT INTO missions
                     (mission_id, request, outcome, state, scope, conditions, steps,
                      created_at, updated_at, evidence, reason, recovery_attempts,
                      version, last_actor)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    mission.mission_id, mission.request, mission.outcome, mission.state,
                    json.dumps(mission.scope), json.dumps([c.to_dict() for c in mission.conditions]),
                    json.dumps([s.to_dict() for s in mission.steps]),
                    _iso(mission.created_at), _iso(mission.updated_at),
                    json.dumps(mission.evidence), mission.reason,
                    mission.recovery_attempts, mission.version, actor,
                ),
            )

    # -- transitions --------------------------------------------------------
    def transition(self, mission_id: str, to: MissionState, *,
                   actor: str = "system", reason: str = "") -> Mission:
        """Move a mission to a new state, enforcing the transition table."""
        mission = self.get(mission_id)
        if mission is None:
            raise MissionTransitionError(f"no mission {mission_id!r}")

        current = MissionState(mission.state)
        target = MissionState(to)

        if current == target:
            return mission  # idempotent: a repeat is not an error

        allowed = TRANSITIONS.get(current, frozenset())
        if target not in allowed:
            raise MissionTransitionError(
                f"{current.value} -> {target.value} is not a legal transition "
                f"(from {current.value} the legal targets are: "
                f"{', '.join(sorted(s.value for s in allowed)) or 'none'})")

        # Completing requires evidence, not merely having run.
        if target is MissionState.COMPLETED:
            if not mission.conditions:
                raise MissionTransitionError(
                    "refusing to complete a mission with no success conditions: "
                    "with nothing to check, 'done' would be an assertion")
            unverified = [c.condition_id for c in mission.conditions
                          if c.status != ConditionStatus.VERIFIED.value]
            if unverified:
                raise MissionTransitionError(
                    "refusing to complete: these conditions are not verified: "
                    + ", ".join(unverified)
                    + ". Use PARTIALLY_COMPLETED to report some verified and some not.")

        return self._apply(mission, target, actor=actor, reason=reason)

    def _apply(self, mission: Mission, target: MissionState, *,
               actor: str, reason: str) -> Mission:
        now = time.time()
        self.db.execute(
            """UPDATE missions
                  SET state = ?, updated_at = ?, reason = ?, version = version + 1,
                      last_actor = ?
                WHERE mission_id = ? AND version = ?""",
            (target.value, _iso(now), reason, actor, mission.mission_id, mission.version),
        )
        mission.state = target.value
        mission.updated_at = now
        mission.reason = reason
        mission.version += 1
        return mission

    # -- steps and conditions ----------------------------------------------
    def mark_step(self, mission_id: str, step_id: str, *, status: str,
                  detail: str = "", actor: str = "system") -> Mission:
        """Record what happened to one step. ``done`` means it ran, not that it worked."""
        if status not in ("pending", "running", "done", "failed", "skipped"):
            raise ValueError(f"{status!r} is not a step status")
        mission = self.get(mission_id)
        if mission is None:
            raise MissionTransitionError(f"no mission {mission_id!r}")
        for step in mission.steps:
            if step.step_id == step_id:
                step.status = status
                step.detail = detail
                break
        else:
            raise KeyError(f"no step {step_id!r} in mission {mission_id!r}")
        self._persist_steps(mission, actor=actor)
        return mission

    def record_condition(self, mission_id: str, condition_id: str, *,
                         status: ConditionStatus, observation: str = "",
                         evidence_id: str = "", actor: str = "verifier") -> Mission:
        """Record what an independent observer found for one success condition.

        Only a verifier calls this. A step's own success message is never passed
        in as ``observation`` -- the caller is responsible for having actually
        observed the world.
        """
        mission = self.get(mission_id)
        if mission is None:
            raise MissionTransitionError(f"no mission {mission_id!r}")
        for condition in mission.conditions:
            if condition.condition_id == condition_id:
                condition.status = ConditionStatus(status).value
                condition.observation = str(observation or "")
                if evidence_id and evidence_id not in condition.evidence:
                    condition.evidence.append(evidence_id)
                break
        else:
            raise KeyError(f"no condition {condition_id!r} in mission {mission_id!r}")
        self._persist_conditions(mission, actor=actor)
        return mission

    # -- persistence helpers ------------------------------------------------
    def _persist_steps(self, mission: Mission, *, actor: str) -> None:
        self.db.execute(
            "UPDATE missions SET steps = ?, updated_at = ?, version = version + 1, "
            "last_actor = ? WHERE mission_id = ? AND version = ?",
            (json.dumps([s.to_dict() for s in mission.steps]), _iso(time.time()),
             actor, mission.mission_id, mission.version),
        )
        mission.version += 1

    def _persist_conditions(self, mission: Mission, *, actor: str) -> None:
        self.db.execute(
            "UPDATE missions SET conditions = ?, updated_at = ?, version = version + 1, "
            "last_actor = ? WHERE mission_id = ? AND version = ?",
            (json.dumps([c.to_dict() for c in mission.conditions]), _iso(time.time()),
             actor, mission.mission_id, mission.version),
        )
        mission.version += 1

    # -- reading ------------------------------------------------------------
    def get(self, mission_id: str) -> Optional[Mission]:
        rows = self.db.query("SELECT * FROM missions WHERE mission_id = ?", (mission_id,))
        return _from_row(rows[0]) if rows else None

    def list(self, limit: int = 50, *, states: Optional[List[MissionState]] = None) -> List[Mission]:
        limit = max(1, min(int(limit), 200))
        if states:
            marks = ",".join("?" for _ in states)
            rows = self.db.query(
                f"SELECT * FROM missions WHERE state IN ({marks}) "
                "ORDER BY updated_at DESC LIMIT ?",
                tuple(s.value for s in states) + (limit,))
        else:
            rows = self.db.query(
                "SELECT * FROM missions ORDER BY updated_at DESC LIMIT ?", (limit,))
        return [_from_row(r) for r in rows]

    def incomplete_missions(self, limit: int = 50) -> List[Mission]:
        """Missions that were mid-flight when the process stopped.

        This is what makes a relaunch resume rather than replay: the caller can
        see exactly which missions were interrupted and in which state, instead
        of treating every stored mission as new.
        """
        rows = self.db.query(
            "SELECT * FROM missions WHERE state IN (?, ?, ?, ?) "
            "ORDER BY updated_at DESC LIMIT ?",
            (MissionState.RUNNING.value, MissionState.VERIFYING.value,
             MissionState.RECOVERING.value, MissionState.PAUSED.value, limit))
        return [_from_row(r) for r in rows]


# -- module helpers ---------------------------------------------------------

def _iso(epoch: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _from_row(row: Any) -> Mission:
    def _load(value: Any, default: Any) -> Any:
        if not value:
            return default
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return default

    conditions = [
        SuccessCondition(
            condition_id=str(c.get("condition_id") or ""),
            statement=str(c.get("statement") or ""),
            verifier=str(c.get("verifier") or ""),
            status=str(c.get("status") or ConditionStatus.PENDING.value),
            observation=str(c.get("observation") or ""),
            evidence=list(c.get("evidence") or []),
        )
        for c in _load(row["conditions"], [])
    ]
    steps = [
        MissionStep(
            step_id=str(s.get("step_id") or ""),
            title=str(s.get("title") or ""),
            tool=str(s.get("tool") or ""),
            params=dict(s.get("params") or {}),
            status=str(s.get("status") or "pending"),
            detail=str(s.get("detail") or ""),
            satisfies=str(s.get("satisfies") or ""),
            idempotency_key=str(s.get("idempotency_key") or ""),
            depends_on=[str(d) for d in (s.get("depends_on") or [])],
        )
        for s in _load(row["steps"], [])
    ]
    mission = Mission(
        mission_id=str(row["mission_id"]),
        request=str(row["request"] or ""),
        outcome=str(row["outcome"] or ""),
        state=str(row["state"]),
        scope=_load(row["scope"], {}),
        conditions=conditions,
        steps=steps,
        created_at=_epoch(row["created_at"]),
        updated_at=_epoch(row["updated_at"]),
        evidence=_load(row["evidence"], []),
        reason=str(row["reason"] or ""),
        recovery_attempts=int(row["recovery_attempts"] or 0),
        version=int(row["version"] or 1),
    )
    return mission


def _epoch(value: Any) -> float:
    from datetime import datetime

    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return 0.0


class _nullcontext:
    """Stand-in when the database wrapper has no transaction helper."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False