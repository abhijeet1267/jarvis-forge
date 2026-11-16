"""Routes for the Privacy Egress Firewall.

Two reads and one try: what is declared, whether one destination would be
allowed right now, and a posture summary. There is deliberately **no route that
widens the allowlist** -- adding a destination is a source change a reviewer
sees, not a button that quietly opens a door.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException


def build_router(require_auth) -> APIRouter:
    router = APIRouter(prefix="/egress", tags=["privacy"],
                       dependencies=[Depends(require_auth)])

    @router.get("")
    async def posture() -> Dict[str, Any]:
        """What this project may contact, and what the firewall does not cover."""
        from ..security.egress import describe_firewall

        return describe_firewall()

    @router.post("/check")
    async def check(body: Dict[str, Any] = Body(None)) -> Dict[str, Any]:
        """Would a read to this address be allowed right now?

        Read-only: it asks the firewall and returns the verdict, so a person can
        see *why* a destination would be refused (or admitted) without making the
        request. The check uses the live privacy-mode setting.
        """
        from ..security.egress import get_firewall, privacy_mode_on

        url = str((body or {}).get("url") or "").strip()
        if not url:
            raise HTTPException(status_code=422, detail="a check needs a url")
        verdict = get_firewall().check(url, privacy_mode=privacy_mode_on())
        return verdict.to_dict()

    return router