"""Routes for Capability Passports.

Read-only except for one explicit POST that records the passport in the audit
log. There is deliberately no route that mints a credential from a passport: a
passport describes standing, and a route that turned a description into
authority would be exactly the bypass this module exists to avoid.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException


def build_router(require_auth) -> APIRouter:
    router = APIRouter(prefix="/passports", tags=["missions"],
                       dependencies=[Depends(require_auth)])

    @router.get("")
    async def list_all() -> Dict[str, Any]:
        """One summary line per worker, so a screen can list before drilling in."""
        from ..core.passports import list_passports

        return {
            "workers": list_passports(registry=_registry()),
            "note": ("counts are what the hash-chained audit log holds; a "
                     "declaration about the registry is not a grant"),
        }

    @router.get("/{worker}")
    async def one(worker: str) -> Dict[str, Any]:
        """One worker's passport: what it holds, has done, and never touched."""
        from ..core.passports import PassportError, build_passport

        try:
            passport = build_passport(worker, registry=_registry())
        except PassportError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return passport.to_dict()

    @router.post("/{worker}/issue")
    async def issue(worker: str) -> Dict[str, Any]:
        """Record in the audit log that this passport was produced.

        The only write here, and it writes an audit entry: who was described and
        what the passport said. It mints no credential and grants nothing.
        """
        from ..core.passports import PassportError, issue_passport

        try:
            return issue_passport(worker, registry=_registry())
        except PassportError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    return router


def _registry():
    """The core's populated registry, or None so the module builds its own.

    A bare ``ToolRegistry()`` holds no tools, so passing one would make every
    passport empty; ``None`` lets the module fall back to the shipped registry.
    """
    try:
        from .app import get_core

        return get_core().tools
    except Exception:
        return None