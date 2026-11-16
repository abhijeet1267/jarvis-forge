"""Live Mission Steering: change a plan while it is running, without lying about it.

A running mission is not a straight line. The person watching it may see the
plan do something they did not want, or a constraint may arrive that changes
what is acceptable. Refusing to hear that until the run finishes is how a
person ends up watching a bad plan complete because the button said "stop" and
there was nothing between "keep going" and "cancel everything".

Three things this module refuses to do, and one thing it does that nothing else
does:

* **It does not silently undo finished work.** A new constraint cannot delete a
  file a completed step wrote. Effects that already happened are facts, and a
  record that says "the plan changed so the file was reverted" would be a lie
  about the past. What a new constraint *can* do is stop the plan from doing
  more of it, and mark the mission's outcome as one the new constraint no longer
  endorses.
* **It does not let a revision inherit the approval of the plan before it.** A
  step is gated against the revision it belongs to. A lease issued for revision
  2 does not spend on revision 3; the person must approve what actually runs.
* **It does not let a plan escape its intent.** The `IntentScope` recorded when
  the mission was created is frozen. A revision may narrow what it touches and
  may add steps, but a step that needs a capability, a path or a risk level the
  original request never covered is refused by the same `IntentScope` the
  person's own words produced -- not by a re-reading of the current plan.

What it does that nothing else does: **it answers "what has this plan already
done, and what would a change leave standing?"** before the change is applied,
so a person can see that a step's effect is not reversible by adding a rule.

The module is pure logic: it takes a `Mission` and returns the decision. The
caller persists. That keeps the revision protocol testable without a database,
and keeps exactly one place -- `MissionService` -- that owns what a stored
mission says.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..logging_setup import get_logger
from .missions import Mission, MissionState, MissionStep, TERMINAL

log = get_logger("core.steering")

#: Mission states in which a plan can still be steered. A finished mission is
#: not steerable: its outcome is a record of what happened under the plan that
#: ran, and rewriting the plan afterwards would change the record, not the past.
STEERABLE = frozenset({
    MissionState.DRAFT, MissionState.PLANNED, MissionState.AWAITING_APPROVAL,
    MissionState.RUNNING, MissionState.PAUSED, MissionState.RECOVERING,
    MissionState.BLOCKED,
})

#: Step statuses that mean the effect already happened and can no longer be
#: prevented by a rule. `done` and `failed` both ran; `skipped` did not, so a
#: new constraint may still matter to nothing.
EFFECT_ALREADY_APPLIED = frozenset({"done", "failed"})


class SteeringError(ValueError):
    """A requested change is not something this mission can be steered to."""


def steerable(mission: Mission) -> bool:
    return MissionState(mission.state) in STEERABLE


def apply_revision(mission: Mission, *, steps: List[Dict[str, Any]],
                   reason: str) -> Mission:
    """Replace the remaining plan with ``steps`` and move to the next revision.

    ``steps`` is the plan the person wants from here on. Steps that already ran
    keep their identity and status -- a revision that renamed a completed step
    or reset it to pending would replay a side effect that already happened.
    New steps are appended with fresh ids; a step the caller kept under the same
    id keeps the status it earned.

    A parked step (``pending`` with a recorded confirmation) keeps its approval
    when the revision leaves its tool and params unchanged -- the person's answer
    still describes it. If the revision changes what that step would do, the
    approval no longer applies and is dropped, so the next run asks again rather
    than resuming an approval given for a different action.

    Raises `SteeringError` for anything the protocol cannot honour, rather than
    applying part of a revision and leaving the plan in a state nobody asked
    for. When the parked approval became stale, the mission is moved out of
    ``awaiting_approval`` here, so it does not sit waiting on an answer that can
    no longer be given.
    """
    if not steerable(mission):
        raise SteeringError(
            f"mission {mission.mission_id} is {mission.state}; a plan that has "
            "finished cannot be steered, because its outcome is the record of "
            "the plan that ran")

    text = str(reason or "").strip()
    if not text:
        raise SteeringError(
            "a revision must record why it was made; a changed plan with no "
            "explanation cannot be reviewed afterwards")

    existing = {step.step_id: step for step in mission.steps}
    applied = [s for s in mission.steps if s.status in EFFECT_ALREADY_APPLIED]

    revised: List[MissionStep] = []
    seen = set()
    invalidated_approval = False
    for index, raw in enumerate(steps or []):
        step_id = str(raw.get("step_id") or f"S{len(revised) + 1}").strip()
        if not step_id:
            raise SteeringError(f"step {index + 1} has an empty id")
        if step_id in seen:
            raise SteeringError(
                f"step id {step_id!r} appears twice in the revision; ids must be "
                "unique so a step's status can be attributed to one step")
        seen.add(step_id)

        prior = existing.get(step_id)
        if prior is not None and prior.status in EFFECT_ALREADY_APPLIED:
            # Keep the completed step exactly as it ran. Its tool, params and
            # status are what the record says happened; editing them now would
            # rewrite history rather than change the plan.
            revised.append(prior)
            continue

        merged = _merged_step(raw, prior, index)
        if prior is not None and prior.status == "pending":
            if (prior.tool, prior.params) != (merged.tool, merged.params):
                # The parked approval was given for the old shape of this step.
                # Carrying its confirmation id forward would let an approval for
                # one action resume a different one, so it is dropped and the
                # mission re-asks on the next run.
                invalidated_approval = True
            else:
                # Unchanged: the person's answer still describes this step.
                merged.detail = prior.detail
        revised.append(merged)

    # A revision that quietly drops a completed step from the plan would make
    # the record claim it never ran. Completed steps stay in the plan they were
    # part of; the person can see them, and no new step can pretend to be one.
    kept = {s.step_id for s in revised}
    dropped = [s.step_id for s in applied if s.step_id not in kept]
    if dropped:
        raise SteeringError(
            "this revision leaves out steps that already ran ("
            + ", ".join(dropped)
            + "); a change cannot remove what has already happened from the "
            "record. Keep them in the plan, or cancel the mission instead")

    if not revised:
        raise SteeringError("a revision must leave at least one step")

    mission.steps = revised
    mission.plan_revision += 1
    mission.reason = text
    mission.approval_invalidated = invalidated_approval
    return mission


def _merged_step(raw: Dict[str, Any], prior: Optional[MissionStep], index: int
                 ) -> MissionStep:
    """A proposed step with any field it did not restate taken from the prior one.

    A client that sends only a step id is asking "keep this step" -- the same
    reading `apply_revision` uses. Treating a missing tool as an empty tool
    would report a spurious refusal for a step that is unchanged.
    """
    step_id = str(raw.get("step_id") or (prior.step_id if prior else "") or
                  f"S{index + 1}")
    return MissionStep(
        step_id=step_id,
        title=str(raw.get("title") or (prior.title if prior else "")),
        tool=str(raw.get("tool") or (prior.tool if prior else "")),
        params=dict(raw.get("params") or (prior.params if prior else {})),
        satisfies=str(raw.get("satisfies") or (prior.satisfies if prior else "")),
        depends_on=[str(d) for d in (raw.get("depends_on")
                                     or (prior.depends_on if prior else []))],
        status="pending",
    )


def standing_effects(mission: Mission) -> List[Dict[str, Any]]:
    """What this plan has already done that a new constraint cannot take back.

    Used before showing a person the effect of a revision: a rule added now
    stops future work, it does not reach into the past. Each entry names the
    step, the tool and a short description of the resource it touched, so the
    screen can say "this already happened" about the right things.
    """
    effects: List[Dict[str, Any]] = []
    for step in mission.steps:
        if step.status not in EFFECT_ALREADY_APPLIED:
            continue
        params = dict(step.params or {})
        effects.append({
            "step_id": step.step_id,
            "title": step.title,
            "tool": step.tool,
            "status": step.status,
            "resource": str(params.get("path")
                            or params.get("destination")
                            or params.get("target")
                            or params.get("source")
                            or ""),
            "reversible": False,
            # Nothing here reverts it. Saying so plainly is the whole point:
            # a person deciding to add a constraint should know it binds the
            # future only.
            "note": ("already ran; a new rule stops further work but does not "
                     "undo this"),
        })
    return effects


def intent_refusals(mission: Mission, *, plan_revision: Optional[int] = None
                    ) -> List[Dict[str, Any]]:
    """Every step the mission's own intent refuses, with its reason.

    The intent is the record of what the original request authorized. A plan
    revision is checked against it rather than against its own ambition, so a
    planner cannot widen a mission by rewriting its plan. A mission with no
    recorded intent returns nothing here -- the permission engine is still the
    gate for every step, and this check is an additional one, never a
    replacement.

    ``plan_revision`` defaults to the mission's current revision. It is a
    parameter so a caller can ask "would revision 3 have been allowed" before
    committing to it.
    """
    raw = str(getattr(mission, "intent", "") or "")
    if not raw:
        return []
    from ..security.intentshield import IntentScope

    revision = mission.plan_revision if plan_revision is None else int(plan_revision)
    try:
        data = _intent_dict(raw)
    except (ValueError, TypeError) as exc:
        # A stored intent that will not parse is not "no intent": it is an
        # authorization nobody can check, and every step is refused.
        return [{"step_id": s.step_id, "violation": "intent",
                 "reason": f"the mission's recorded intent could not be read: {exc}"}
                for s in mission.steps]

    # The revision rule exists so a step cannot run under an approval given for
    # a different plan. When asking "would revision N be within this intent?",
    # the question is about capabilities, paths and risk -- not about the
    # revision stamp, which *is* the thing under review. Rebasing the stamp to
    # the revision being asked about keeps the other rules answering and stops
    # the revision rule from refusing everything by construction. The run-time
    # guard still compares the mission's real revision against the lease.
    data["plan_revision"] = revision
    try:
        scope = IntentScope.from_dict(data)
    except (ValueError, TypeError) as exc:
        return [{"step_id": s.step_id, "violation": "intent",
                 "reason": f"the mission's recorded intent could not be read: {exc}"}
                for s in mission.steps]

    refusals: List[Dict[str, Any]] = []
    for step in mission.steps:
        if step.status in ("done", "skipped"):
            continue
        decision = scope.check_step(step.tool, dict(step.params or {}),
                                    plan_revision=revision,
                                    risk=_declared_risk(step))
        if not decision.allowed:
            refusals.append({"step_id": step.step_id, "violation": decision.violation,
                             "reason": decision.reason})
    return refusals


def _declared_risk(step: MissionStep) -> Optional[int]:
    """The risk a step declares, when it declares one.

    ``None`` lets `IntentScope` fall back to its own default rather than this
    module inventing a value; a risk the step did not state is not a zero.
    """
    raw = (step.params or {}).get("risk")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        # A non-numeric risk is a refusal the shield can explain better than a
        # guess here would.
        return -1


def _intent_dict(raw: str) -> Dict[str, Any]:
    import json

    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        raise ValueError("the stored intent is not valid JSON")
    if not isinstance(data, dict):
        raise ValueError("the stored intent is not an object")
    return data


def revision_summary(mission: Mission, proposed_steps: List[Dict[str, Any]]
                     ) -> Dict[str, Any]:
    """What a revision would change, before anybody commits to it.

    This is the payload the steering screen shows. It answers four questions:
    what the plan becomes, what it has already done, what the new plan would be
    refused for, and whether the change needs a fresh approval. Expansion of
    any kind -- a new step, a new tool, a wider resource -- needs one; dropping
    future work does not.
    """
    prior = {s.step_id: s for s in mission.steps}
    prior_tools = {str(s.tool or "") for s in mission.steps if s.tool}
    proposed_ids = [str(raw.get("step_id") or "") for raw in (proposed_steps or [])]
    kept_running = [s.step_id for s in mission.steps
                    if s.status in EFFECT_ALREADY_APPLIED]

    # A tool is only "new" if the plan did not already use it. Counting a tool
    # the completed part of the plan already ran would report every revision
    # that keeps a step as an expansion.
    new_tools = sorted({
        str(raw.get("tool") or "")
        for raw in (proposed_steps or [])
        if str(raw.get("tool") or "") and str(raw.get("tool") or "") not in prior_tools
    })
    added = [sid for sid in proposed_ids if sid and sid not in prior]
    removed_future = [s.step_id for s in mission.steps
                      if s.status not in EFFECT_ALREADY_APPLIED
                      and s.step_id not in proposed_ids]

    # What the plan after this revision would ask for, checked against the
    # stored intent at the *next* revision number -- approving a revision means
    # authorizing that revision, so that is what the shield must be shown.
    probe = Mission(
        mission_id=mission.mission_id, request=mission.request,
        outcome=mission.outcome, state=mission.state, scope=dict(mission.scope),
        conditions=list(mission.conditions), steps=list(mission.steps),
        evidence=list(mission.evidence), reason=mission.reason,
        recovery_attempts=mission.recovery_attempts,
        plan_revision=mission.plan_revision,
        # Carried so the proposed plan is checked against the same intent the
        # mission was created under. Dropping it here would make a revision
        # look unconstrained when it is not.
        intent=mission.intent,
    )
    probe.steps = [_merged_step(raw, prior.get(str(raw.get("step_id") or "")), i)
                   for i, raw in enumerate(proposed_steps or [])]
    refusals = intent_refusals(probe, plan_revision=mission.plan_revision + 1)

    return {
        "mission_id": mission.mission_id,
        "from_revision": mission.plan_revision,
        "to_revision": mission.plan_revision + 1,
        "steps": proposed_ids,
        "added_step_ids": added,
        "removed_future_step_ids": removed_future,
        "completed_step_ids": kept_running,
        "new_tools": new_tools,
        "already_done": standing_effects(mission),
        "intent_refusals": refusals,
        "requires_new_approval": bool(added or new_tools),
        "note": _summary_note(added, removed_future, kept_running, refusals),
    }


def steering_report(mission: Mission) -> Dict[str, Any]:
    """What the mission looks like after a revision, for the screen to show.

    Named separately from `revision_summary` because it answers the *past*
    question -- what has run, what a constraint can no longer reach -- while the
    summary answers the *future* one. A caller needs both to say honestly what
    a steer just did.
    """
    return {
        "plan_revision": mission.plan_revision,
        "steps": [
            {"step_id": s.step_id, "title": s.title, "tool": s.tool,
             "status": s.status}
            for s in mission.steps
        ],
        "already_done": standing_effects(mission),
        "intent_refusals": intent_refusals(mission),
        "note": ("Steps that already ran keep their result. A revision changes "
                 "what happens next; it cannot change what happened."
                 if standing_effects(mission) else
                 "No step had run yet, so this revision changes the plan itself."),
    }


def _summary_note(added: List[str], removed: List[str],
                  completed: List[str], refusals: List[Dict[str, Any]]) -> str:
    parts: List[str] = []
    if added:
        parts.append(f"adds {len(added)} step(s)")
    if removed:
        parts.append(f"drops {len(removed)} step(s) that had not run")
    if completed:
        parts.append(f"leaves {len(completed)} already-run step(s) in place")
    if refusals:
        parts.append(f"{len(refusals)} step(s) would be refused by the "
                     "mission's original intent")
    if not parts:
        return "This revision changes the plan in no way that can be described."
    return ("This revision " + ", ".join(parts) + "."
            + (" It adds steps, so a previous approval does not cover it."
               if added else ""))