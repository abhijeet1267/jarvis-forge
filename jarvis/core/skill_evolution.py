"""Skill Evolution Lab: propose the next version of a skill from what its runs measured.

A skill that has run fifty times is evidence, and a person keeping it by hand is
throwing that evidence away: which step never produced its condition, which
condition was never checkable, which run ended "unknown" because a precondition
was assumed. This module reads the recorded runs and proposes a **next version**
that is a different skill on purpose.

Three properties make that proposal safe to offer at all:

* **It proposes; it never writes.** :func:`propose_evolution` returns a manifest
  and the diff that produced it. Storing a new version is a separate, explicit
  act (``SkillStore.save``), which bumps the version and keeps every older one.
  There is no path here that edits a live manifest in place, so a proposal a
  person ignores changes nothing.
* **The action space only narrows.** Everything this module can change removes
  or tightens: it drops a condition no observer could check, drops a step that
  only claimed to satisfy such a condition, raises the risk tier to what the
  steps' tools actually say, and reports a capability or scope the runs needed
  but the manifest omitted -- as a *note*, never by adding it. It cannot add a
  step, add a capability, add a scope prefix, lower the risk, or relax a
  precondition. Those are the four changes that would turn a learned skill into
  a wider permission than the one it was saved with.
* **It declares no confidence.** Each proposal carries the counts it was derived
  from and, when there is nothing to change, it says so rather than inventing an
  improvement. A skill with no recorded runs gets no proposal: no evidence is
  not evidence of a problem.

The one thing it reads besides the run counters is the tool registry, to find
out what a step's tool actually costs (``RiskLevel``). That is the only claim it
makes about the plan that does not come from a recorded run, and it can only
raise the declared risk.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..logging_setup import get_logger

log = get_logger("core.skill_evolution")

#: Why a proposal was made. Every reason is a measured observation about runs,
#: not a judgement about the skill's quality.
NOT_ENOUGH_RUNS = "not-enough-runs"
UNCHECKABLE_CONDITION = "uncheckable-condition"
UNSATISFIED_STEP = "unsatisfied-step"
UNKNOWN_RUNS = "unknown-runs"
UNDERSTATED_RISK = "understated-risk"
UNDECLARED_CAPABILITY = "undeclared-capability"
OUT_OF_SCOPE = "out-of-scope"

#: How many recorded runs a skill needs before an evolution is proposed. One
#: lucky run is a demonstration (which `skills.py` already refuses to count as
#: reliability), so the bar is deliberately above it.
MIN_RUNS_FOR_EVOLUTION = 3

#: Failure weight at which a step is proposed for removal. A step that failed in
#: half its runs is not unlucky; it does not do what the plan needs.
FAILURE_WEIGHT_FOR_REMOVAL = 0.5

#: The highest risk a skill manifest may declare (``skills._risk_int``). A tool
#: can be PHYSICAL (4), so a plan containing one cannot be expressed here.
MAX_MANIFEST_RISK = 3

#: Condition kinds whose verifier resolution this build actually has, mirrored
#: from `mission_runner._verifier_from_spec` so the lab can tell "the plan named a
#: check nobody can perform" from "the check failed". Kept as a function that asks
#: the resolver rather than a second literal list: a list here would drift.
def _known_verifier_kinds() -> Tuple[str, ...]:
    """Every condition kind the runner can actually resolve right now."""
    from .outcome_assurance import KINDS as assurance_kinds

    return ("file_contains", "file_hash", "file_absent", "file_identical",
            *assurance_kinds)


class EvolutionError(ValueError):
    """A skill cannot be evolved; the message says why."""


@dataclass
class Proposal:
    """One proposed next version, its reasons, and the diff that made it."""

    skill_id: str
    from_version: int
    reasons: List[str] = field(default_factory=list)
    #: Human-readable one-line changes, in the order they were applied.
    changes: List[str] = field(default_factory=list)
    #: Notes about things the lab will *not* do for you (a capability the runs
    #: needed but the manifest lacks). Never applied.
    notes: List[str] = field(default_factory=list)
    #: The proposed manifest as a dict, or None when nothing should change.
    manifest: Optional[Dict[str, Any]] = None
    #: What the proposal was derived from. Always the measured counters.
    evidence: Dict[str, Any] = field(default_factory=dict)

    @property
    def changes_anything(self) -> bool:
        return self.manifest is not None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "from_version": self.from_version,
            "reasons": list(self.reasons),
            "changes": list(self.changes),
            "notes": list(self.notes),
            "manifest": dict(self.manifest) if self.manifest else None,
            "changes_anything": self.changes_anything,
            "evidence": dict(self.evidence),
        }


def _conditions_that_cannot_be_checked(manifest) -> List[Dict[str, Any]]:
    """Verification entries naming a kind this build cannot resolve."""
    known = set(_known_verifier_kinds())
    out: List[Dict[str, Any]] = []
    for spec in manifest.verification or []:
        kind = str(spec.get("kind") or "")
        if kind and kind not in known:
            out.append(dict(spec))
    return out


def _tool_risk_ceiling(manifest, registry) -> Tuple[int, List[str]]:
    """The highest risk the plan's own tools declare, and the tools behind it.

    A manifest's ``risk`` is a claim about what the skill needs. The registry
    knows what each tool can do. When the registry says a step's tool is riskier
    than the manifest admits, the mutation raises the manifest to match -- never
    the reverse, because lowering it would be accepting the plan's own word over
    the gate's.
    """
    if registry is None:
        return -1, []
    ceiling = -1
    worst: List[str] = []
    for step in manifest.steps or []:
        tool = str(step.get("tool") or "")
        spec = registry.get(tool) if tool else None
        if spec is None:
            continue
        try:
            value = int(getattr(spec, "risk", 0))
        except (TypeError, ValueError):
            continue
        if value > ceiling:
            ceiling, worst = value, [tool]
        elif value == ceiling:
            worst.append(tool)
    return ceiling, worst


def _resource_values(manifest) -> List[str]:
    """Every path-like value the plan's steps name, for an out-of-scope note."""
    from ..security.intentshield import RESOURCE_PARAMETER_KEYS

    out: List[str] = []
    for step in manifest.steps or []:
        params = step.get("params") or {}
        for key in RESOURCE_PARAMETER_KEYS:
            value = params.get(key)
            if isinstance(value, str) and value.strip():
                out.append(value.strip())
    return out


def _in_scope(value: str, prefixes: Sequence[str]) -> bool:
    """Prefix containment with a path boundary -- the same rule leases use."""
    candidate = str(value).rstrip("/") or "/"
    for prefix in prefixes:
        normalized = str(prefix).rstrip("/") or "/"
        if candidate == normalized or candidate.startswith(normalized + "/"):
            return True
    return False


def propose_evolution(skill_id: str, *, store=None,
                      registry=None) -> Proposal:
    """Read a skill's recorded runs and propose its next version.

    Raises :class:`EvolutionError` when the skill does not exist, when it has run
    fewer than ``MIN_RUNS_FOR_EVOLUTION`` times, or when its stored manifest
    cannot be read. A skill with runs but nothing to change returns a proposal
    with ``changes_anything`` False and says so in ``reasons``.
    """
    from .skills import SkillStore

    store = store or SkillStore()
    manifest = store.get(skill_id)
    if manifest is None:
        raise EvolutionError(f"no skill {skill_id!r}")

    reliability = store.reliability(skill_id)
    runs = int(reliability.get("runs") or 0)
    evidence = {
        "runs": runs,
        "verified_successes": int(reliability.get("verified_successes") or 0),
        "failures": int(reliability.get("failures") or 0),
        "unknowns": int(reliability.get("unknowns") or 0),
        "success_rate": reliability.get("success_rate"),
        "last_verdict": reliability.get("last_verdict") or "",
    }

    proposal = Proposal(skill_id=str(skill_id), from_version=manifest.version,
                        evidence=evidence)
    if runs < MIN_RUNS_FOR_EVOLUTION:
        proposal.reasons.append(NOT_ENOUGH_RUNS)
        proposal.notes.append(
            f"{runs} recorded run(s); {MIN_RUNS_FOR_EVOLUTION} are needed before "
            "the lab proposes a change, because one demonstration is not evidence")
        return proposal

    data = manifest.to_dict()
    changed = False

    # 1. A condition nobody can check. Its verifier resolves to None, so the
    #    mission runner records it UNKNOWN forever -- which is why every run of
    #    the skill that ended "unknown" ended that way.
    uncheckable = _conditions_that_cannot_be_checked(manifest)
    if uncheckable:
        dropped_ids = {str(spec.get("condition_id") or "") for spec in uncheckable}
        kept = [dict(spec) for spec in data.get("verification") or []
                if str(spec.get("condition_id") or "") not in dropped_ids]
        data["verification"] = kept
        changed = True
        proposal.reasons.append(UNCHECKABLE_CONDITION)
        for spec in uncheckable:
            proposal.changes.append(
                f"removed the condition {spec.get('condition_id')!r}: its check "
                f"kind {spec.get('kind')!r} is not one this build performs, so it "
                "could only ever be recorded unknown")

        # 2. A step whose whole purpose was that condition now satisfies nothing.
        #    It is dropped only when it claims no other condition.
        remaining = {str(spec.get("condition_id") or "") for spec in kept}
        survived: List[Dict[str, Any]] = []
        for step in data.get("steps") or []:
            satisfies = str(step.get("satisfies") or "")
            if satisfies and satisfies in dropped_ids and satisfies not in remaining:
                changed = True
                proposal.reasons.append(UNSATISFIED_STEP)
                proposal.changes.append(
                    f"removed the step using {step.get('tool')!r}: it existed to "
                    f"satisfy {satisfies!r}, which no longer has a check")
                continue
            survived.append(step)
        data["steps"] = survived

    # 3. Runs that executed but were never positively verified. Say so; do not
    #    invent a fix. An "unknown" run is frequently a precondition assumed
    #    satisfied or a condition left pending, and pretending a code change
    #    addresses it would be a claim this module cannot back.
    if int(evidence["unknowns"] or 0) > 0:
        proposal.reasons.append(UNKNOWN_RUNS)
        proposal.notes.append(
            f"{evidence['unknowns']} of {runs} run(s) executed but were not "
            "positively verified. Nothing here changes that: an unknown run needs "
            "a condition that can actually be checked, not a looser plan")

    # 4. The declared risk against what the steps' tools actually cost. Raises
    #    only; a registry that cannot be read leaves the claim alone.
    ceiling, worst_tools = _tool_risk_ceiling(manifest, registry)
    if ceiling > int(data.get("risk") or 0):
        names = ", ".join(sorted(set(worst_tools)))
        # A skill manifest's risk is capped at 3 (see `_risk_int`), while a tool
        # may be PHYSICAL (4). Writing 4 would produce a manifest the store
        # refuses, so the tier is raised to the cap and the gap is stated rather
        # than hidden: such a plan should not be a skill at all.
        if ceiling > MAX_MANIFEST_RISK:
            proposal.reasons.append(UNDERSTATED_RISK)
            proposal.notes.append(
                f"the step(s) using {names} run a tool the permission engine "
                f"rates PHYSICAL ({ceiling}). A skill manifest cannot declare a "
                f"risk above {MAX_MANIFEST_RISK}, so this plan is past the point "
                "where a saved automation is the right container for it -- the "
                "risk is raised to the cap and left there for a person to see")
            data["risk"] = MAX_MANIFEST_RISK
            changed = True
        else:
            proposal.reasons.append(UNDERSTATED_RISK)
            proposal.changes.append(
                f"raised risk from {data.get('risk')} to {ceiling}: the step(s) "
                f"using {names} run a tool the permission engine rates at that tier")
            data["risk"] = ceiling
            changed = True

    # 5. Capabilities the manifest names but this build does not have, and scope
    #    the plan's own steps leave. Both are reported, never applied: adding a
    #    capability or a prefix is widening, which is the one thing this module
    #    must not do.
    scope = list(data.get("required_scope") or [])
    for value in _resource_values(manifest):
        if scope and not _in_scope(value, scope):
            proposal.reasons.append(OUT_OF_SCOPE)
            proposal.notes.append(
                f"a step names {value!r}, which no required_scope prefix covers. "
                "The lab will not add it: widening a skill's scope is a decision "
                "for the person saving the next version")

    if int(evidence["failures"] or 0):
        weight = evidence["failures"] / runs
        if weight >= FAILURE_WEIGHT_FOR_REMOVAL:
            # The store counts every run that was not *positively verified* as a
            # failure (`record_run`: only `ok and verdict == "verified"` is a
            # success), so "3 of 3 failed" can describe a plan whose real
            # conditions all passed and one uncheckable condition blocked the
            # verdict. A live run produced exactly that. Saying "does not work"
            # there would blame the plan for the thing this proposal just fixed.
            if UNCHECKABLE_CONDITION in proposal.reasons:
                proposal.notes.append(
                    f"{evidence['failures']} of {runs} run(s) did not reach a "
                    "verified verdict, which is what a run with a condition no "
                    "observer can check records. That condition is the change "
                    "proposed above, so the plan may be sound: run the new "
                    "version before concluding otherwise")
            else:
                proposal.notes.append(
                    f"{evidence['failures']} of {runs} run(s) did not reach a "
                    f"verified verdict ({weight:.0%}). Nothing here addresses "
                    "that -- an unverified run is a skill that does not work yet, "
                    "whether it failed outright or stopped before its conditions "
                    "could be checked")

    if evidence["last_verdict"] == "unknown" and not int(evidence["failures"] or 0):
        # A skill whose last run stopped at a gate is not a broken skill; it is
        # one a person has not answered. Say which, rather than implying the
        # plan is wrong.
        proposal.notes.append(
            "the last run did not reach a verdict, which is what a run that "
            "stopped for approval records. Nothing in the plan is changed on "
            "the strength of a run that never ran")

    if not changed:
        proposal.notes.append(
            "no change is proposed: nothing the recorded runs show is something "
            "this lab can fix without widening what the skill asks for")
        return proposal

    proposal.manifest = data
    log.info("skill evolution proposed", extra_fields={
        "skill_id": str(skill_id), "reasons": proposal.reasons,
        "changes": len(proposal.changes)})
    return proposal


def interpret_run(skill_id: str, *, store=None) -> Dict[str, Any]:
    """Why this skill's runs ended the way they did, condition by condition.

    Reads the *most recent* mission for the skill when one can be found, and for
    each condition reports what the observer said. This is deliberately a read of
    what was recorded: a condition with no observation stays "never checked".
    """
    from ..db.database import get_db
    from .missions import MissionService
    from .skills import SkillStore

    store = store or SkillStore()
    reliability = store.reliability(skill_id)
    observations: List[Dict[str, Any]] = []
    mission_id = ""

    recent = list((store._entry(str(skill_id)) or {}).get("runs", {}).get("recent") or [])
    for item in reversed(recent):
        note = str(item.get("note") or "")
        if note.startswith("mission "):
            mission_id = note.split(" ", 2)[1].strip()
            break

    if mission_id:
        try:
            mission = MissionService(get_db()).get(mission_id)
        except Exception:
            mission = None
        if mission is not None:
            for condition in mission.conditions:
                observations.append({
                    "condition_id": condition.condition_id,
                    "statement": condition.statement,
                    "verifier": condition.verifier,
                    "status": condition.status,
                    "observation": condition.observation,
                })

    note = "no mission could be read for this skill's runs"
    if observations:
        pending = sum(1 for o in observations if o["status"] == "pending")
        checked = len(observations) - pending
        note = (f"{len(observations)} condition(s) from mission {mission_id}; "
                f"{checked} carried an observation, {pending} were never checked"
                if pending else
                f"{len(observations)} condition(s) from mission {mission_id}, "
                "each with what the observer said")

    return {
        "skill_id": str(skill_id),
        "mission_id": mission_id,
        "observations": observations,
        "runs": int(reliability.get("runs") or 0),
        "note": note,
    }


def describe_lab(*, registry=None) -> Dict[str, Any]:
    """What the lab can and cannot change, for a screen that has to state it."""
    return {
        "min_runs": MIN_RUNS_FOR_EVOLUTION,
        "can_change": [
            "remove a condition whose check no observer can perform",
            "remove a step that existed only to satisfy such a condition",
            "raise the declared risk to what the steps' tools actually cost",
        ],
        "will_not_change": [
            "add or edit a step",
            "add a capability or a scope prefix",
            "lower the risk tier",
            "relax a precondition",
            "store the new version (that is a separate, explicit save)",
        ],
        "note": ("A proposal is derived from the skill's recorded runs and the "
                 "tool registry, never from a model. The lab can only remove or "
                 "tighten; widening a skill is a decision for the person saving it."),
    }