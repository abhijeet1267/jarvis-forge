"""Explain a failed task from its own record -- adapts mission_diagnosis.

This is the surface VO-06 needs: "why didn't that work?" answered from the
mission ledger, not from a model's guess. The tool reads the most recent
non-successful mission, hands its recorded steps to
:func:`jarvis.core.mission_diagnosis.diagnose`, and reports what the record
actually says.

It performs no action and proposes none it can execute itself: the recovery
line is advice, and carrying any of it out still goes through the ordinary
planner and permission gate.
"""

from __future__ import annotations

from typing import Any, Dict, List

from ..core.mission_diagnosis import diagnose, render
from ..core.models import RiskLevel, ToolSpec
from ..core.missions import MissionState
from .base import ToolContext, ok

#: States worth explaining: a finished-but-unsuccessful mission is the thing a
#: person asks "why" about. A verified one has nothing to diagnose.
_EXPLAINABLE = (
    MissionState.FAILED,
    MissionState.CANCELLED,
    MissionState.PARTIALLY_COMPLETED,
    MissionState.BLOCKED,
)


def _mission_row(mission: Any) -> Dict[str, Any]:
    if isinstance(mission, dict):
        return mission
    to_dict = getattr(mission, "to_dict", None)
    if callable(to_dict):
        try:
            return dict(to_dict())
        except Exception:
            return {}
    return {}


def _steps_of(mission: Any, row: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The steps the mission recorded, as plain dicts."""
    steps = row.get("steps")
    if isinstance(steps, list) and steps:
        return [s for s in steps if isinstance(s, dict)]
    raw = getattr(mission, "steps", None)
    if isinstance(raw, list):
        out: List[Dict[str, Any]] = []
        for step in raw:
            as_dict = getattr(step, "to_dict", None)
            if callable(as_dict):
                try:
                    out.append(dict(as_dict()))
                except Exception:
                    continue
            elif isinstance(step, dict):
                out.append(step)
        return out
    return []


async def explain_last_failure(ctx: ToolContext, mission_id: str = "") -> Any:
    """Why did the last task not work? Answered from the mission ledger."""
    core = getattr(ctx, "core", None)
    missions = getattr(core, "missions", None) if core is not None else None
    if missions is None:
        return ok("explain_last_failure",
                  "I do not have access to the mission ledger right now, so I cannot "
                  "tell you what happened. I will not guess.",
                  device=ctx.device_id, found=False, verified=False)

    mission = None
    if mission_id:
        mission = missions.get(mission_id)
        if mission is None:
            return ok("explain_last_failure",
                      f"I have no record of a task called {mission_id!r}.",
                      device=ctx.device_id, found=False, verified=False)
    else:
        recent = missions.list(limit=50, states=list(_EXPLAINABLE))
        mission = recent[0] if recent else None
        if mission is None:
            return ok("explain_last_failure",
                      "I have no recorded task that failed or was cancelled, so there "
                      "is nothing to explain. I am not going to invent a failure.",
                      device=ctx.device_id, found=False, verified=False)

    row = _mission_row(mission)
    steps = _steps_of(mission, row)
    diag = diagnose(row, steps)
    text = render(diag)
    return ok("explain_last_failure", text, device=ctx.device_id, found=True,
              verified=False,
              mission_id=str(row.get("mission_id") or ""),
              state=str(row.get("state") or ""),
              outcome_unknown=bool(diag.outcome_unknown),
              failed_step=diag.failed_step or "",
              safe_next_steps=list(diag.safe_next_steps))


SPECS = [
    (
        ToolSpec(
            name="explain_last_failure",
            description=("Explain why the most recent failed or cancelled task did not "
                         "work, using its recorded steps. Reports when the outcome is "
                         "unknown instead of inventing a cause."),
            risk=RiskLevel.LOW,
            requires_confirmation=False,
            reversible=True,
            parameters={"mission_id": "string"},
            required_parameters=[],
            tags=["computer", "task", "diagnosis"],
        ),
        explain_last_failure,
    ),
]