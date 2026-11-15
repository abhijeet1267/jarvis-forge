"""Saved Workspace routes: a named set of things opened together.

The prefix is ``/saved-workspaces`` rather than ``/workspaces``: that path is
already the *coding* workspace registry (a directory opened as a controlled
worktree), and an earlier draft of this router registered the same paths, which
silently shadowed it. A saved workspace and an open coding workspace are
different things and now have different names.

Editing a workspace is a saved preference, so the routes are plain CRUD. A
launch is a real action -- it starts applications and opens files -- so it is a
separate route and it answers with a per-member result rather than a single
success flag.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, HTTPException


def build_router(require_auth) -> APIRouter:
    router = APIRouter(prefix="/saved-workspaces", tags=["saved-workspaces"],
                       dependencies=[Depends(require_auth)])

    def service():
        from ..core.workspaces import WorkspaceService
        from ..db.database import get_db

        return WorkspaceService(get_db())

    @router.get("")
    async def list_workspaces() -> Dict[str, Any]:
        return {"workspaces": service().list()}

    @router.post("")
    async def create_workspace(body: Dict[str, Any] = Body(None)) -> Dict[str, Any]:
        body = body or {}
        try:
            return service().create(
                str(body.get("name") or ""),
                description=str(body.get("description") or ""),
                project_dir=str(body.get("project_dir") or ""),
                apps=body.get("apps"), files=body.get("files"),
                urls=body.get("urls"))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @router.get("/{workspace_id}")
    async def get_workspace(workspace_id: str) -> Dict[str, Any]:
        workspace = service().get(workspace_id)
        if workspace is None:
            raise HTTPException(status_code=404, detail="no such workspace")
        return workspace

    @router.patch("/{workspace_id}")
    async def update_workspace(workspace_id: str,
                               body: Dict[str, Any] = Body(None)) -> Dict[str, Any]:
        body = body or {}
        allowed = {"name", "description", "project_dir", "apps", "files", "urls"}
        changes = {key: value for key, value in body.items() if key in allowed}
        if not changes:
            raise HTTPException(status_code=422, detail="nothing to change")
        try:
            return service().update(workspace_id, **changes)
        except KeyError:
            raise HTTPException(status_code=404, detail="no such workspace")
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @router.post("/{workspace_id}/duplicate")
    async def duplicate_workspace(workspace_id: str,
                                  body: Dict[str, Any] = Body(None)) -> Dict[str, Any]:
        body = body or {}
        try:
            return service().duplicate(workspace_id, str(body.get("name") or ""))
        except KeyError:
            raise HTTPException(status_code=404, detail="no such workspace")
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @router.delete("/{workspace_id}")
    async def delete_workspace(workspace_id: str) -> Dict[str, Any]:
        if not service().delete(workspace_id):
            raise HTTPException(status_code=404, detail="no such workspace")
        return {"deleted": workspace_id}

    @router.post("/{workspace_id}/launch")
    async def launch_workspace(workspace_id: str) -> Dict[str, Any]:
        """Open every member, reporting what was observed for each.

        A member that fails is in ``results`` with ``ok: false`` and its reason;
        the route itself succeeds because the launch did happen. ``verified`` is
        true only when every attempted member reported success.
        """
        try:
            return service().launch(workspace_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="no such workspace")

    return router