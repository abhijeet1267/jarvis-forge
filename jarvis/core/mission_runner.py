"""Run a mission: gate each step, execute it, then have something else check it.

The runner is deliberately small. It does not plan, it does not decide policy,
and it does not judge its own work. Its job is the sequence that the mission
contract exists to make safe:

1. **Gate** -- every step goes through the same `PermissionEngine` a chat action
   goes through. A mission is not a permission; naming an action in a plan does
   not authorize it.
2. **Execute** -- through the owning agent, exactly as the pipeline does, so a
   mission gains no capabilities a person could not get by asking directly.
3. **Verify** -- through `verify`, which observes the world *after* the step,
   with a different mechanism than the step used. A file write is verified by
   reading the file back and hashing it, not by the write returning OK.
4. **Record** -- the outcome is written to the mission and to the hash-chained
   audit log, so the evidence survives the process.

What the runner refuses to do is as important as what it does:

* It will not run a step the gate blocks. The mission stops in BLOCKED with the
  gate's own reason, so the person is told what policy objected to.
* It will not retry a failed step automatically. One approval buys one attempt;
  retry is a recovery decision (P4), made explicitly and bounded.
* It will not mark a condition VERIFIED on a step's success message. Conditions
  are set only by the verifier, and a verifier that cannot observe the world
  reports UNKNOWN rather than optimistically passing.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..logging_setup import get_logger
from .missions import ConditionStatus, Mission, MissionService, MissionState

log = get_logger("core.mission_runner")


@dataclass
class Verification:
    """What an independent observer concluded about one acceptance condition."""

    condition_id: str
    status: ConditionStatus
    observation: str
    #: Stable reference to the evidence (an audit log id, once recorded).
    evidence_id: str = ""


@dataclass
class StepOutcome:
    step_id: str
    ran: bool
    ok: bool
    detail: str = ""
    verification: Optional[Verification] = None


#: A verifier receives the mission and its scope and returns a Verification.
#: It must observe the world; it must not take a step's own message as input.
Verifier = Callable[[Mission], Verification]


class Verifiers:
    """Independent observers for the acceptance conditions this project can check.

    Each one answers a question about *state after the fact*. None of them reads
    the executor's result, which is what keeps "the tool said OK" from becoming
    "it worked".
    """

    @staticmethod
    def file_contains(path: str, needle: str, condition_id: str) -> Verifier:
        """The file exists and contains `needle`, re-read from disk."""

        def verify(mission: Mission) -> Verification:
            target = Path(path).expanduser()
            try:
                data = target.read_bytes()
            except FileNotFoundError:
                return Verification(condition_id, ConditionStatus.FAILED,
                                    f"{target} does not exist after the step")
            except OSError as exc:
                return Verification(condition_id, ConditionStatus.UNKNOWN,
                                    f"{target} could not be read: {exc}")
            text = data.decode("utf-8", "replace")
            digest = hashlib.sha256(data).hexdigest()[:16]
            if needle in text:
                return Verification(condition_id, ConditionStatus.VERIFIED,
                                    f"{target} contains {needle!r} "
                                    f"(sha256:{digest}, {len(data)} bytes)")
            return Verification(condition_id, ConditionStatus.FAILED,
                                f"{target} does not contain {needle!r} "
                                f"(sha256:{digest}, {len(data)} bytes)")

        return verify

    @staticmethod
    def file_hash(path: str, expected_sha256: str, condition_id: str) -> Verifier:
        """The file's content hashes to the expected value."""

        def verify(mission: Mission) -> Verification:
            target = Path(path).expanduser()
            try:
                data = target.read_bytes()
            except FileNotFoundError:
                return Verification(condition_id, ConditionStatus.FAILED,
                                    f"{target} does not exist")
            except OSError as exc:
                return Verification(condition_id, ConditionStatus.UNKNOWN,
                                    f"{target} could not be read: {exc}")
            actual = hashlib.sha256(data).hexdigest()
            if actual == expected_sha256:
                return Verification(condition_id, ConditionStatus.VERIFIED,
                                    f"{target} sha256 matches ({actual[:16]}…)")
            return Verification(condition_id, ConditionStatus.FAILED,
                                f"{target} sha256 is {actual[:16]}…, "
                                f"expected {expected_sha256[:16]}…")

        return verify

    @staticmethod
    def file_identical(path: str, source: str, condition_id: str) -> Verifier:
        """The file at ``path`` holds exactly the bytes at ``source``.

        Both are read at verification time, after the step ran, so this compares
        states rather than trusting a copy call's return value. A missing either
        side is a FAILED (the effect is not there), not an UNKNOWN: the
        condition claimed the destination would hold the source's contents.
        """

        def verify(mission: Mission) -> Verification:
            target, origin = Path(path).expanduser(), Path(source).expanduser()
            try:
                target_bytes = target.read_bytes()
            except FileNotFoundError:
                return Verification(condition_id, ConditionStatus.FAILED,
                                    f"{target} does not exist after the step")
            except OSError as exc:
                return Verification(condition_id, ConditionStatus.UNKNOWN,
                                    f"{target} could not be read: {exc}")
            try:
                source_bytes = origin.read_bytes()
            except FileNotFoundError:
                return Verification(
                    condition_id, ConditionStatus.UNKNOWN,
                    f"{origin} no longer exists, so the copy cannot be compared")
            except OSError as exc:
                return Verification(condition_id, ConditionStatus.UNKNOWN,
                                    f"{origin} could not be read: {exc}")

            if target_bytes == source_bytes:
                digest = hashlib.sha256(target_bytes).hexdigest()[:16]
                return Verification(condition_id, ConditionStatus.VERIFIED,
                                    f"{target} matches {origin} exactly "
                                    f"(sha256:{digest}, {len(target_bytes)} bytes)")
            return Verification(
                condition_id, ConditionStatus.FAILED,
                f"{target} is {len(target_bytes)} bytes, {origin} is "
                f"{len(source_bytes)}; the contents differ")

        return verify

    @staticmethod
    def file_absent(path: str, condition_id: str) -> Verifier:
        """The path no longer exists."""

        def verify(mission: Mission) -> Verification:
            target = Path(path).expanduser()
            if target.exists():
                return Verification(condition_id, ConditionStatus.FAILED,
                                    f"{target} still exists")
            return Verification(condition_id, ConditionStatus.VERIFIED,
                                f"{target} is gone")

        return verify

    @staticmethod
    def always_unknown(reason: str, condition_id: str) -> Verifier:
        """For a condition this system cannot observe. Never returns verified."""
        return lambda mission: Verification(
            condition_id, ConditionStatus.UNKNOWN, reason)


def _parked_confirmation(detail: str) -> str:
    """The confirmation id recorded in a parked step's detail, if any."""
    import re

    match = re.search(r"confirmation (\S+)", detail or "")
    return match.group(1).rstrip(")").strip() if match else ""


def _verifier_from_spec(spec: str, condition_id: str) -> Optional[Verifier]:
    """Build a verifier from a condition's stored declarative spec.

    The spec is JSON naming one of the observers above and the fields it needs,
    e.g. ``{"kind": "file_contains", "path": "/a", "needle": "x"}``. It is data,
    not code: a mission row can name an observer this build already has, never a
    callable. Anything unparseable or unknown returns None so the condition is
    recorded UNKNOWN rather than silently passing.
    """
    import json

    if not spec:
        return None
    try:
        parsed = json.loads(spec)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None

    kind = str(parsed.get("kind") or "")
    if kind == "file_contains":
        return Verifiers.file_contains(
            str(parsed.get("path") or ""), str(parsed.get("needle") or ""), condition_id)
    if kind == "file_hash":
        return Verifiers.file_hash(
            str(parsed.get("path") or ""), str(parsed.get("sha256") or ""), condition_id)
    if kind == "file_absent":
        return Verifiers.file_absent(str(parsed.get("path") or ""), condition_id)
    if kind == "file_identical":
        return Verifiers.file_identical(
            str(parsed.get("path") or ""), str(parsed.get("source") or ""), condition_id)
    return None


class MissionRunner:
    """Drives one mission through gating, execution and independent verification."""

    def __init__(self, service: MissionService, *, execute_step=None,
                 permission=None, audit=None, leases=None) -> None:
        self.service = service
        #: `execute_step(action, ctx)` -> ActionResult-shaped object. Injected so
        #: a test can drive the runner without a live agent registry, and so the
        #: production caller can hand in the orchestrator's own executor.
        self._execute_step = execute_step
        self._permission = permission
        self._audit = audit
        #: A `LeaseService`, when the caller wants per-step authorization spent
        #: against a scoped grant. It is checked *in addition to* the permission
        #: engine, never instead of it.
        self._leases = leases

    # -- public -------------------------------------------------------------
    async def run(self, mission_id: str, *, verifiers: Optional[Dict[str, Verifier]] = None,
                  actor: str = "mission_runner",
                  approval: Optional[Dict[str, Any]] = None) -> Mission:
        """Plan -> gate -> execute -> verify, one step at a time.

        Stops at the first blocked step or failed execution. It does not guess
        its way past a refusal.
        """
        mission = self.service.get(mission_id)
        if mission is None:
            raise ValueError(f"no mission {mission_id!r}")

        verifiers = dict(verifiers or {})

        if MissionState(mission.state) is MissionState.DRAFT:
            mission = self.service.transition(
                mission_id, MissionState.PLANNED, actor=actor,
                reason="plan reviewed and accepted")

        if MissionState(mission.state) is MissionState.AWAITING_APPROVAL:
            # Resuming from a parked approval is only legal when the caller
            # supplies one. Without it the mission stays parked -- this is the
            # difference between "the person approved" and "the runner decided".
            if not approval or not str(approval.get("confirmation_id") or "").strip():
                return mission

        if MissionState(mission.state) in (MissionState.PLANNED,
                                           MissionState.AWAITING_APPROVAL):
            mission = self.service.transition(
                mission_id, MissionState.RUNNING, actor=actor,
                reason=("approval supplied; continuing"
                        if approval else
                        "no step requires approval before execution"))

        # A step that already finished is not repeated: resuming a mission must
        # not re-run a side effect that already happened.
        for step in mission.steps:
            if step.status in ("done", "skipped"):
                continue

            gate = self._gate(step, mission)
            if gate is not None and not gate.get("allowed", False):
                reason = str(gate.get("reason") or "refused by policy")

                # A CONFIRM matched by an approval the caller supplied is the
                # person answering. The confirmation id must match the one the
                # gate issued for this step: an approval for something else is
                # not an approval for this.
                # `PermissionEngine.evaluate` mints a fresh confirmation id on
                # every call, so comparing against the one it just returned can
                # never match a stored approval -- a parked run would re-park
                # forever. The id that matters is the one recorded when the run
                # was parked; that is what a person was shown and approved.
                parked_conf = str(getattr(step, "detail", "") or "")
                parked_conf = _parked_confirmation(parked_conf)
                offered = str((approval or {}).get("confirmation_id") or "").strip()

                approved_here = (
                    str(gate.get("decision") or "").upper() == "CONFIRM"
                    and offered
                    and (offered == parked_conf or
                         (not parked_conf and
                          offered == str(gate.get("confirmation_id") or "").strip())))

                if approved_here:
                    self.service.mark_step(
                        mission_id, step.step_id, status="pending",
                        detail=f"approved ({gate.get('confirmation_id')}); running",
                        actor=actor)
                else:
                    # CONFIRM is neither a refusal nor a permission: the engine
                    # is asking a person. Park in AWAITING_APPROVAL with the
                    # confirmation id, so resuming is explicit and checkable. A
                    # mission parked here stays parked across a restart.
                    if str(gate.get("decision") or "").upper() == "CONFIRM":
                        # Record the confirmation id ON THE STEP. It is the only
                        # durable place an approval can be matched against: the
                        # engine mints a new id each call, and the mission's
                        # reason string is rewritten on the next transition.
                        confirmation = str(gate.get("confirmation_id") or "").strip()
                        self.service.mark_step(
                            mission_id, step.step_id, status="pending",
                            detail=f"awaiting approval: {reason}; "
                                   f"confirmation {confirmation or 'unspecified'}",
                            actor=actor)
                        return self.service.transition(
                            mission_id, MissionState.AWAITING_APPROVAL, actor=actor,
                            reason=(f"step {step.step_id} needs approval "
                                    f"({reason}); confirmation "
                                    f"{gate.get('confirmation_id') or 'unspecified'}"))

                    self.service.mark_step(mission_id, step.step_id, status="failed",
                                           detail=f"not executed: {reason}", actor=actor)
                    return self.service.transition(
                        mission_id, MissionState.BLOCKED, actor=actor,
                        reason=f"step {step.step_id} was refused: {reason}")

            # A lease, when the caller supplied one for this step, is spent *immediately
            # before* the step runs and *after* it passed the permission engine.
            # Both checks are required: the engine says the tool is allowed at
            # all, the lease says this mission may use it on this resource now.
            # A revoked, expired or out-of-scope lease stops the step here.
            lease_refusal = self._spend_lease(step, mission, approval)
            if lease_refusal is not None:
                self.service.mark_step(mission_id, step.step_id, status="failed",
                                       detail=f"not executed: {lease_refusal}",
                                       actor=actor)
                return self.service.transition(
                    mission_id, MissionState.BLOCKED, actor=actor,
                    reason=f"step {step.step_id} was refused: {lease_refusal}")

            mission = self.service.mark_step(mission_id, step.step_id,
                                             status="running", actor=actor)
            outcome = await self._run_one(step, mission)
            mission = self.service.mark_step(
                mission_id, step.step_id,
                status="done" if outcome.ok else "failed",
                detail=outcome.detail, actor=actor)

            if not outcome.ok:
                return self.service.transition(
                    mission_id, MissionState.FAILED, actor=actor,
                    reason=f"step {step.step_id} failed: {outcome.detail}")

        # Every step has run. Now the part that decides whether it worked.
        mission = self.service.transition(
            mission_id, MissionState.VERIFYING, actor=actor,
            reason="all steps executed; verifying the acceptance conditions")

        mission = self._verify_all(mission, verifiers, actor=actor)

        verdict = mission.verdict()
        if verdict == "verified":
            return self.service.transition(
                mission_id, MissionState.COMPLETED, actor=actor,
                reason="every acceptance condition was independently verified")
        return self.service.transition(
            mission_id, MissionState.PARTIALLY_COMPLETED, actor=actor,
            reason=f"steps ran, but the outcome is {verdict}: "
                   "see the unverified conditions")

    # -- internals ----------------------------------------------------------
    def _gate(self, step, mission: Mission) -> Optional[Dict[str, Any]]:
        """Ask the real permission engine about one step.

        A mission's scope narrows permission; it never widens it. The engine
        decides, and this method only relays.
        """
        if self._permission is None:
            # No gate supplied is not "allowed". Running ungated would be a
            # capability the rest of the system does not grant.
            return {"allowed": False,
                    "reason": "no permission engine is attached to this runner"}
        return self._permission(step, mission)

    async def run_parallel(self, mission_id: str, *,
                           verifiers: Optional[Dict[str, Verifier]] = None,
                           actor: str = "mission_runner",
                           approval: Optional[Dict[str, Any]] = None,
                           max_concurrent: int = 4) -> Mission:
        """Run eligible steps concurrently, honouring dependencies and resources.

        The difference from `run` is ordering, not authority: every step still
        passes the permission engine and spends its lease, and any refusal
        stops the mission exactly as it does in the sequential path. What can
        happen at the same time is decided by `scheduler.ready_steps`, which
        refuses to run two steps that name the same file.

        Concurrency is bounded by `max_concurrent` so a mission with many
        independent writes cannot open an unbounded number of them.
        """
        import asyncio

        mission = self.service.get(mission_id)
        if mission is None:
            raise ValueError(f"no mission {mission_id!r}")
        verifiers = dict(verifiers or {})

        if MissionState(mission.state) is MissionState.DRAFT:
            mission = self.service.transition(mission_id, MissionState.PLANNED,
                                              actor=actor, reason="plan accepted")
        if MissionState(mission.state) is MissionState.AWAITING_APPROVAL:
            if not approval or not str(approval.get("confirmation_id") or "").strip():
                return mission
        if MissionState(mission.state) in (MissionState.PLANNED,
                                           MissionState.AWAITING_APPROVAL):
            mission = self.service.transition(
                mission_id, MissionState.RUNNING, actor=actor,
                reason="eligible steps may run concurrently")

        from .scheduler import ready_steps

        semaphore = asyncio.Semaphore(max(1, int(max_concurrent)))

        async def run_one(step):
            async with semaphore:
                return await self._run_step_guarded(step, mission, approval, actor)

        # A batch is one wave of eligible steps. Each wave waits for the whole
        # wave to finish before the next is chosen: a step whose dependency
        # completed inside the wave becomes eligible on the next pass, and the
        # resource reservations in `ready_steps` stay valid for the wave.
        while True:
            current = self.service.get(mission_id)
            if current is None or MissionState(current.state).value != "running":
                return current
            batch = ready_steps(current.steps)
            if not batch:
                break
            outcomes = await asyncio.gather(
                *[run_one(step) for step in batch], return_exceptions=False)
            for step, blocked_reason in outcomes:
                if blocked_reason:
                    return self.service.transition(
                        mission_id, MissionState.BLOCKED, actor=actor,
                        reason=f"step {step.step_id} was refused: {blocked_reason}")

        mission = self.service.transition(
            mission_id, MissionState.VERIFYING, actor=actor,
            reason="every eligible step ran; verifying the acceptance conditions")
        mission = self._verify_all(mission, verifiers, actor=actor)
        verdict = mission.verdict()
        if verdict == "verified":
            return self.service.transition(
                mission_id, MissionState.COMPLETED, actor=actor,
                reason="every acceptance condition was independently verified")
        return self.service.transition(
            mission_id, MissionState.PARTIALLY_COMPLETED, actor=actor,
            reason=f"steps ran, but the outcome is {verdict}")

    async def _run_step_guarded(self, step, mission: Mission,
                                approval: Optional[Dict[str, Any]],
                                actor: str):
        """Gate, lease, execute and record one step. Returns (step, refusal)."""
        gate = self._gate(step, mission)
        if gate is not None and not gate.get("allowed", False):
            reason = str(gate.get("reason") or "refused by policy")
            self.service.mark_step(mission.mission_id, step.step_id, status="failed",
                                   detail=f"not executed: {reason}", actor=actor)
            return step, reason

        refusal = self._spend_lease(step, mission, approval)
        if refusal is not None:
            self.service.mark_step(mission.mission_id, step.step_id, status="failed",
                                   detail=f"not executed: {refusal}", actor=actor)
            return step, refusal

        self.service.mark_step(mission.mission_id, step.step_id,
                               status="running", actor=actor)
        outcome = await self._run_one(step, mission)
        self.service.mark_step(mission.mission_id, step.step_id,
                               status="done" if outcome.ok else "failed",
                               detail=outcome.detail, actor=actor)
        return step, "" if outcome.ok else outcome.detail

    def _spend_lease(self, step, mission: Mission,
                     approval: Optional[Dict[str, Any]]) -> Optional[str]:
        """Spend this step's lease, returning a refusal reason or None.

        `approval` names the lease to spend (`{"lease_id": ...}`), so the
        authorization a person granted is the one actually used. A runner with a
        lease service but no lease for the step refuses: an unleased step under a
        lease-enforcing runner has no authorization to point at.
        """
        if self._leases is None:
            return None

        lease_id = str((approval or {}).get("lease_id") or "").strip()
        if not lease_id:
            return ("this mission is running under lease enforcement, and this "
                    "step has no lease to spend")

        resource = str((step.params or {}).get("path") or "")
        try:
            self._leases.consume(
                lease_id,
                capability=step.tool or "",
                resource=resource,
                plan_revision=mission.version,
                session_id=str((approval or {}).get("session_id") or ""),
            )
        except Exception as exc:      # LeaseRefusal and anything it wraps
            return f"the lease does not authorize this step: {exc}"
        return None

    async def _run_one(self, step, mission: Mission) -> StepOutcome:
        if self._execute_step is None:
            return StepOutcome(step.step_id, ran=False, ok=False,
                               detail="no step executor is attached to this runner")
        try:
            result = self._execute_step(step, mission)
            if hasattr(result, "__await__"):
                result = await result
        except Exception as exc:  # a tool raising is a failed step, not a crash
            return StepOutcome(step.step_id, ran=True, ok=False,
                               detail=f"the tool raised: {exc}")

        ok = bool(getattr(result, "ok", False))
        detail = str(getattr(result, "message", "") or "")
        if not detail:
            # `ActionResult` puts the reason in `error` when it fails and in
            # `output`/`message` when it succeeds. Reading only `message` made a
            # real failure report the word "failed" and nothing else -- the
            # person is owed the tool's own reason.
            detail = str(getattr(result, "error", "") or "")
        if not detail:
            output = getattr(result, "output", None)
            if isinstance(output, dict) and output:
                detail = str(output)[:300]
        return StepOutcome(step.step_id, ran=True, ok=ok, detail=detail or
                           ("succeeded" if ok else "failed"))

    def _verify_all(self, mission: Mission, verifiers: Dict[str, Verifier],
                    *, actor: str) -> Mission:
        """Run each condition's independent observer and record what it found.

        A caller-supplied verifier wins. When none is supplied, the condition's
        own declarative spec is used -- a mission created from a recorded
        demonstration carries the check it was recorded with, so verification
        does not depend on the caller remembering to pass it again. A condition
        with neither stays UNKNOWN, which is what it is: nobody looked.
        """
        for condition in mission.conditions:
            verifier = verifiers.get(condition.condition_id)
            if verifier is None:
                verifier = _verifier_from_spec(condition.verifier,
                                               condition.condition_id)
            if verifier is None:
                # No verifier is not a pass. The condition stays UNKNOWN, which
                # is exactly what it is: nobody looked.
                result = Verification(
                    condition.condition_id, ConditionStatus.UNKNOWN,
                    "no independent verifier was supplied for this condition")
            else:
                try:
                    result = verifier(mission)
                except Exception as exc:
                    result = Verification(
                        condition.condition_id, ConditionStatus.UNKNOWN,
                        f"the verifier raised: {exc}")

            evidence_id = self._record_evidence(mission, condition, result)

            # Evidence is what a VERIFIED verdict rests on. If an audit log is
            # attached and the write failed, the observation exists only in
            # memory -- so the condition is downgraded to UNKNOWN rather than
            # recorded as verified with a reference pointing at nothing. The
            # mission then reports partial/unknown instead of claiming proof it
            # cannot produce.
            if self._audit is not None and not evidence_id and \
                    result.status is ConditionStatus.VERIFIED:
                result = Verification(
                    condition.condition_id, ConditionStatus.UNKNOWN,
                    "the observation could not be recorded as evidence "
                    f"({result.observation}), so it cannot be checked later")

            mission = self.service.record_condition(
                mission.mission_id, condition.condition_id,
                status=result.status, observation=result.observation,
                evidence_id=evidence_id, actor=actor)
        return mission

    def _record_evidence(self, mission: Mission, condition, result: Verification) -> str:
        """Write the observation to the hash-chained audit log and return its id.

        Returns "" when no audit log is attached or the write failed; the caller
        treats that as "this claim has no citable evidence".
        """
        if self._audit is None:
            return ""
        try:
            record = self._audit.record(
                intent=f"verify mission {mission.mission_id} condition "
                       f"{condition.condition_id}",
                tool="mission_verifier",
                target=condition.statement,
                risk="read",
                result=result.status.value,
                detail={
                    "mission_id": mission.mission_id,
                    "condition_id": condition.condition_id,
                    "observation": result.observation,
                },
            )
            return str(getattr(record, "log_id", "") or "")
        except Exception as exc:
            log.info("could not record verification evidence",
                     extra_fields={"error": str(exc)[:120]})
            return ""