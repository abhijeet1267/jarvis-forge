"""Mac Control routes: what is running, what can launch, and what happened.

Read-only discovery is free; launching an application is a real action and goes
through the same authenticated surface as everything else.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, HTTPException


def build_router(require_auth) -> APIRouter:
    router = APIRouter(prefix="/mac", tags=["mac"],
                       dependencies=[Depends(require_auth)])

    @router.get("/control")
    async def mac_control_status() -> Dict[str, Any]:
        """Supported applications, what is running, and what is not available."""
        from ..core.maccontrol import status

        return status()

    @router.post("/open")
    async def mac_open(body: Dict[str, Any] = Body(None)) -> Dict[str, Any]:
        """Launch an application and report whether it was observed running."""
        from ..core.maccontrol import open_application

        body = body or {}
        app = str(body.get("app") or "").strip()
        if not app:
            raise HTTPException(status_code=422, detail="an application is required")
        result = open_application(app, str(body.get("path") or ""))
        # 409 rather than 500: "that is not an app I know" or "it did not start"
        # is a refusal of the request, not a server fault.
        if not result.get("ok"):
            raise HTTPException(status_code=409, detail=result)
        return result

    return router
