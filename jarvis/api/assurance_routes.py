"""Routes for Outcome Assurance.

List what this build can observe, and run one observation against the world
right now. The run is the same observer the mission runner uses to verify a
condition, so a check here cannot disagree with a check there.
"""

from __future__ import annotations

import json
from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, HTTPException


def build_router(require_auth) -> APIRouter:
    router = APIRouter(prefix="/assurance", tags=["missions"],
                       dependencies=[Depends(require_auth)])

    @router.get("")
    async def kinds() -> Dict[str, Any]:
        """What can be verified, and what each observation is allowed to carry."""
        from ..core.outcome_assurance import supported_kinds

        return {
            "kinds": supported_kinds(),
            "rule": ("An observer reads the world, never the executor's message. A check "
                     "that could not run is UNKNOWN, never a pass."),
        }

    @router.post("/observe")
    async def observe(body: Dict[str, Any] = Body(None)) -> Dict[str, Any]:
        """Run one observation now and return what it saw.

        Read-only with respect to the mission store: it observes the world and
        reports a status. Only the allowlisted command observer runs a process,
        and it refuses anything outside that list.
        """
        from ..core.outcome_assurance import observer_for_spec

        body = body or {}
        spec = body.get("spec")
        if isinstance(spec, dict):
            spec = json.dumps(spec)
        if not spec:
            raise HTTPException(status_code=422, detail="an observation needs a spec")
        observation = observer_for_spec(str(spec))
        if observation is None:
            raise HTTPException(
                status_code=422,
                detail=("this build has no observer for that condition; a check that "
                        "cannot run is UNKNOWN, not a pass"))
        result = observation()
        return {"status": result.status, "detail": result.detail}

    return router