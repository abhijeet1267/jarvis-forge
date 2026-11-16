"""Routes for the Reality HUD and human takeover.

Two reads and two writes, and the writes are the same control the Control Tower
already enforces -- pause and resume -- reached from one screen instead of
seventeen. There is deliberately no route here that touches a pointer, injects
input, or captures a frame: a takeover in this system hands the machine back by
stopping what the agents would do next, not by driving the screen.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, HTTPException


def build_router(require_auth) -> APIRouter:
    router = APIRouter(prefix="/reality", tags=["missions"],
                       dependencies=[Depends(require_auth)])

    @router.get("")
    async def hud() -> Dict[str, Any]:
        """What this Mac actually looks like right now."""
        from ..core.reality_hud import snapshot

        return snapshot(tower=_tower())

    @router.post("/takeover")
    async def takeover(body: Dict[str, Any] = Body(None)) -> Dict[str, Any]:
        """Pause every running worker and say exactly what that did."""
        from ..core.reality_hud import take_over

        return take_over(tower=_tower(), reason=str((body or {}).get("reason") or ""))

    @router.post("/release")
    async def release() -> Dict[str, Any]:
        """Resume the workers this screen paused, and only those."""
        from ..core.reality_hud import release_all

        return release_all(tower=_tower())

    return router


def _tower():
    """The process-wide tower, or a refusal rather than a second instance.

    ``ControlTower.get()`` is a singleton precisely so a pause set through the API
    stops the worker the runner actually calls. Building a fresh one here would
    make the takeover button affect a registry nobody dispatches through -- the
    badge-that-lies failure this screen exists to avoid.
    """
    from ..core.control_tower import ControlTower

    try:
        return ControlTower.get()
    except Exception as exc:  # pragma: no cover - defensive
        raise HTTPException(
            status_code=503,
            detail=f"the control tower is unavailable, so no takeover can be enforced: {exc}")