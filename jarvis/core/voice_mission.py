"""Run a voice command as a real mission, so its outcome is actually verified.

Why this exists
---------------
``/command`` executes its actions inline (``JarvisCore._execute_plan``) and
creates the mission only as a *ledger stub*: the record ends up
``partially_completed / verdict=unknown`` with its step still ``pending``,
because nothing on that path ever calls ``mark_step`` or ``record_condition``.
The action may have worked -- ``open_application`` re-reads the process table --
but the mission cannot say so, and a mission that cannot say so is why "did it
work?" had no evidence-backed answer.

``MissionRunner`` is the component that *does* record steps and conditions and
runs independent verifiers. It was reachable only through
``POST /missions/{id}/run``. This module is the join: a command creates the same
typed mission, then hands it to the same ``MissionRunner``, through the same
gate and the same executor the mission routes already build.

What it deliberately does not do
--------------------------------
* It does not replace ``/command``. The inline path stays exactly as it was and
  is still the default; this is reached only when
  ``JARVIS_COMMAND_MISSION_MODE`` is on.
* It does not invent an executor, a permission gate or a verifier. All three are
  the ones the mission routes construct.
* It does not lower risk. Every step goes through ``_gate_factory``'s real
  ``PermissionEngine``, so a destructive step parks for approval exactly as it
  does when a person starts the mission from Mission Control.
* It does not guess verifiers. Only kinds in ``outcome_assurance.KINDS`` are
  emitted, and only from the tool's *declared* parameters.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

#: Tools whose success is a state a reader can observe afterwards, mapped to the
#: verifier that observes it. A tool absent from this map gets no condition --
#: an unknown condition would be reported UNKNOWN forever, so not emitting one
#: is more honest than emitting a check nobody can satisfy.
_OBSERVABLE = {
    "open_application": "process_running",
    "close_app": "process_running",
}


def _process_name(app_param: str) -> str:
    """The process-table name for an application parameter.

    ``open_application`` takes what a person calls the app ("Calculator",
    "VS Code"). The process name is the bundle's executable: "Calculator",
    "Code". Guessing a full display name would verify the wrong thing, so this
    only trims a trailing ".app" and leaves the rest to the caller's spelling --
    the observer reports "absent" when the name is wrong, which is an honest
    UNKNOWN-ish result rather than a false pass.
    """
    name = str(app_param or "").strip()
    if name.lower().endswith(".app"):
        name = name[:-4]
    return name


def conditions_for(action: Any) -> List[Dict[str, Any]]:
    """The verifiable conditions a planned action should carry, if any.

    Returns an empty list when the action's tool has no observer this build
    trusts. The mission then records its step but no condition, and the verdict
    stays ``unknown`` rather than claiming a verification that never happened.
    """
    tool = str(getattr(action, "tool", "") or "")
    kind = _OBSERVABLE.get(tool)
    if kind is None:
        return []

    params = dict(getattr(action, "params", None) or {})
    app = _process_name(str(params.get("app") or ""))

    # A close that names no application cannot be observed: the verifier would
    # check some other process. Emit nothing rather than a check against "".
    if not app:
        return []

    if tool == "close_app":
        # Closing is verified by the process being *gone*, and a quit is
        # asynchronous -- the app may still be exiting when this check runs. A
        # bounded grace window lets it disappear; it only ever ends on the
        # process actually being gone, so it cannot turn absence into a pass.
        return [{"condition_id": "S1", "statement": f"{app} is no longer running",
                 "verifier": {"kind": "process_running", "process": app,
                              "absent": True, "grace": 3.0}}]

    return [{"condition_id": "S1", "statement": f"{app} is running",
             "verifier": {"kind": "process_running", "process": app}}]


def steps_for(actions: List[Any]) -> List[Dict[str, Any]]:
    """Mission steps from the planner's typed actions, one step per action."""
    from .missions import MissionStep

    out: List[Dict[str, Any]] = []
    for index, action in enumerate(actions):
        out.append({
            "step_id": f"S{index + 1}",
            "title": getattr(action, "reason", "") or f"{action.tool}",
            "tool": str(action.tool or ""),
            "params": dict(action.params or {}),
            "depends_on": [f"S{index}"] if index else [],
            "idempotency_key": "",
            "_satisfies": f"S{index + 1}",
        })
    return out


def attach_conditions(steps: List[Dict[str, Any]],
                      conditions: List[Dict[str, Any]]) -> None:
    """Point each step at the condition that verifies it, when one exists.

    ``MissionStep.satisfies`` is the join between "what I did" and "what must be
    true"; MissionRunner uses it to find the verifier for a step. Without it a
    condition would be recorded but never checked against the step that was
    meant to bring it about.
    """
    by_id = {c["condition_id"]: c for c in conditions}
    for step in steps:
        target = step.pop("_satisfies", "")
        if target in by_id:
            step["satisfies"] = target
            step["title"] = by_id[target]["statement"]


def safe_slug(text: str, limit: int = 60) -> str:
    """A short readable mission request string, bounded."""
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
    return cleaned[:limit] if cleaned else "voice command"


async def run_as_mission(core: Any, request: Any, plan: Any) -> Optional[Any]:
    """Execute *plan*'s actions through MissionRunner and return the mission.

    Returns ``None`` when there is nothing to run (no actions) or when the
    mission could not be created, so the caller can fall back to the inline path
    rather than reporting a success that did not happen.

    The runner is given the *same* executor and gate the mission routes build, so
    a step that would be refused from Mission Control is refused here too.
    """
    actions = list(getattr(plan, "actions", None) or [])
    if not actions:
        return None

    from ..api.missions_routes import (_executor_factory, _gate_factory,
                                       _audit, _verifiers_from)
    from .mission_runner import MissionRunner

    conditions = conditions_for(actions[0]) if len(actions) == 1 else []
    steps = steps_for(actions)
    attach_conditions(steps, conditions)

    # The mission stores a condition's verifier as *text*; `_verifier_from_spec`
    # reads JSON back out of it. Serialising here is what lets the mission be
    # re-verified later without the caller re-sending the spec.
    stored_conditions = [
        {**c, "verifier": json.dumps(c["verifier"], sort_keys=True)}
        if isinstance(c.get("verifier"), dict) else dict(c)
        for c in conditions
    ]

    try:
        mission = core.missions.create(
            safe_slug(getattr(request, "text", "")),
            scope={},
            conditions=stored_conditions,
            steps=steps,
        )
    except Exception:
        return None

    verifiers = _verifiers_from(
        {c["condition_id"]: c["verifier"] for c in conditions
         if isinstance(c.get("verifier"), dict)})

    runner = MissionRunner(
        core.missions,
        execute_step=_executor_factory(core),
        permission=_gate_factory(core),
        audit=_audit(),
    )
    try:
        return await runner.run(mission.mission_id, verifiers=verifiers,
                                actor="voice_operator")
    except Exception:
        # A runner failure must not take the turn down: the mission record is
        # still the truth of what happened, and the caller reports from it.
        return core.missions.get(mission.mission_id)