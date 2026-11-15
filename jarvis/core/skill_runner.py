"""Run a stored skill: load it, authorize it, execute it as a mission, verify it.

The skills store deliberately has no execute path. This module is that path, and
it exists here rather than inside the store so the store stays a pure
metadata record and execution goes through exactly one pipeline.

What makes running a skill here different from running a saved script:

* **The skill is a plan, not a capability.** Its steps become a mission, and the
  mission runner gates every step through the PermissionEngine and the lease
  exactly as a hand-written mission would. A skill cannot grant itself anything;
  the manifest's ``required_capabilities`` are compared with what the caller
  actually has before a step runs.
* **Verification is declarative and independent.** A skill names how its result
  is checked (``file_contains``, ``file_hash``, ``file_absent``). Those map to
  the same verifiers the mission runner uses, so ``verified`` means an observer
  read the world -- not that the last step returned OK.
* **A run is recorded with its measured outcome**, including "unknown". A skill
  that has never run reports no success rate at all rather than 100%.

A disabled skill does not run. An untrusted manifest is validated before it is
used, not after.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from ..logging_setup import get_logger
from .mission_runner import MissionRunner, Verifiers
from .missions import ConditionStatus, MissionService

log = get_logger("core.skill_runner")


class SkillRunError(ValueError):
    """A skill could not be run; the message says which gate refused it."""


def _verifier_for(spec: Dict[str, Any], condition_id: str):
    """Map a declarative verification entry to a real verifier.

    An unknown ``kind`` becomes ``always_unknown`` on purpose: a check this
    system does not know how to perform is reported as not-performed, never as
    passed.
    """
    kind = str(spec.get("kind") or "")
    if kind == "file_contains":
        return Verifiers.file_contains(str(spec.get("path") or ""),
                                       str(spec.get("needle") or ""), condition_id)
    if kind == "file_hash":
        return Verifiers.file_hash(str(spec.get("path") or ""),
                                   str(spec.get("sha256") or ""), condition_id)
    if kind == "file_absent":
        return Verifiers.file_absent(str(spec.get("path") or ""), condition_id)
    return Verifiers.always_unknown(
        f"a check of kind {kind!r} is not one this system performs, so the "
        "condition was not checked", condition_id)


def _check_preconditions(manifest, *, settings: Optional[Any] = None) -> List[str]:
    """What must be true before this skill may run.

    Preconditions are declared as strings so a manifest stays data. The two this
    runner can actually evaluate are the platform and the presence of a path; a
    precondition it cannot evaluate is treated as *not satisfied*, because
    assuming an unchecked precondition would be inventing safety.
    """
    problems: List[str] = []
    import sys

    if manifest.supported_platforms:
        current = "macos" if sys.platform == "darwin" else sys.platform
        if current not in manifest.supported_platforms:
            problems.append(
                f"this skill supports {', '.join(manifest.supported_platforms)} "
                f"but this machine is {current}")

    for precondition in manifest.preconditions:
        text = str(precondition or "").strip()
        if not text:
            continue
        lower = text.lower()
        if lower.startswith("path exists:"):
            from pathlib import Path

            target = Path(text.split(":", 1)[1].strip()).expanduser()
            if not target.exists():
                problems.append(f"precondition not met: {target} does not exist")
        elif lower.startswith("command available:"):
            import shutil

            name = text.split(":", 1)[1].strip()
            if shutil.which(name) is None:
                problems.append(f"precondition not met: {name} is not installed")
        else:
            problems.append(
                f"precondition {text!r} cannot be evaluated by this system, so it "
                "is treated as not met")
    return problems


async def run_skill(
    skill_id: str,
    *,
    execute_step=None,
    permission=None,
    store=None,
    mission_service: Optional[MissionService] = None,
    verifiers: Optional[Dict[str, Any]] = None,
    approval: Optional[Dict[str, Any]] = None,
    run_id: str = "",
    available_capabilities: Optional[List[str]] = None,
    allowed_scope: Optional[List[str]] = None,
    max_risk: Optional[int] = None,
) -> Dict[str, Any]:
    """Execute a stored skill and return what actually happened.

    `execute_step` and `permission` are required, and deliberately so: a skill
    runner with no gate would be an automation path around the permission
    engine. The caller passes the same gate and executor the rest of the system
    uses (in production, the orchestrator's own).

    Returns the mission id, verdict, mission payload and the skill's measured
    reliability after this run. A refusal (disabled skill, failed precondition,
    capability not available) raises `SkillRunError` before anything executes.
    """
    from .skills import SkillStore, validate_skill

    if execute_step is None or permission is None:
        raise SkillRunError(
            "a skill cannot run without an executor and a permission gate; "
            "supply both so the run is authorized like any other work")

    started = time.monotonic()
    store = store or SkillStore()

    manifest = store.get_runnable(skill_id)
    if manifest is None:
        raise SkillRunError(
            f"skill {skill_id!r} is not runnable: it is missing, disabled, or has "
            "no stored version")

    problems = _check_preconditions(manifest)
    if problems:
        raise SkillRunError("; ".join(problems))

    # The manifest asks for capabilities and scope; the caller states what it
    # actually has. A skill never widens that.
    validation = validate_skill(
        manifest,
        available_capabilities=set(available_capabilities or []),
        allowed_scope=list(allowed_scope or []),
        max_risk=max_risk)
    if validation:
        raise SkillRunError("; ".join(validation))

    if mission_service is None:
        from ..db.database import get_db

        mission_service = MissionService(get_db())

    conditions = []
    default_verifiers: Dict[str, Any] = {}
    for index, spec in enumerate(manifest.verification or []):
        condition_id = str(spec.get("condition_id") or f"C{index + 1}")
        conditions.append({
            "condition_id": condition_id,
            "statement": str(spec.get("statement")
                             or spec.get("description")
                             or f"{manifest.name or skill_id} result {condition_id}"),
            "verifier": str(spec.get("kind") or "declared"),
        })
        default_verifiers[condition_id] = _verifier_for(spec, condition_id)

    # A skill invocation has an identity so approval can *resume* it.
    #
    # Creating a fresh mission on every call meant an approval for the parked
    # run could never reach it: the run that needed authorizing and the run that
    # executed were different missions. A caller may now name the run; passing
    # the same `run_id` again continues that mission rather than starting a
    # second one. Without an id, a new run is created and its id is returned so
    # the caller has something to resume.
    mission = _existing_run(mission_service, run_id) if run_id else None
    if mission is None:
        mission = mission_service.create(
            f"run skill {manifest.name or skill_id}",
            outcome=f"skill {skill_id} v{manifest.version}",
            scope={"paths": list(manifest.required_scope)},
            conditions=conditions,
            steps=[dict(step) for step in manifest.steps],
        )
        run_id = mission.mission_id

    runner = MissionRunner(mission_service,
                           execute_step=execute_step,
                           permission=permission,
                           leases=_leases_for(approval))
    result = await runner.run(
        mission.mission_id,
        verifiers=verifiers if verifiers is not None else default_verifiers,
        approval=approval,
    )

    verdict = result.verdict()
    ok = result.state == "completed" and verdict == "verified"
    duration = time.monotonic() - started
    reliability = store.record_run(
        skill_id, ok=ok,
        verdict=verdict if verdict else "unknown",
        duration_s=duration,
        note=f"mission {mission.mission_id} ended {result.state}")

    log.info("skill run", extra_fields={
        "skill_id": skill_id, "mission_id": mission.mission_id,
        "state": result.state, "verdict": verdict})

    return {
        "skill_id": skill_id,
        "version": manifest.version,
        # The stable identity a caller resumes with after approving.
        "run_id": run_id or mission.mission_id,
        "mission_id": mission.mission_id,
        "state": result.state,
        "verdict": verdict,
        # The measured counters, so the caller never has to infer a success rate.
        "reliability": reliability,
        "mission": result.to_dict(),
    }


def _existing_run(mission_service, run_id: str):
    """The mission for a named run, or None when it does not exist.

    A resumed run must be the same mission -- same plan, same conditions, same
    approval. Looking it up by id (rather than creating a new one) is what makes
    "approve then resume" mean something.
    """
    try:
        return mission_service.get(str(run_id))
    except Exception:
        return None


def _leases_for(approval: Optional[Dict[str, Any]]):
    """The lease service when the caller supplied a lease to spend."""
    if not approval or not str(approval.get("lease_id") or "").strip():
        return None
    from ..db.database import get_db
    from ..security.leases import LeaseService

    return LeaseService(get_db())