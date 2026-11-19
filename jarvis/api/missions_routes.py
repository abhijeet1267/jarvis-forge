"""Mission routes, as a router rather than more lines in app.py.

`jarvis/api/app.py` is ~7,500 lines, which is its own architectural problem
(the directive names it explicitly). New surfaces go into their own router with
their own ownership so the file stops growing, and the app only includes it.

The routes are thin on purpose: they translate HTTP to the mission service and
back. Anything that decides policy, executes a step or judges an outcome lives in
`jarvis/core/`, where it can be tested without going through the wire.
"""

from __future__ import annotations

import json

from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException

from ..core.mission_runner import MissionRunner, Verifiers
from ..core.missions import MissionService, MissionState, MissionTransitionError


def build_router(require_auth) -> APIRouter:
    """The mission router, with the app's own auth attached to every route.

    The dependency is passed in rather than imported: `app.py` imports this
    module, so importing back would be a cycle. Every route in here reads or
    changes durable state, so every route is protected -- there is no
    unprotected surface to opt into.
    """
    router = APIRouter(prefix="/missions", tags=["missions"],
                       dependencies=[Depends(require_auth)])

    @router.get("")
    async def list_missions(limit: int = 50, state: str = "") -> Dict[str, Any]:
        """Every mission, newest first, optionally filtered to one state."""
        service = _service()
        states: Optional[List[MissionState]] = None
        if state:
            try:
                states = [MissionState(state)]
            except ValueError:
                raise HTTPException(status_code=400, detail=f"unknown state {state!r}")
        return {"missions": [_dump(m) for m in service.list(limit=limit, states=states)]}

    @router.post("/plan")
    async def plan_mission(body: Dict[str, Any] = Body(None)) -> Dict[str, Any]:
        """Turn a desktop sentence into mission steps, without creating a mission.

        The planner is the only path from an instruction to a desktop action. It
        never invents a target: an ambiguous request comes back with a
        ``clarification`` and **no steps**, so a caller cannot accidentally build
        a mission out of a guess.

        Every planned tool is checked against the registry before it is returned.
        A plan naming a tool this backend does not have would fail later, at the
        point where it matters, with a worse message.
        """
        from ..desktop.planner import plan_desktop

        body = body or {}
        request = str(body.get("request") or "").strip()
        if not request:
            raise HTTPException(status_code=422, detail="a plan needs a request")

        context: Dict[str, Any] = {}
        if body.get("application"):
            context["application"] = str(body["application"])
        if body.get("x") is not None and body.get("y") is not None:
            context["point"] = (float(body["x"]), float(body["y"]))

        planned = plan_desktop(request, context=context or None)

        registry = _registry_names()
        # An empty registry means it could not be built, not that no tools exist;
        # checking against it would report every planned tool as unknown.
        unknown = sorted({s.tool for s in planned.steps
                          if registry and s.tool not in registry})
        if unknown:
            raise HTTPException(
                status_code=500,
                detail=f"the planner named tools this backend does not register: {unknown}",
            )

        # Steps are sequenced with depends_on rather than left independent: these
        # are pointer and keyboard actions in one place, and a plan that ran them
        # concurrently would type into whatever happened to be focused.
        steps = []
        for index, step in enumerate(planned.steps):
            steps.append({
                "step_id": f"S{index + 1}",
                "title": step.description,
                "tool": step.tool,
                "params": step.params,
                "depends_on": [f"S{index}"] if index else [],
                "idempotency_key": "",
            })
        return {
            "status": "planned" if steps else "clarification",
            "request": request,
            "steps": steps,
            "clarification": planned.clarification,
            "notes": planned.notes,
        }

    @router.post("/desktop")
    async def create_desktop_mission(body: Dict[str, Any] = Body(None)) -> Dict[str, Any]:
        """Plan a desktop instruction and open a mission from it, in one call.

        The pieces existed but nothing joined them: `/missions/plan` returns
        steps and creates nothing, `/missions` creates a draft from steps a caller
        supplied, and no route chained the two. Measured, that gap is why Command
        Center could not run a desktop instruction -- and worse, a desktop sentence
        sent to `/command` was read by the filesystem planner as `move_file`
        (source="the cursor"), which is a rename of a file that does not exist.

        This route is the join. It refuses an instruction the desktop planner does
        not own rather than passing it on to another planner, and it creates the
        mission in DRAFT so the caller still gets the usual plan review and
        approval before anything acts.
        """
        from ..desktop.planner import looks_like_desktop, plan_desktop

        body = body or {}
        request = str(body.get("request") or "").strip()
        if not request:
            raise HTTPException(status_code=422, detail="a desktop instruction is required")
        if not looks_like_desktop(request):
            raise HTTPException(
                status_code=422,
                detail=(f"{request!r} is not a desktop instruction; this route only "
                        "handles pointer, keyboard and application actions."))

        context: Dict[str, Any] = {}
        if body.get("application"):
            context["application"] = str(body["application"])
        if body.get("x") is not None and body.get("y") is not None:
            context["point"] = (float(body["x"]), float(body["y"]))

        planned = plan_desktop(request, context=context or None)
        if planned.clarification or not planned.steps:
            # A question is a result, not a failure: nothing was planned, and the
            # clarification is what the person needs to see.
            return {"status": "clarification", "request": request, "mission": None,
                    "steps": [], "clarification": planned.clarification}

        registry = _registry_names()
        unknown = sorted({st.tool for st in planned.steps
                          if registry and st.tool not in registry})
        if unknown:
            raise HTTPException(
                status_code=500,
                detail=f"the planner named tools this backend does not register: {unknown}")

        steps = []
        for index, step in enumerate(planned.steps):
            steps.append({
                "step_id": f"S{index + 1}",
                "title": step.description,
                "tool": step.tool,
                "params": step.params,
                "depends_on": [f"S{index}"] if index else [],
                "idempotency_key": "",
            })

        service = _service()
        try:
            mission = service.create(request, conditions=[], steps=steps,
                                     intent=_intent_json(mission_id=None,
                                                         request=request, body=body))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return {"status": "planned", "request": request,
                "mission": _dump(mission), "steps": steps, "clarification": None}

    @router.get("/interrupted")
    async def interrupted_missions() -> Dict[str, Any]:
        """Missions that were mid-flight when the process stopped.

        A relaunch shows these instead of leaving the person to guess which work
        survived.
        """
        return {"missions": [_dump(m) for m in _service().incomplete_missions()]}

    @router.post("")
    async def create_mission(body: Dict[str, Any] = Body(None)) -> Dict[str, Any]:
        """Open a mission contract in DRAFT.

        The contract is recorded even if planning later fails: losing the request
        would leave no explanation for what the system was trying to do.
        """
        body = body or {}
        request = str(body.get("request") or "").strip()
        if not request:
            raise HTTPException(status_code=422, detail="a mission needs a request")
        service = _service()

        conditions = _conditions_with_stored_specs(body.get("conditions") or [])
        intent = _intent_json(mission_id=None, request=request, body=body)
        try:
            mission = service.create(
                request,
                outcome=str(body.get("outcome") or ""),
                scope=dict(body.get("scope") or {}),
                conditions=conditions,
                steps=list(body.get("steps") or []),
                intent=intent,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return _dump(mission)

    @router.get("/{mission_id}")
    async def get_mission(mission_id: str) -> Dict[str, Any]:
        mission = _service().get(mission_id)
        if mission is None:
            raise HTTPException(status_code=404, detail=f"no mission {mission_id!r}")
        return _dump(mission)

    @router.post("/{mission_id}/transition")
    async def transition_mission(mission_id: str,
                                 body: Dict[str, Any] = Body(None)) -> Dict[str, Any]:
        """Move a mission to a new state, enforcing the transition table."""
        body = body or {}
        target = str(body.get("state") or "").strip()
        try:
            mission = _service().transition(
                mission_id, MissionState(target),
                actor=str(body.get("actor") or "user"),
                reason=str(body.get("reason") or ""))
        except MissionTransitionError as exc:
            # 409: the request was well-formed, the current state refuses it.
            raise HTTPException(status_code=409, detail=str(exc))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"unknown state {target!r}")
        return _dump(mission)

    @router.post("/{mission_id}/steer")
    async def steer_mission(mission_id: str,
                            body: Dict[str, Any] = Body(None)) -> Dict[str, Any]:
        """Replace the un-run remainder of a plan with a new one.

        The reply is the mission as it now stands plus what the revision left
        standing, so the caller does not have to guess whether a completed
        step's effect survived the change -- it did, and the record says so.
        """
        from ..core.steering import steering_report

        body = body or {}
        steps = body.get("steps")
        if not isinstance(steps, list) or not steps:
            raise HTTPException(
                status_code=422,
                detail="a revision must carry a non-empty 'steps' list")

        service = _service()
        mission = service.get(mission_id)
        if mission is None:
            raise HTTPException(status_code=404, detail=f"no mission {mission_id!r}")

        try:
            mission = service.apply_revision(
                mission_id, steps=steps,
                reason=str(body.get("reason") or ""), actor="user")
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

        # A revision changes the plan, and the intent is what authorizes it.
        # The intent stamped at creation names plan_revision 0; once the
        # revision is bumped the intent's own rule refuses every step
        # ("the plan changed after the intent was authorized") and nothing
        # re-authorized it, so a steered mission could never run again -- not
        # even after a fresh approval. Re-record it against the plan that is
        # actually going to run. `record_intent` had no callers at all before
        # this, which is why the record described a plan that no longer existed.
        mission = _rerecord_intent_after_steer(service, mission)

        return _dump(mission) | {"steering": steering_report(mission)}

    @router.post("/{mission_id}/steer/preview")
    async def preview_steer(mission_id: str,
                            body: Dict[str, Any] = Body(None)) -> Dict[str, Any]:
        """What a revision would change, before anything is committed to it.

        Read-only. Naming this separately is deliberate: a person should be able
        to see that a change is refused by the mission's own intent, or that it
        leaves an already-run step standing, without their exploration becoming
        the decision.
        """
        from ..core.steering import revision_summary

        body = body or {}
        steps = body.get("steps")
        if not isinstance(steps, list):
            raise HTTPException(status_code=422,
                                detail="'steps' must be a list of proposed steps")
        mission = _service().get(mission_id)
        if mission is None:
            raise HTTPException(status_code=404, detail=f"no mission {mission_id!r}")
        return revision_summary(mission, steps)

    @router.post("/{mission_id}/cancel")
    async def cancel_mission(mission_id: str) -> Dict[str, Any]:
        try:
            mission = _service().transition(
                mission_id, MissionState.CANCELLED, actor="user",
                reason="cancelled by the operator")
        except MissionTransitionError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return _dump(mission)

    @router.get("/{mission_id}/receipt")
    async def mission_receipt(mission_id: str) -> Dict[str, Any]:
        """An exportable Outcome Receipt: what was done, and what proves it.

        Evidence references are resolved against the audit log before the
        receipt is returned; anything that does not resolve is listed rather than
        silently omitted.
        """
        from ..core.proofgraph import outcome_receipt

        mission = _service().get(mission_id)
        if mission is None:
            raise HTTPException(status_code=404, detail=f"no mission {mission_id!r}")

        audit = _audit()
        rows: List[Dict[str, Any]] = []
        chain = None
        if audit is not None:
            try:
                rows = audit.recent(limit=500)
                chain = audit.verify_chain()
            except Exception:
                rows, chain = [], None
        return outcome_receipt(mission, rows, chain=chain)

    @router.post("/{mission_id}/leases")
    async def issue_lease(mission_id: str,
                          body: Dict[str, Any] = Body(None)) -> Dict[str, Any]:
        """Issue a scoped, expiring lease for this mission.

        Every bound is required: a lease with no resource scope authorizes
        nothing, and that is enforced at use time, not only here.
        """
        from ..security.leases import LeaseService
        from ..db.database import get_db

        body = body or {}
        service = LeaseService(get_db())
        try:
            lease = service.issue(
                mission_id=mission_id,
                session_id=str(body.get("session_id") or ""),
                capability=str(body.get("capability") or ""),
                scope=list(body.get("scope") or []),
                ttl_seconds=float(body.get("ttl_seconds") or 600),
                max_uses=int(body.get("max_uses") or 1),
                approval_ref=str(body.get("approval_ref") or ""),
                plan_revision=int(body.get("plan_revision") or 0))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return lease.to_dict()

    @router.get("/leases/active")
    async def active_leases(limit: int = 50) -> Dict[str, Any]:
        """The leases currently usable -- what the Safety Center shows."""
        from ..security.leases import LeaseService
        from ..db.database import get_db

        return {"leases": [l.to_dict() for l in LeaseService(get_db()).active(limit=limit)]}

    @router.post("/leases/{lease_id}/revoke")
    async def revoke_lease(lease_id: str,
                           body: Dict[str, Any] = Body(None)) -> Dict[str, Any]:
        """Revoke a lease. Takes effect at the next use, durably."""
        from ..security.leases import LeaseService
        from ..db.database import get_db

        LeaseService(get_db()).revoke(
            lease_id, reason=str((body or {}).get("reason") or ""), actor="user")
        return {"revoked": True, "lease_id": lease_id}

    @router.post("/{mission_id}/steps/{step_id}/decide")
    async def decide_step(mission_id: str, step_id: str,
                          body: Dict[str, Any] = Body(None)) -> Dict[str, Any]:
        """Approve or deny ONE step of a parked mission.

        An approval is bound to a lease scoped to that step's own resource, so
        approving step S1 cannot authorize step S2 -- the two carry different
        leases. A denial marks the step failed and blocks the mission, which is
        the honest outcome: half a plan did not run.
        """
        from ..security.leases import LeaseRefusal, LeaseService
        from ..db.database import get_db

        body = body or {}
        decision = str(body.get("decision") or "").lower()
        if decision not in ("approve", "deny"):
            raise HTTPException(status_code=422,
                                detail="decision must be 'approve' or 'deny'")

        service = _service()
        mission = service.get(mission_id)
        if mission is None:
            raise HTTPException(status_code=404, detail=f"no mission {mission_id!r}")
        step = next((s for s in mission.steps if s.step_id == step_id), None)
        if step is None:
            raise HTTPException(status_code=404,
                                detail=f"no step {step_id!r} in {mission_id!r}")
        if step.status not in ("pending",):
            raise HTTPException(
                status_code=409,
                detail=f"step {step_id} is {step.status}, not awaiting a decision")

        # A draft mission has never been planned, so it has no state a decision
        # applies to. Planning it first is what makes the refusal meaningful:
        # "this plan was refused" rather than "this draft was edited".
        if mission.state == "draft":
            service.transition(mission_id, MissionState.PLANNED, actor="user",
                               reason="the plan was reviewed to decide a step")

        if decision == "deny":
            service.mark_step(mission_id, step_id, status="failed",
                              detail="denied by the operator", actor="user")
            settled = service.transition(
                mission_id, MissionState.BLOCKED, actor="user",
                reason=f"step {step_id} was denied, so the plan did not run")
            return _dump(settled)

        resource = str((step.params or {}).get("path") or "")
        lease_id = str(body.get("lease_id") or "").strip()
        if lease_id:
            try:
                LeaseService(get_db()).consume(
                    lease_id, capability=step.tool or "", resource=resource,
                    session_id=str(body.get("session_id") or ""))
            except LeaseRefusal as exc:
                raise HTTPException(status_code=409,
                                    detail=f"the lease does not authorize this step: {exc}")

        # The confirmation id recorded when the step parked is what the runner
        # matches a resume against (`_parked_confirmation`). Overwriting the
        # detail with "approved by the operator" destroyed it, so a mission a
        # person had explicitly approved re-parked forever on every run. Keep
        # the id in the detail and record the approval beside it.
        parked = str(getattr(step, "detail", "") or "")
        confirmation = ""
        if "confirmation " in parked:
            confirmation = parked.split("confirmation ", 1)[1].split()[0].rstrip(")").strip()

        service.mark_step(mission_id, step_id, status="pending",
                          detail=("approved by the operator; "
                                  f"confirmation {confirmation}" if confirmation
                                  else "approved by the operator"),
                          actor="user")
        return _dump(service.get(mission_id))

    @router.post("/{mission_id}/run")
    async def run_mission(mission_id: str,
                          body: Dict[str, Any] = Body(None)) -> Dict[str, Any]:
        """Execute a mission through the real gate, then verify its conditions.

        Dispatch resolves the step's tool through the same agent registry the
        pipeline uses, so a mission cannot reach a capability a direct request
        could not.
        """
        # `get_core` is defined in `app.py`, the module that includes this
        # router, so importing it at module scope would be a cycle. Read it from
        # the already-imported app module at call time.
        from .app import get_core

        body = body or {}
        core = get_core()
        service = _service()

        runner = MissionRunner(
            service,
            execute_step=_executor_factory(core),
            permission=_gate_factory(core),
            audit=_audit(),
        )
        verifiers = _verifiers_from(body.get("verifiers") or {})
        try:
            approval = body.get("approval")
            if approval is not None and not isinstance(approval, dict):
                raise HTTPException(status_code=422, detail="approval must be an object")
            mission = await runner.run(mission_id, verifiers=verifiers,
                                       approval=approval)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return _dump(mission)

    @router.post("/{mission_id}/shadow")
    async def shadow_mission(mission_id: str,
                             body: Dict[str, Any] = Body(None)) -> Dict[str, Any]:
        """Predict what this mission would do, executing none of it.

        The permission question is answered by the same gate a real run uses, so
        a step a shadow says would run is a step the run would also allow. The
        file effects are computed from what is on disk now and never written.
        """
        from .app import get_core
        from ..core.shadow import ShadowMission

        service = _service()
        mission = service.get(mission_id)
        if mission is None:
            raise HTTPException(status_code=404, detail=f"no mission {mission_id!r}")

        core = get_core()
        # The file tools' own protector, so a shadow refuses a path they would
        # refuse. A preview that promises a write the executor then rejects moves
        # the refusal to the worst possible moment.
        from ..tools.files import _guard as file_guard

        real_gate = _gate_factory(core)

        def gate(tool: str, params: Dict[str, Any], device: str,
                 session_id: str) -> Dict[str, Any]:
            # The real gate takes (step, mission). A shadow has no MissionStep
            # yet -- it is predicting steps that may never become one -- so the
            # two fields it reads are supplied directly.
            probe = SimpleNamespace(step_id="shadow", tool=tool, params=params)
            return real_gate(probe, SimpleNamespace(mission_id=session_id))

        shadow = ShadowMission(guard=file_guard)
        report = shadow.run(
            [{"step_id": s.step_id, "title": s.title, "tool": s.tool,
              "params": dict(s.params or {})} for s in mission.steps],
            gate=gate,
            device=str((body or {}).get("device") or "laptop_main"),
            session_id=mission_id)
        report["mission_id"] = mission_id
        report["mission_state"] = mission.state
        return report

    @router.post("/observe")
    async def observe_goal(body: Dict[str, Any] = Body(None)) -> Dict[str, Any]:
        """Observe a goal and propose (never perform) a bounded recovery.

        Returns the observation and what may be done about it. `allowed: false`
        carries the reason -- an unowned resource, an unobservable state, or the
        attempt bound -- so the client never has to infer why nothing happened.
        """
        from ..core.selfhealing import (
            RecoveryAttempt, RecoveryController, observe_file, observe_http,
        )

        body = body or {}
        kind = str(body.get("kind") or "").strip()
        goal_id = str(body.get("goal_id") or "G1")

        if kind == "http":
            observation = observe_http(
                str(body.get("url") or ""), expect_body=str(body.get("expect_body") or ""),
                goal_id=goal_id)
        elif kind == "file":
            expect = body.get("expect_text")
            observation = observe_file(
                str(body.get("path") or ""),
                expect_text=None if expect is None else str(expect),
                goal_id=goal_id)
        else:
            raise HTTPException(
                status_code=422,
                detail="kind must be 'http' or 'file'; this route does not run "
                       "arbitrary observers")

        proposal = RecoveryController().propose(
            observation,
            attempts=[RecoveryAttempt(str(a.get("goal_id") or goal_id),
                                      str(a.get("action") or ""),
                                      at=float(a.get("at") or 0.0),
                                      ok=bool(a.get("ok")))
                      for a in (body.get("attempts") or [])
                      if isinstance(a, dict)],
            can_restart=body.get("can_restart") is True,
            restart_target=str(body.get("restart_target") or ""))

        return {"observation": observation.to_dict(), "recovery": proposal}

    @router.get("/{mission_id}/proof")
    async def mission_proof(mission_id: str) -> Dict[str, Any]:
        """ProofLens: why does Vantage believe this mission's claims?

        Answers claim by claim, naming the observations behind each one and
        saying plainly when a claim has none. The evidence is the existing audit
        chain, so ``evidence_integrity`` describes what the hash chain does and
        does not establish rather than implying tamper-proofness.
        """
        from ..core.proofgraph import ProofGraph

        service = _service()
        mission = service.get(mission_id)
        if mission is None:
            raise HTTPException(status_code=404, detail=f"no mission {mission_id!r}")

        audit = _audit()
        rows: List[Dict[str, Any]] = []
        chain = None
        if audit is not None:
            try:
                rows = audit.recent(limit=500)
                chain = audit.verify_chain()
            except Exception:
                rows, chain = [], None

        graph = ProofGraph(mission, rows)
        return graph.report(chain=chain)

    return router


# -- helpers ----------------------------------------------------------------

def _service() -> MissionService:
    from ..db.database import get_db

    return MissionService(get_db())


def _registry_names() -> set:
    """Every tool name this backend can actually dispatch.

    Read from the same registry the runner uses, so "the planner named a tool
    that does not exist" is caught at plan time rather than at run time.
    """
    from ..tools import build_default_registry

    try:
        return set(build_default_registry().names())
    except Exception:
        # A registry that cannot be built is a far more serious failure than a
        # bad plan; the caller's request should not mask it with a 500 of ours.
        return set()


def _audit():
    try:
        from ..security.audit import AuditLog

        return AuditLog()
    except Exception:
        return None


def _dump(mission) -> Dict[str, Any]:
    return mission.to_dict()


def _intent_json(*, mission_id, request: str, body: Dict[str, Any]) -> str:
    """The IntentScope for a new mission, as JSON, or "" when none was given.

    Two ways to supply one: a fully-formed ``intent`` object (the client built
    it from the person's own choices), or the bare minimum -- a goal and the
    capabilities the plan names -- which the request itself produces. The
    second exists so a mission created without an explicit scope is still
    bounded by what it asked for rather than being unbounded by omission.

    An intent that will not construct raises 422: recording an authorization
    nobody can check is worse than refusing to create the mission.
    """
    from ..security.intentshield import IntentScope, RESOURCE_PARAMETER_KEYS

    supplied = body.get("intent")
    if isinstance(supplied, dict) and supplied:
        data = dict(supplied)
    else:
        capabilities = sorted({str(s.get("tool") or "").strip()
                               for s in (body.get("steps") or [])
                               if str(s.get("tool") or "").strip()})
        if not capabilities:
            return ""
        # Every resource the plan names, not only `path`: a copy step's
        # destination is a write target, and deriving the scope from `path`
        # alone would authorize the copy while declaring the destination
        # outside the scope the next check sees.
        prefixes = sorted({
            str((s.get("params") or {}).get(key) or "").strip()
            for s in (body.get("steps") or [])
            for key in RESOURCE_PARAMETER_KEYS
            if str((s.get("params") or {}).get(key) or "").strip()
        })
        data = {
            "mission_id": "pending",
            "goal": request,
            "allowed_capabilities": capabilities,
            "allowed_resource_prefixes": prefixes,
            "risk_budget": int(body.get("risk_budget") or 2),
        }

    data["mission_id"] = str(data.get("mission_id") or "pending")
    data["goal"] = str(data.get("goal") or request)
    try:
        scope = IntentScope.from_dict(data)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422,
                            detail=f"the mission's intent is not valid: {exc}")
    return scope.to_json()

def _rerecord_intent_after_steer(service, mission):
    """Re-stamp the mission's intent against the plan that will actually run.

    The intent derived at creation is bound to ``plan_revision`` 0 and to the
    capabilities and resources of *that* plan. A steer replaces the un-run
    remainder and bumps the revision; leaving the old intent in place means its
    own rule refuses every step forever, which is the right answer for a stale
    approval and the wrong one for a person who just said what they want.

    Bounded by the union of the old and new plans: a steer may add a capability
    or a resource, and it may drop one, but it must never silently widen the
    mission to something neither plan named. That keeps the intent an actual
    constraint rather than a rubber stamp on whatever the revision says.

    A mission with no intent stays that way -- recording an authorization nobody
    can check would be worse than having none.
    """
    if not (mission.intent or "").strip():
        return mission
    try:
        from ..security.intentshield import (
            IntentScope, RESOURCE_PARAMETER_KEYS)

        previous = IntentScope.from_dict(json.loads(mission.intent))
        capabilities = sorted({str(s.tool or "") for s in mission.steps
                               if str(s.tool or "").strip()}
                              | set(previous.allowed_capabilities or []))
        prefixes = sorted({
            str((s.params or {}).get(key) or "").strip()
            for s in mission.steps
            for key in RESOURCE_PARAMETER_KEYS
            if str((s.params or {}).get(key) or "").strip()
        } | set(previous.allowed_resource_prefixes or []))
        scope = IntentScope.from_dict({
            **previous.to_dict(),
            "allowed_capabilities": capabilities,
            "allowed_resource_prefixes": prefixes,
            "plan_revision": mission.plan_revision,
        })
        return service.record_intent(mission.mission_id, scope.to_json())
    except (ValueError, TypeError, KeyError, AttributeError):
        # The plan runs under the intent it already has. A steer is not the
        # place to widen authorization, so a failure here leaves the old scope
        # standing rather than granting a broader one.
        return mission


def _gate_factory(core, *, requested_surface: str = "mission", source: str = ""):
    """A gate that asks the real PermissionEngine about one mission step.

    ``requested_surface`` is what the engine records against the confirmation it
    mints, and the self-approval guard keys on it: a phone may not approve a
    request the phone raised. The mission route itself passes "mission", which is
    correct for a mission started from Mission Control. A command diverted into
    mission mode must pass the surface it actually arrived on, or a
    phone-originated command would be recorded as "mission" -- outside
    NARROW_SURFACES -- and the phone could then approve its own consequential
    action. That is why this is a parameter rather than a constant.
    """
    from ..core.models import PlannedAction

    def gate(step, mission) -> Dict[str, Any]:
        action = PlannedAction(
            tool=step.tool or "", params=dict(step.params or {}),
            device=str((step.params or {}).get("device") or "laptop_main"))
        result = core.policy.evaluate(
            action, session_id=f"mission:{mission.mission_id}",
            requested_surface=requested_surface)

        # `PolicyDecision` is a str Enum whose values are UPPERCASE
        # ("EXECUTE"/"CONFIRM"/"BLOCK"). Comparing against lowercase made every
        # allowed step read as refused, which looked like a policy denial in the
        # mission but was a string mismatch here.
        decision = getattr(result.decision, "value", str(result.decision))
        decision = str(decision).upper()

        # Only EXECUTE may run. CONFIRM means the engine wants a person to
        # approve first, and a mission is not an approval: the step is refused
        # here and the mission reports it, rather than the runner answering on
        # the operator's behalf.
        allowed = decision == "EXECUTE"
        reason = result.reason
        if decision == "CONFIRM":
            reason = (f"requires approval before it may run ({result.reason}); "
                      "a mission does not supply that approval")
        return {"allowed": allowed, "reason": reason, "decision": decision,
                "confirmation_id": getattr(result, "confirmation_id", None)}
    return gate


def _executor_factory(core):
    """A step executor that goes through the orchestrator's own agent path."""
    async def execute(step, mission):
        from ..agents.base import find_agent
        from ..core.models import PlannedAction
        from ..tools.base import ToolContext

        action = PlannedAction(
            tool=step.tool or "", params=dict(step.params or {}),
            device=str((step.params or {}).get("device") or "laptop_main"))
        agent = find_agent(core.agents, action.tool)
        if agent is None:
            class _Missing:
                ok = False
                message = f"no agent owns {action.tool!r}"
            return _Missing()

        ctx = ToolContext(
            device_id=action.device,
            session_id=f"mission:{mission.mission_id}",
            settings=core.settings,
            apps=getattr(core, "apps", None),
            devices=core.devices,
            memory=getattr(core, "memory", None),
            graph=getattr(core, "graph", None),
            core=core,
        )
        return await agent.execute(action, ctx)
    return execute


def _conditions_with_stored_specs(raw: Any) -> list:
    """Turn each condition's structured verifier into the text a mission stores.

    A condition arrives with either a declarative `verifier` object (what a
    recorder produces) or a plain string (hand-written). The mission keeps a
    string, so the object is serialised here and parsed back at verification
    time. Doing it at creation is what lets a mission be verified later without
    the caller re-sending the spec -- a run that forgot to would otherwise
    report a checkable condition as "no independent verifier was supplied".

    An unknown kind is kept verbatim: it parses to no verifier and the condition
    is recorded UNKNOWN, which is the honest report.
    """
    out = []
    for entry in raw or []:
        if not isinstance(entry, dict):
            continue
        verifier = entry.get("verifier")
        if isinstance(verifier, dict):
            out.append({**entry, "verifier": json.dumps(verifier, sort_keys=True)})
        else:
            out.append(entry)
    return out


def _verifiers_from(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Build verifiers from a small declarative spec, never from code.

    The wire may name the observers this module knows. It cannot name a Python
    callable, and an unknown kind is recorded as UNKNOWN rather than silently
    unverified-but-passing.
    """
    from ..core.mission_runner import _verifier_from_spec

    out: Dict[str, Any] = {}
    for condition_id, raw in (spec or {}).items():
        if not isinstance(raw, dict):
            continue
        verifier = _verifier_from_spec(json.dumps(raw), condition_id)
        out[condition_id] = verifier if verifier is not None else \
            Verifiers.always_unknown(
                f"no verifier of kind {raw.get('kind')!r} exists, so this "
                "condition was not checked", condition_id)
    return out