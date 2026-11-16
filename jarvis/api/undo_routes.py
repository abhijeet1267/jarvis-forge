"""Routes for the Temporal Undo Map.

Read the timeline and its summary, and undo exactly one reversible entry. There
is no bulk undo and no "undo everything": each reversal reads the world and can
refuse, so a batch would report success for a list whose members mostly refused.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, HTTPException


def build_router(require_auth) -> APIRouter:
    router = APIRouter(prefix="/undo", tags=["missions"],
                       dependencies=[Depends(require_auth)])

    @router.get("")
    async def timeline(limit: int = 60) -> Dict[str, Any]:
        """Every recorded effect, newest first, with whether it can be undone."""
        from ..core.undo_map import get_undo_map

        undo_map = get_undo_map()
        body = undo_map.summary()
        body["entries"] = undo_map.timeline(limit=limit)
        return body

    @router.post("/{entry_id}")
    async def undo_one(entry_id: str) -> Dict[str, Any]:
        """Reverse one reversible effect, or refuse with the reason it cannot."""
        from ..core.undo_map import UndoError, get_undo_map

        try:
            return get_undo_map().undo(entry_id)
        except UndoError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    return router