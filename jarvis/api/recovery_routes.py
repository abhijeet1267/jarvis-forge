"""Routes for the Recovery Tournament.

Read-only: it ranks the safe repairs for a failure and explains the ranking. It
never executes a repair -- the recovery engine does that on the real path -- so
this surface cannot become a second way to retry something.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, HTTPException


def build_router(require_auth) -> APIRouter:
    router = APIRouter(prefix="/recovery", tags=["recovery"],
                       dependencies=[Depends(require_auth)])

    @router.post("/tournament")
    async def tournament(body: Dict[str, Any] = Body(None)) -> Dict[str, Any]:
        """Rank the safe repairs for a described failure.

        The caller supplies a failed tool, its error text and (optionally) the
        verification it reached. The reply is every candidate the recovery
        engine would consider, each with its safety verdict, likelihood and
        cost, plus the best automatic one -- or ``null`` when nothing is safe
        and the person has to decide.

        No model is consulted and no repair is run: the ranking comes from the
        tool's own registry spec and the recovery engine's declared policy.
        """
        from ..core.models import ActionResult, Verification
        from ..core.recovery_tournament import RecoveryTournament

        body = body or {}
        tool = str(body.get("tool") or "").strip()
        if not tool:
            raise HTTPException(status_code=422, detail="a tournament needs a tool")

        from .app import get_core

        core = get_core()
        spec = core.tools.get(tool)
        verification = str(body.get("verification") or Verification.UNKNOWN.value)
        try:
            verification = Verification(verification)
        except ValueError:
            verification = Verification.UNKNOWN

        result = ActionResult(
            ok=False, tool=tool,
            error=str(body.get("error") or "") or None,
            message=str(body.get("message") or ""),
            verification=verification,
            verified=verification is Verification.SUCCESS)

        alternatives = body.get("alternatives")
        if alternatives is not None and not isinstance(alternatives, list):
            raise HTTPException(status_code=422,
                                detail="'alternatives' must be a list of tool names")

        tour = RecoveryTournament(engine=core.recovery).evaluate(
            tool=tool, spec=spec, result=result, registry=core.tools,
            alternatives=([str(a) for a in alternatives]
                          if alternatives is not None else None))
        return tour.to_dict()

    return router