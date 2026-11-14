"""ParallelWorld routes: preview and apply proposed local file changes.

Kept separate from the mission router because previewing a file change is not a
mission -- it is a general capability the UI offers before a mission exists. The
surface is small on purpose: a preview that never writes, and an apply that
demands explicit approval and refuses a stale source.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, HTTPException


def build_router(require_auth) -> APIRouter:
    """The file-preview router. Auth is passed in to avoid an import cycle."""
    router = APIRouter(prefix="/files", tags=["files"],
                       dependencies=[Depends(require_auth)])

    @router.post("/preview")
    async def preview_file_changes(body: Dict[str, Any] = Body(None)) -> Dict[str, Any]:
        """Show proposed local file changes without applying them.

        Pure read: the targets are not touched, so a client can preview a plan it
        never applies. Conflicts (a create over an existing file, a replace under
        a missing one, a protected path) make ``applicable`` false and say why.
        """
        return _preview(body)

    @router.post("/apply")
    async def apply_file_changes(body: Dict[str, Any] = Body(None)) -> Dict[str, Any]:
        """Apply a previewed plan, but only with explicit approval.

        The source is re-hashed immediately before each write, so a file edited
        after the preview is refused rather than overwritten. `approved` must be
        literally true; nothing else is inferred as consent.
        """
        body = body or {}
        if body.get("approved") is not True:
            raise HTTPException(status_code=409,
                                detail="applying file changes requires approved=true")
        return _apply(body)

    @router.post("/undo")
    async def undo_file_changes(body: Dict[str, Any] = Body(None)) -> Dict[str, Any]:
        """Restore what an apply wrote, refusing where newer work exists.

        The caller supplies the `journal` that `apply` returned. Undo does not
        keep hidden state: what would be restored is inspectable before it runs.
        A file edited after the apply is refused, not overwritten.
        """
        from ..core.parallelworld import ParallelWorld
        from ..tools.files import _guard as file_guard

        body = body or {}
        journal = body.get("journal")
        if not isinstance(journal, dict):
            raise HTTPException(status_code=422,
                                detail="the apply journal is required")
        if body.get("approved") is not True:
            raise HTTPException(status_code=409,
                                detail="undo requires approved=true")
        return ParallelWorld(guard=file_guard).undo(journal, approved=True)

    return router


def _world():
    from ..core.parallelworld import ParallelWorld
    from ..tools.files import _guard as file_guard

    return ParallelWorld(guard=file_guard)


def _operations(body: Dict[str, Any]):
    operations = (body or {}).get("operations")
    if not isinstance(operations, list) or not operations:
        raise HTTPException(status_code=422, detail="an operations list is required")
    return operations


def _preview(body: Dict[str, Any]) -> Dict[str, Any]:
    operations = _operations(body)
    world = _world()
    try:
        plan = world.plan(operations)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return world.preview(plan)


def _apply(body: Dict[str, Any]) -> Dict[str, Any]:
    operations = _operations(body)
    world = _world()
    try:
        plan = world.plan(operations)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return world.apply(plan, approved=True)