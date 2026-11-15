"""Project Doctor routes: inspect a real project, and propose (never apply) repairs.

Inspection is read-only by default. `run_checks` turns on the project's own
build/test command, because that is a real execution and should be asked for.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, HTTPException


def build_router(require_auth) -> APIRouter:
    router = APIRouter(prefix="/doctor", tags=["doctor"],
                       dependencies=[Depends(require_auth)])

    @router.post("/inspect")
    async def inspect_project(body: Dict[str, Any] = Body(None)) -> Dict[str, Any]:
        """Inspect a project directory and report what was actually observed."""
        from ..core.projectdoctor import inspect

        body = body or {}
        path = str(body.get("path") or "").strip()
        if not path:
            raise HTTPException(status_code=422, detail="a project path is required")
        try:
            return inspect(path, run_checks=body.get("run_checks") is True)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    @router.post("/repair")
    async def propose_project_repair(body: Dict[str, Any] = Body(None)) -> Dict[str, Any]:
        """Propose a repair in ParallelWorld's operation shape.

        Nothing is applied here: the client previews and approves the operations
        through the file route, so a repair is subject to the same checks as any
        other change.
        """
        from ..core.projectdoctor import propose_repair

        body = body or {}
        path = str(body.get("path") or "").strip()
        if not path:
            raise HTTPException(status_code=422, detail="a project path is required")
        return propose_repair(path, str(body.get("finding") or ""))

    return router
