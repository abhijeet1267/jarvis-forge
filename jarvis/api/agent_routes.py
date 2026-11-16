"""The Agent Control Tower's HTTP surface.

Reads and controls only: it reports what the workers are doing and sets the
gate they consult. It never dispatches a tool itself -- execution stays on the
mission and chat paths, so the tower cannot become a second way to run things
that skips the permission engine.

The read endpoint is ``GET /control-tower`` rather than ``GET /agents`` because
``app.py`` already serves ``/agents`` (the capability listing) and a duplicate
route would silently shadow it. The controls hang off ``/agents/{name}/...``
because that namespace is the same subject -- one agent -- even though the
listing lives elsewhere.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, HTTPException


def build_router(require_auth) -> APIRouter:
    router = APIRouter(tags=["agents"],
                       dependencies=[Depends(require_auth)])

    def _tower():
        from ..core.control_tower import ControlTower

        return ControlTower.get()

    def _ensure_registered() -> None:
        """Populate the tower from the live agent set if it has not been yet.

        The orchestrator registers on construction, but a caller reaching this
        router before any chat has run would otherwise see an empty tower and
        read it as "no agents" rather than "not registered yet".
        """
        from .app import get_core

        tower = _tower()
        if tower.workers():
            return
        try:
            core = get_core()
        except Exception:
            return
        tower.register_from_registry(core.tools)

    @router.get("/control-tower")
    async def list_workers() -> Dict[str, Any]:
        """Every worker, its state, its counters, and the boundary note."""
        _ensure_registered()
        return _tower().snapshot()

    @router.post("/agents/{name}/pause")
    async def pause_worker(name: str,
                           body: Dict[str, Any] = Body(None)) -> Dict[str, Any]:
        """Stop the worker accepting new calls until it is resumed."""
        from ..core.control_tower import WorkerRefusal

        _ensure_registered()
        try:
            worker = _tower().pause(
                name, by="user",
                reason=str((body or {}).get("reason") or ""))
        except WorkerRefusal as exc:
            raise HTTPException(status_code=409, detail=exc.reason)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc).strip("'"))
        return worker.to_dict()

    @router.post("/agents/{name}/resume")
    async def resume_worker(name: str) -> Dict[str, Any]:
        from ..core.control_tower import WorkerRefusal

        _ensure_registered()
        try:
            worker = _tower().resume(name)
        except WorkerRefusal as exc:
            raise HTTPException(status_code=409, detail=exc.reason)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc).strip("'"))
        return worker.to_dict()

    @router.post("/agents/{name}/cancel")
    async def cancel_worker(name: str,
                            body: Dict[str, Any] = Body(None)) -> Dict[str, Any]:
        """Stop the worker for the rest of this process.

        Deliberately not the same as pause: a cancelled worker does not come
        back on its own, so a person is not surprised by an agent returning.
        """
        from ..core.control_tower import WorkerRefusal

        _ensure_registered()
        try:
            worker = _tower().cancel(
                name, by="user",
                reason=str((body or {}).get("reason") or ""))
        except WorkerRefusal as exc:
            raise HTTPException(status_code=409, detail=exc.reason)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc).strip("'"))
        return worker.to_dict()

    @router.post("/agents/{name}/budget")
    async def set_budget(name: str,
                         body: Dict[str, Any] = Body(None)) -> Dict[str, Any]:
        """Set how many more calls the worker may make. ``null`` lifts the bound."""
        _ensure_registered()
        body = body or {}
        raw = body.get("budget")
        budget = None if raw is None else int(raw)
        try:
            worker = _tower().set_budget(name, budget)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc).strip("'"))
        return worker.to_dict()

    return router