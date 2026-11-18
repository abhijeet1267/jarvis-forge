"""Explain a failed mission from its recorded step data, without inventing causes.

The diagnosis is a *reading of the record*, not a story about it. Every claim
below comes from a field the mission or a step actually carries:

* the mission row's ``request`` / ``outcome`` / ``state`` / ``reason``, and its
  ``conditions`` (the success conditions a verifier records against);
* a step's ``step_id``, ``title``, ``tool``, ``status``, ``detail``,
  ``satisfies`` and -- when a caller supplies one -- ``verification``.

The step dictionaries here have the shape ``MissionStep.to_dict`` writes
(``core.missions``), which is also the shape stored in the ``missions.steps``
JSON column: ``step_id, title, tool, params, status, detail, satisfies,
idempotency_key, depends_on``. There is deliberately no ``error`` or
``reason`` key in that shape; the runner records a failure's text in
``detail``, so that is read as the error. ``error`` and ``reason`` are still
accepted first when a caller has enriched the record, because reading a field
that is there is not guessing.

When a field is absent the diagnosis says so in ``observations`` and leaves the
value empty. It never fills a gap with an assumption. In particular, a failed
step with no verification recorded leaves ``outcome_unknown`` True: the step
may or may not have changed the world, and this module is not entitled to
choose.

``safe_next_steps`` only names non-destructive moves -- inspect, re-read, ask
the user, request a missing permission, retry a harmless read. It never
suggests re-running a step automatically, because a failed step's effect is
exactly the thing that is not known.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..logging_setup import get_logger

log = get_logger("core.mission_diagnosis")

_DONE = "done"
_FAILED = "failed"
_SKIPPED = "skipped"

#: Where a failed step's text can live, in reading order. ``detail`` is what
#: ``MissionStep.to_dict`` actually stores; the other two are accepted when a
#: caller has added them, never synthesised.
_ERROR_KEYS = ("error", "reason", "detail")

#: Verification statuses that settle the outcome one way or the other.
_KNOWN_VERIFICATION = frozenset({"verified", "failed"})

#: Failure text that names a refusal rather than a tool error. Used only to
#: choose a safe next step, never to assert a cause.
_REFUSAL_MARKERS = ("permission", "denied", "not allowed", "approval",
                    "refused", "refusal", "policy", "lease", "intent", "blocked")


@dataclass
class Diagnosis:
    """What the recorded mission and step data support saying about a failure."""

    intended: str
    last_completed_step: Optional[str]
    failed_step: Optional[str]
    error: str
    observations: List[str] = field(default_factory=list)
    outcome_unknown: bool = False
    safe_next_steps: List[str] = field(default_factory=list)


def _text(value: Any) -> str:
    """A scalar recorded in the mission, as text. Absent means empty, not zero."""
    if value is None:
        return ""
    return str(value).strip()


def _dicts(value: Any) -> List[Dict[str, Any]]:
    """The mapping entries of a recorded list, ignoring anything unreadable."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _step_name(step: Dict[str, Any], index: int) -> str:
    """A stable name for one step from its own record."""
    return (_text(step.get("step_id"))
            or _text(step.get("title"))
            or f"step {index + 1}")


def _condition_for(step: Dict[str, Any],
                   conditions: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The success condition this step claims to satisfy, when one is recorded."""
    satisfies = _text(step.get("satisfies"))
    if not satisfies:
        return None
    for condition in conditions:
        if _text(condition.get("condition_id")) == satisfies:
            return condition
    return None


def _verification(step: Dict[str, Any],
                  conditions: List[Dict[str, Any]]) -> tuple[str, str, bool]:
    """``(status, observation, recorded)`` for one step, from what is present.

    Three record shapes are read, in order:

    1. a ``verification`` entry on the step itself -- a mapping, a status
       string, or an object with ``status``/``observation`` attributes;
    2. the success condition the step's ``satisfies`` names, which is the join
       the mission contract actually stores;
    3. nothing -- reported as not recorded rather than assumed either way.
    """
    if step.get("verification") is not None:
        raw = step["verification"]
        if isinstance(raw, dict):
            return (_text(raw.get("status")),
                    _text(raw.get("observation")) or _text(raw.get("detail")),
                    True)
        if isinstance(raw, str):
            return _text(raw), "", True
        if hasattr(raw, "status"):
            return (_text(getattr(raw, "status", "")),
                    _text(getattr(raw, "observation", "")),
                    True)

    condition = _condition_for(step, conditions)
    if condition is not None:
        return (_text(condition.get("status")),
                _text(condition.get("observation")),
                True)
    return "", "", False


def _selected_error(step: Dict[str, Any]) -> str:
    """The failure text the step itself recorded, or "".

    The runner writes a refused step's reason and a failed tool's reason into
    ``detail``; that is the real field. A caller-supplied ``error`` or
    ``reason`` is preferred only because it is more specific, never invented.
    """
    for key in _ERROR_KEYS:
        text = _text(step.get(key))
        if text:
            return text
    return ""


def _looks_like_refusal(error: str) -> bool:
    """Whether the recorded failure text names a refusal rather than a fault."""
    haystack = error.lower()
    return any(marker in haystack for marker in _REFUSAL_MARKERS)


def diagnose(mission: dict, steps: list[dict]) -> Diagnosis:
    """Read a diagnosis out of the recorded mission and its steps.

    Nothing here runs, retries or observes the world. It reports what the
    record says, marks fields the record does not answer, and offers only
    non-destructive next moves.
    """
    mission_data: Dict[str, Any] = mission if isinstance(mission, dict) else {}
    raw_steps: List[Any] = steps if isinstance(steps, list) else []
    if not raw_steps:
        # A caller may pass the mission row alone; its ``steps`` column is the
        # same record, so read that rather than reporting an empty mission.
        nested = mission_data.get("steps")
        if isinstance(nested, list) and nested:
            raw_steps = nested
    step_records = [step for step in raw_steps if isinstance(step, dict)]

    observations: List[str] = []

    intended = _text(mission_data.get("outcome")) or _text(mission_data.get("request"))
    if not intended:
        observations.append("the mission's intended outcome was not recorded.")

    state = _text(mission_data.get("state"))
    reason = _text(mission_data.get("reason"))
    if state and reason:
        observations.append(
            f"the mission is recorded in state {state!r}, with the reason: {reason}")
    elif state:
        observations.append(
            f"the mission is recorded in state {state!r}; no reason for that "
            "state was recorded.")
    else:
        observations.append("the mission's state was not recorded.")

    if raw_steps and len(step_records) != len(raw_steps):
        observations.append(
            f"{len(raw_steps) - len(step_records)} step record(s) were not "
            "readable mappings and were ignored.")

    if step_records:
        statuses = ", ".join(
            f"{_step_name(step, index)}={_text(step.get('status')) or 'unrecorded'}"
            for index, step in enumerate(step_records))
        observations.append(f"step statuses as recorded: {statuses}.")
    else:
        observations.append("no steps were recorded for this mission.")

    conditions = _dicts(mission_data.get("conditions"))
    if conditions:
        counts: Dict[str, int] = {}
        for condition in conditions:
            status = _text(condition.get("status")) or "unrecorded"
            counts[status] = counts.get(status, 0) + 1
        summary = ", ".join(f"{key}={counts[key]}" for key in sorted(counts))
        observations.append(f"acceptance conditions as recorded: {summary}.")

    failed_index = next(
        (index for index, step in enumerate(step_records)
         if _text(step.get("status")) == _FAILED),
        None,
    )

    failed_step: Optional[str] = None
    error = ""
    outcome_unknown: bool

    if failed_index is not None:
        failed = step_records[failed_index]
        failed_step = _step_name(failed, failed_index)
        tool = _text(failed.get("tool"))
        tool_note = f" (tool {tool!r})" if tool else ""
        observations.append(
            f"the first failed step in the record is {failed_step}{tool_note}.")

        error = _selected_error(failed)
        if not error:
            observations.append(
                f"{failed_step} recorded no error text, so why it failed was "
                "not recorded.")
        else:
            observations.append(
                f"{failed_step} recorded this failure text: {error}")
            if error.lower().startswith("not executed:"):
                observations.append(
                    f"{failed_step} is recorded as not executed, so the "
                    "refusal came before the tool ran.")

        failed_names = [
            _step_name(step, index)
            for index, step in enumerate(step_records)
            if _text(step.get("status")) == _FAILED
        ]
        if len(failed_names) > 1:
            observations.append(
                f"{len(failed_names)} steps are marked failed; this diagnosis "
                f"describes the first, {failed_step}.")

        verification_status, verification_observation, recorded = _verification(
            failed, conditions)
        if not recorded:
            observations.append(
                f"no verification was recorded for {failed_step}, so whether "
                "it changed anything could not be confirmed.")
            outcome_unknown = True
        elif verification_status in _KNOWN_VERIFICATION:
            detail = (f" and observed: {verification_observation}"
                      if verification_observation else "")
            observations.append(
                f"the verification recorded for {failed_step} has status "
                f"{verification_status!r}{detail}.")
            outcome_unknown = False
        else:
            status_text = verification_status or "unrecorded"
            observations.append(
                f"the verification recorded for {failed_step} has status "
                f"{status_text!r}, so the outcome could not be confirmed.")
            outcome_unknown = True
    else:
        observations.append("no step is marked failed.")
        skipped = sum(1 for step in step_records
                      if _text(step.get("status")) == _SKIPPED)
        if skipped:
            observations.append(f"{skipped} step(s) were recorded as skipped.")
        unfinished = [
            index for index, step in enumerate(step_records)
            if _text(step.get("status")) not in (_DONE, _SKIPPED)
        ]
        if step_records and not unfinished:
            observations.append(
                "every recorded step is marked done or skipped; that is the "
                "step record, and it is not by itself an independent "
                "verification of the intended outcome.")
        elif step_records:
            observations.append(
                f"{len(unfinished)} recorded step(s) did not finish, so the "
                "mission's outcome was not recorded as complete.")
        outcome_unknown = not step_records or bool(unfinished)

    done_indices = [
        index for index, step in enumerate(step_records)
        if _text(step.get("status")) == _DONE
    ]
    if failed_index is not None:
        completed_before = [index for index in done_indices if index < failed_index]
    else:
        completed_before = done_indices
    last_completed_step: Optional[str] = None
    if completed_before:
        index = completed_before[-1]
        last_completed_step = _step_name(step_records[index], index)
    elif failed_index is not None:
        observations.append("no completed step was recorded before the failure.")
    else:
        observations.append("no completed step was recorded.")

    safe_next_steps: List[str] = []
    if failed_index is not None:
        inspect = "Inspect the target's current state before anything else."
        if outcome_unknown:
            inspect += (" The failing step's effect was not confirmed, so it "
                        "may or may not have happened.")
        safe_next_steps.append(inspect)
        safe_next_steps.append(
            "Re-read the file or resource the failing step named, using a "
            "read-only tool.")
        if _looks_like_refusal(error):
            safe_next_steps.append(
                "Ask the user for the missing permission or approval; the "
                "recorded failure text names a refusal.")
        safe_next_steps.append(
            "Ask the user to choose the target before any further action.")
        safe_next_steps.append(
            "Retry a harmless read only if the user asks; do not automatically "
            "re-run the failing step.")
    elif step_records:
        safe_next_steps.append(
            "Check the intended outcome with an independent read before "
            "reporting the mission as done.")
        safe_next_steps.append(
            "Ask the user to choose the next target if any step's effect looks "
            "uncertain.")
    else:
        safe_next_steps.append(
            "Ask the user to restate the intended outcome; no steps were "
            "recorded.")

    log.debug("mission diagnosed", extra_fields={
        "mission_id": _text(mission_data.get("mission_id")),
        "failed_step": failed_step or "",
        "outcome_unknown": outcome_unknown,
    })

    return Diagnosis(
        intended=intended,
        last_completed_step=last_completed_step,
        failed_step=failed_step,
        error=error,
        observations=observations,
        outcome_unknown=outcome_unknown,
        safe_next_steps=safe_next_steps,
    )


def render(diag: Diagnosis) -> str:
    """A short human-readable block, with every world-claim tied to the record."""
    lines: List[str] = []
    lines.append(f"Intended: {diag.intended}." if diag.intended
                 else "Intended: not recorded.")

    if diag.last_completed_step:
        lines.append(
            f"Last completed step, as recorded: {diag.last_completed_step}.")
    else:
        lines.append("Last completed step: none was recorded.")

    if diag.failed_step:
        if diag.error:
            lines.append(
                f"Failed step, as recorded: {diag.failed_step} -- {diag.error}")
        else:
            lines.append(
                f"Failed step, as recorded: {diag.failed_step} (no error text "
                "was recorded).")
    else:
        lines.append("Failed step: none was recorded.")

    if diag.outcome_unknown:
        lines.append(
            "The result could not be confirmed: no verification of the failed "
            "step was recorded, or it was recorded as unknown.")
    elif diag.failed_step:
        lines.append(
            "The failed step's verification was recorded, so the outcome of "
            "that step is known as recorded; it does not make the mission's "
            "intended outcome true.")
    else:
        lines.append(
            "Every recorded step is marked done; that is the step record, and "
            "it does not by itself show that the intended outcome holds.")

    if diag.observations:
        lines.append("Observations:")
        lines.extend(f"- {observation}" for observation in diag.observations)

    if diag.safe_next_steps:
        lines.append("Safe next steps:")
        lines.extend(f"- {step}" for step in diag.safe_next_steps)

    return "\n".join(lines)