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

        # A caller that supplies a lease gets it enforced against this exact
        # operation: the capability, the resource, the plan revision and the
        # session. `approved: true` alone is a boolean, not an authorization --
        # this is the path by which a scoped grant is actually spent.
        lease_id = str(body.get("lease_id") or "").strip()

        # An operator who has required leases gets a refusal, not a quiet
        # downgrade to boolean approval.
        from ..config import get_settings

        if not lease_id and get_settings().require_lease_for_file_apply:
            raise HTTPException(
                status_code=409,
                detail="this server requires a scoped lease to apply file changes; "
                       "issue one at POST /missions/{id}/leases and pass its id")

        if lease_id:
            from ..db.database import get_db
            from ..security.leases import LeaseRefusal, LeaseService

            operations = body.get("operations") or []
            service = LeaseService(get_db())
            try:
                for operation in operations:
                    service.consume(
                        lease_id,
                        # The lease names a *capability* (`write_file`); the
                        # operation is create/replace/append/delete. Spending
                        # against the operation's own name would never match.
                        capability="write_file",
                        resource=str(operation.get("path") or ""),
                        session_id=str(body.get("session_id") or ""))
            except LeaseRefusal as exc:
                raise HTTPException(
                    status_code=409,
                    detail=f"the lease does not authorize this apply: {exc}")

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

    @router.post("/scan")
    async def scan_folder(body: Dict[str, Any] = Body(None)) -> Dict[str, Any]:
        """Report duplicates and large files under a folder. Read-only.

        Bounded in depth and file count, and it does not follow symlinked
        directories -- a link is not a subtree the person selected.
        """
        from ..core.smartfiles import scan

        body = body or {}
        path = str(body.get("path") or "").strip()
        if not path:
            raise HTTPException(status_code=422, detail="a folder path is required")
        try:
            return scan(path)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    @router.post("/organize-plan")
    async def organize_plan_route(body: Dict[str, Any] = Body(None)) -> Dict[str, Any]:
        """Propose moving loose files into type folders.

        The operations come back in the apply route's shape, so the client
        previews and approves them there rather than through a second path.
        """
        from ..core.smartfiles import organize_plan

        body = body or {}
        path = str(body.get("path") or "").strip()
        if not path:
            raise HTTPException(status_code=422, detail="a folder path is required")
        try:
            return organize_plan(path, by=str(body.get("by") or "type"))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    @router.post("/rename-plan")
    async def rename_plan_route(body: Dict[str, Any] = Body(None)) -> Dict[str, Any]:
        """Propose a bulk rename, reporting collisions rather than resolving them.

        Operations come back in the apply route's shape, so nothing is renamed
        here.
        """
        from ..core.smartfiles import rename_plan

        body = body or {}
        path = str(body.get("path") or "").strip()
        if not path:
            raise HTTPException(status_code=422, detail="a folder path is required")
        try:
            return rename_plan(
                path,
                prefix=str(body.get("prefix") or ""),
                suffix=str(body.get("suffix") or ""),
                find=str(body.get("find") or ""),
                replace=str(body.get("replace") or ""),
                sequence=body.get("sequence") is True,
                start=int(body.get("start") or 1))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    @router.get("/recovery")
    async def file_recovery_report() -> Dict[str, Any]:
        """Interrupted transactions and what a restart can honestly conclude.

        Read-only with respect to your files: it compares recorded hashes with
        what is on disk now and classifies each operation as completed,
        not_applied, changed_since or unknown. Nothing is replayed -- a repair
        needs its own authorisation, and this route does not provide one.
        """
        from ..core.journal import FileTransactionJournal, sha256_of_file

        journal = FileTransactionJournal()
        try:
            reports = journal.reconcile(sha256_of_file)
        except Exception as exc:
            raise HTTPException(status_code=500,
                                detail=f"the file journal could not be read: {exc}")
        needing_action = [r for r in reports if r.get("state") in ("unknown", "failed")]
        return {"transactions": reports,
                "needing_action": len(needing_action),
                "note": ("Reconciling only reports. Applying a repair or an undo is a "
                         "separate, authorised action.")}

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