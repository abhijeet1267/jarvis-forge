"""Routes for the Skill Evolution Lab.

Three reads and one write, and the write stores a *new version* the caller has
already reviewed -- never a mutation of the live one. There is deliberately no
route that applies a proposal without its manifest coming back through the
normal ``POST /skills`` validation, because that endpoint is where a hand-edited
manifest and a proposed one are checked by the same rules.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, HTTPException


def build_router(require_auth) -> APIRouter:
    router = APIRouter(prefix="/skill-evolution", tags=["missions"],
                       dependencies=[Depends(require_auth)])

    @router.get("")
    async def describe_lab() -> Dict[str, Any]:
        """What the lab may change and what it refuses to change."""
        from ..core.skill_evolution import describe_lab as describe

        return describe()

    @router.get("/{skill_id}")
    async def propose(skill_id: str) -> Dict[str, Any]:
        """Read a skill's recorded runs and propose its next version."""
        from ..core.skill_evolution import EvolutionError, propose_evolution
        from ..core.skills import SkillStore

        try:
            proposal = propose_evolution(skill_id, store=SkillStore(),
                                         registry=_registry())
        except EvolutionError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return proposal.to_dict()

    @router.get("/{skill_id}/observations")
    async def observations(skill_id: str) -> Dict[str, Any]:
        """What each condition of the skill's last run actually observed."""
        from ..core.skill_evolution import interpret_run
        from ..core.skills import SkillStore

        try:
            return interpret_run(skill_id, store=SkillStore())
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @router.post("/{skill_id}/accept")
    async def accept(skill_id: str,
                     body: Dict[str, Any] = Body(None)) -> Dict[str, Any]:
        """Store a proposal as the skill's next version.

        With no body the proposal is re-derived here, so the stored version is
        exactly what the lab shows. A caller that *does* send a manifest has it
        re-validated by ``SkillManifest.from_dict`` and ``validate_skill`` like
        any hand-written one. Either way the version becomes ``latest + 1`` and
        every older version is kept.
        """
        from ..core.skill_evolution import EvolutionError, propose_evolution
        from ..core.skills import SkillManifest, SkillStore, validate_skill

        body = body or {}
        raw = body.get("manifest")

        if raw is None:
            # Draw the proposal again, server-side. That is the honest default:
            # the version stored is then exactly what the lab proposes now, not
            # whatever a client kept a copy of. It also means a client cannot
            # hand the store something the lab never proposed without saying so.
            try:
                raw = propose_evolution(skill_id, store=SkillStore(),
                                        registry=_registry()).manifest
            except EvolutionError as exc:
                raise HTTPException(status_code=404, detail=str(exc))
            if raw is None:
                raise HTTPException(
                    status_code=409,
                    detail=("the lab proposes no change for this skill right now; "
                            "there is no new version to store"))

        if not isinstance(raw, dict):
            raise HTTPException(status_code=422,
                                detail="the manifest to store must be an object")
        if str(raw.get("skill_id") or "") != str(skill_id):
            raise HTTPException(
                status_code=422,
                detail=("the manifest names a different skill than the URL; a "
                        "proposal may only be stored under the skill it was made for"))

        try:
            manifest = SkillManifest.from_dict(raw)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

        problems = validate_skill(
            manifest,
            available_capabilities=set(body.get("available_capabilities") or
                                       raw.get("required_capabilities") or []),
            allowed_scope=list(body.get("allowed_scope") or
                               raw.get("required_scope") or []))
        # A capability or scope check needs a caller who states what they hold.
        # When neither the body nor the manifest says, the structural problems
        # are what can honestly be reported; validation still ran on them.
        structural = [p for p in problems
                      if not p.startswith(("capability ", "required scope "))]
        if structural:
            raise HTTPException(status_code=422, detail="; ".join(structural))

        stored = SkillStore().save(manifest)
        return {
            "stored": stored.to_dict(),
            "version": stored.version,
            "supersedes": int(body.get("from_version") or 0) or None,
            "note": ("the previous version is kept; this one is a new version "
                     "and neither is run until something runs it"),
        }

    return router


def _registry():
    """The tool registry, or None when it cannot be built.

    A missing registry is not an error: the lab simply has no second opinion
    about risk tiers, and says so by leaving the manifest's claim alone.
    """
    try:
        from .app import get_core

        return get_core().tools
    except Exception:
        return None