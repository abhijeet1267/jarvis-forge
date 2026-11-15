"""Routes for IntentShield, the skills library, and the file journal.

Kept in one router because they are all read-mostly inspection surfaces whose
job is to make the system's own state legible: what a mission is authorized to
do, what skills exist, and whether an interrupted file commit needs attention.
None of them executes anything -- execution stays in the mission path.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, HTTPException


def build_router(require_auth) -> APIRouter:
    router = APIRouter(prefix="/intelligence", tags=["intelligence"],
                       dependencies=[Depends(require_auth)])

    @router.post("/intent/check")
    async def check_intent(body: Dict[str, Any] = Body(None)) -> Dict[str, Any]:
        """Ask whether a proposed step stays inside an authorized intent.

        The caller supplies the intent and the step; the answer names the
        violated constraint so the client can explain the refusal rather than
        just greying out a button.
        """
        from ..security.intentshield import IntentScope

        body = body or {}
        try:
            scope = IntentScope.from_dict(body.get("intent") or {})
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc))

        decision = scope.check_step(
            str(body.get("tool") or ""),
            dict(body.get("params") or {}),
            plan_revision=int(body.get("plan_revision") or scope.plan_revision))
        return decision.to_dict()

    @router.get("/leases/active")
    async def active_leases(limit: int = 50) -> Dict[str, Any]:
        """Every lease currently usable. What Safety Center shows."""
        from ..security.leases import LeaseService
        from ..db.database import get_db

        return {"leases": [l.to_dict()
                           for l in LeaseService(get_db()).active(limit=limit)]}

    @router.post("/leases/{lease_id}/revoke")
    async def revoke_lease(lease_id: str,
                           body: Dict[str, Any] = Body(None)) -> Dict[str, Any]:
        """Revoke one lease. Effective at the next use, durably."""
        from ..security.leases import LeaseService
        from ..db.database import get_db

        LeaseService(get_db()).revoke(
            lease_id, reason=str((body or {}).get("reason") or "revoked by the operator"),
            actor="user")
        return {"revoked": True, "lease_id": lease_id}

    @router.post("/leases/revoke-all")
    async def revoke_all_leases() -> Dict[str, Any]:
        """Emergency stop: revoke every active lease at once.

        Work that needed one stops rather than continuing on a stale grant. The
        count is what was actually revoked, so the caller can say how much
        stopped rather than implying everything did.
        """
        from ..security.leases import LeaseService
        from ..db.database import get_db

        service = LeaseService(get_db())
        revoked = 0
        for lease in service.active(limit=200):
            service.revoke(lease.lease_id, reason="emergency stop", actor="user")
            revoked += 1
        return {"revoked": revoked}

    @router.post("/intent/delta")
    async def intent_delta(body: Dict[str, Any] = Body(None)) -> Dict[str, Any]:
        """What a plan revision change adds, before anyone approves it.

        The Trust Delta: a changed plan must not inherit the old approval. This
        reports the expansion in words a person can weigh -- a new capability,
        a new resource prefix, a higher risk budget -- and says whether the
        change needs fresh approval, rather than letting a widened plan reuse a
        decision made for a narrower one.
        """
        from ..security.intentshield import IntentScope, scope_delta

        body = body or {}
        try:
            old = IntentScope.from_dict(body.get("old") or {})
            new = IntentScope.from_dict(body.get("new") or {})
            delta = scope_delta(old, new)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc))

        expanded = bool(delta.get("added_capabilities")
                        or delta.get("added_resource_prefixes")
                        or delta.get("increased_risk"))
        return {**delta,
                "expanded": expanded,
                "requires_new_approval": expanded,
                "note": ("The plan now asks for more than it was approved for. "
                         "A previous approval does not cover this."
                         if expanded else
                         "The plan did not widen its scope, so the existing "
                         "approval still describes it.")}

    @router.get("/missions/{mission_id}/schedule")
    async def mission_schedule(mission_id: str) -> Dict[str, Any]:
        """Which steps may run now, and why the others are waiting.

        Read-only: it reports the scheduler's decision without starting anything.
        """
        from ..core.missions import MissionService
        from ..core.scheduler import schedule_summary
        from ..db.database import get_db

        mission = MissionService(get_db()).get(mission_id)
        if mission is None:
            raise HTTPException(status_code=404, detail=f"no mission {mission_id!r}")
        return schedule_summary(mission.steps)

    # -- Demonstrate-to-Automate -------------------------------------------
    #
    # One recorder process-wide: a demonstration is a single live session, and
    # two concurrent recorders would interleave one person's steps into another
    # recording.
    @router.post("/recordings")
    async def start_recording(body: Dict[str, Any] = Body(None)) -> Dict[str, Any]:
        """Begin an explicit recording. Nothing is captured implicitly."""
        from ..core.recorder import Recorder, RecordingError, _recorder_singleton

        try:
            recording = _recorder_singleton().start(str((body or {}).get("name") or ""))
        except RecordingError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return recording.to_dict()

    @router.get("/recordings/current")
    async def current_recording() -> Dict[str, Any]:
        """The open recording, or null.

        The screen polls this while a demonstration is in progress, and reads it
        on appear so reopening the screen does not lose sight of a recording that
        is still open.
        """
        from ..core.recorder import _recorder_singleton

        recording = _recorder_singleton().current()
        return {"recording": recording.to_dict() if recording is not None else None}

    @router.post("/recordings/steps")
    async def capture_step(body: Dict[str, Any] = Body(None)) -> Dict[str, Any]:
        """Record one semantic step. A sensitive parameter is refused, not stored."""
        from ..core.recorder import Recorder, RecordingError, _recorder_singleton

        body = body or {}
        try:
            step = _recorder_singleton().capture(
                str(body.get("kind") or ""),
                dict(body.get("params") or {}),
                label=str(body.get("label") or ""))
        except RecordingError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return step.to_dict()

    @router.post("/recordings/stop")
    async def stop_recording() -> Dict[str, Any]:
        from ..core.recorder import Recorder, RecordingError, _recorder_singleton

        try:
            return _recorder_singleton().stop().to_dict()
        except RecordingError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @router.post("/recordings/draft")
    async def recording_draft(body: Dict[str, Any] = Body(None)) -> Dict[str, Any]:
        """The reviewable workflow draft. Unverified until a replay succeeds.

        An optional ``steps`` list is the review: the values the person supplied
        in the editor, in the order the steps should run. Supplying them here
        rather than at run time is what lets a value filled in after capture
        produce a checkable condition -- the person is reviewing, and what they
        review is exactly what the replay will be checked against.
        """
        from ..core.recorder import Recorder, RecordingError, _recorder_singleton

        body = body or {}
        reviewed = body.get("steps")
        try:
            return _recorder_singleton().derive(
                reviewed=reviewed if isinstance(reviewed, list) else None)
        except RecordingError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @router.delete("/recordings/current")
    async def discard_recording() -> Dict[str, Any]:
        """Discard the current recording. Nothing is left behind."""
        from ..core.recorder import Recorder, _recorder_singleton

        return {"discarded": _recorder_singleton().discard()}

    @router.get("/goals")
    async def list_goals() -> Dict[str, Any]:
        """Opt-in monitored goals and when each was last observed."""
        from ..core.goalguard import GoalStore

        return {"goals": [g.public() if hasattr(g, "public") else g.to_dict()
                          for g in GoalStore().list()]}

    @router.post("/goals")
    async def add_goal(body: Dict[str, Any] = Body(None)) -> Dict[str, Any]:
        """Register a goal. Monitoring is opt-in, and a short interval is refused."""
        from ..core.goalguard import GoalStore

        try:
            goal = GoalStore().add(body or {})
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return goal.public() if hasattr(goal, "public") else goal.to_dict()

    @router.post("/goals/{goal_id}/disable")
    async def disable_goal(goal_id: str) -> Dict[str, Any]:
        from ..core.goalguard import GoalStore

        GoalStore().disable(goal_id)
        return {"disabled": True, "goal_id": goal_id}

    @router.post("/goals/tick")
    async def tick_goals() -> Dict[str, Any]:
        """Observe every goal that is due, right now.

        The caller owns the cadence; `due()` guarantees a goal is returned at
        most once per interval, so repeated ticks cannot form duplicate loops.
        """
        from ..core.goalguard import GoalStore, run_due_observations

        return {"observations": run_due_observations(GoalStore())}

    @router.get("/skills")
    async def list_skills() -> Dict[str, Any]:
        """Installed skills with their measured reliability."""
        from ..core.skills import SkillStore

        store = SkillStore()
        out = []
        for manifest in store.list():
            entry = manifest.to_dict()
            entry["reliability"] = store.reliability(manifest.skill_id)
            out.append(entry)
        return {"skills": out}

    @router.post("/skills")
    async def save_skill(body: Dict[str, Any] = Body(None)) -> Dict[str, Any]:
        """Validate and store a skill manifest.

        A manifest is untrusted input until validated: unknown fields, a bad
        risk class or an empty step list are refused here rather than failing
        later at execution time.
        """
        from ..core.skills import SkillManifest, SkillStore, validate_skill

        body = body or {}
        try:
            manifest = SkillManifest.from_dict(body.get("skill") or body)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

        problems = validate_skill(
            manifest,
            available_capabilities=set(body.get("available_capabilities") or []),
            allowed_scope=list(body.get("allowed_scope") or []))
        if problems:
            raise HTTPException(status_code=422, detail="; ".join(problems))

        stored = SkillStore().save(manifest)
        return stored.to_dict()

    @router.post("/skills/{skill_id}/enable")
    async def enable_skill(skill_id: str) -> Dict[str, Any]:
        """Re-enable a skill. A disabled skill cannot run; this is the way back."""
        from ..core.skills import SkillStore

        return {"enabled": SkillStore().enable(skill_id), "skill_id": skill_id}

    @router.post("/skills/{skill_id}/run")
    async def run_skill_route(skill_id: str,
                              body: Dict[str, Any] = Body(None)) -> Dict[str, Any]:
        """Execute a stored skill as a real mission.

        The skill's steps become a mission and go through the same permission
        engine, leases and verifiers as any other work. Callers state which
        capabilities and resource scope they actually hold; a skill never
        widens that. Verification is the skill's own declarative checks, so a
        run with no check cannot report success.
        """
        from ..core.skill_runner import SkillRunError, run_skill
        body = body or {}
        try:
            from .app import get_core as _get_core
            from .missions_routes import _executor_factory, _gate_factory

            core = _get_core()
            result = await run_skill(
                skill_id,
                execute_step=_executor_factory(core),
                permission=_gate_factory(core),
                approval=body.get("approval"),
                run_id=str(body.get("run_id") or ""),
                available_capabilities=list(body.get("available_capabilities") or []),
                allowed_scope=list(body.get("allowed_scope") or []),
                max_risk=body.get("max_risk"))
        except SkillRunError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return result

    @router.post("/skills/{skill_id}/disable")
    async def disable_skill(skill_id: str) -> Dict[str, Any]:
        from ..core.skills import SkillStore

        return {"disabled": SkillStore().disable(skill_id), "skill_id": skill_id}

    @router.get("/journal")
    async def journal_report() -> Dict[str, Any]:
        """Reconcile any file transaction a crash left unfinished.

        Read-only with respect to the user's files: it compares recorded hashes
        with what is on disk now and classifies each entry. Nothing is replayed.
        """
        from ..core.journal import FileTransactionJournal, sha256_of_file

        journal = FileTransactionJournal()
        try:
            reports = journal.reconcile(sha256_of_file)
            # Bound the stored history so the file cannot grow without limit.
            # Unfinished transactions are never pruned.
            journal.prune(keep=50)
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"the file journal could not be read: {exc}")
        return {"transactions": reports, "unfinished_before_reconcile":
                len(journal.unfinished())}

    return router
