"""Routes for Adaptive Autonomy Control.

Read the dial, move it, and see what a given level would do to a given tool.
There is no route that bypasses the risk ceiling: the ceiling lives in
``AutonomyControl.apply`` and this surface can only choose a level.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException


def build_router(require_auth) -> APIRouter:
    router = APIRouter(prefix="/autonomy", tags=["privacy"],
                       dependencies=[Depends(require_auth)])

    @router.get("")
    async def posture() -> Dict[str, Any]:
        """The dial's position, the levels, and the ceiling that no level crosses."""
        from ..security.autonomy import describe_autonomy

        return describe_autonomy()

    @router.post("")
    async def set_level(body: Dict[str, Any] = Body(None)) -> Dict[str, Any]:
        """Move the dial. ``scope`` narrows it to a resource prefix."""
        from ..security.autonomy import AutonomyError, get_autonomy

        body = body or {}
        try:
            level = int(body.get("level"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="a level is required")
        try:
            setting = get_autonomy().set(level=level,
                                         scope=str(body.get("scope") or ""),
                                         note=str(body.get("note") or ""))
        except AutonomyError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return setting.to_dict()

    @router.post("/preview")
    async def preview(body: Dict[str, Any] = Body(None)) -> Dict[str, Any]:
        """What would this tool's verdict be at this level, for these params?

        Read-only. It runs the real classifier and the real dial against a tool
        from the registry, so the preview cannot disagree with what a run does --
        the same reason the tournament ranks against the live recovery engine.
        """
        from ..core.models import PolicyDecision
        from ..security.autonomy import AutonomySetting, get_autonomy
        from ..security.risk import RiskClassifier
        from .app import get_core

        body = body or {}
        tool = str(body.get("tool") or "").strip()
        if not tool:
            raise HTTPException(status_code=422, detail="a preview needs a tool")
        core = get_core()
        spec = core.tools.get(tool)
        if spec is None:
            raise HTTPException(status_code=404, detail=f"unknown tool {tool!r}")

        params = body.get("params") or {}
        if not isinstance(params, dict):
            raise HTTPException(status_code=422, detail="'params' must be an object")

        baseline, baseline_reason = RiskClassifier().decide(spec, params)

        control = get_autonomy()
        original = control.setting
        try:
            level = body.get("level")
            if level is None:
                final, why = control.apply(spec, params, baseline, baseline_reason)
            else:
                control._setting = AutonomySetting(  # noqa: SLF001 - a preview, restored below
                    level=int(level), scope=str(body.get("scope") or ""))
                final, why = control.apply(spec, params, baseline, baseline_reason)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        finally:
            control._setting = original

        return {
            "tool": tool,
            "risk": int(spec.risk),
            "risk_label": spec.risk_label(),
            "baseline": {"decision": baseline.value, "reason": baseline_reason},
            "final": {"decision": final.value, "reason": why},
            "stricter": final is not baseline,
            "level": control.setting.level if body.get("level") is None
                     else int(body.get("level")),
        }

    return router