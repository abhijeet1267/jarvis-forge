"""The orchestrator (sections 4.1, 9, 34, 47, 60).

Implements the full loop:

    Intent -> Plan -> Permission -> Tool -> Execution -> Verification
           -> Memory -> Response

It is the only place that sequences those stages. Agents execute; the policy
engine authorises; the orchestrator coordinates and reports.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..agents.base import Agent, build_agents, find_agent
from ..config import get_settings
from ..db.database import get_db
from ..gateway.router import ModelRouter
from ..logging_setup import get_logger
from ..memory.graph import ProjectGraph
from ..memory.store import MemoryStore
from ..registry.apps import AppRegistry
from ..registry.devices import get_device_registry
from ..registry.tools import ToolRegistry
from ..security.audit import AuditLog
from ..security.policy import PermissionEngine
from ..security.risk import RiskClassifier
from ..tools import build_default_registry
from ..tools.base import ToolContext
from .models import (
    ActionStatus,
    CommandRequest,
    CommandResponse,
    MemoryScope,
    Plan,
    PlannedAction,
    PolicyDecision,
    Verification,
    new_id,
)
from .planner import RulePlanner
from .recovery import RecoveryEngine, RecoveryOutcome
from .tasks import LifecycleReport, TaskManager

log = get_logger("core.orchestrator")


@dataclass
class JobRetry:
    """What a user-requested re-run of a job did.

    ``executed`` counts the actions this call put in front of an agent, so the
    endpoint that reports it cannot drift from what ran. When a step is gated,
    ``executed`` is 0 and ``response`` carries the fresh confirmation to approve.
    """

    job_id: str
    report: LifecycleReport
    response: Optional[CommandResponse] = None

    @property
    def executed(self) -> int:
        return len(self.response.actions) if self.response is not None else 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "accepted": self.report.accepted,
            "status": self.report.status,
            "executed": self.executed,
            "reason": self.report.reason,
            "message": self.report.message,
            "confirmation_id": self.response.confirmation_id if self.response else "",
        }


class JarvisCore:
    """Wires the six core components together and runs the loop."""

    def __init__(
        self,
        tools: Optional[ToolRegistry] = None,
        apps: Optional[AppRegistry] = None,
    ) -> None:
        self.settings = get_settings()
        self.db = get_db()

        self.apps = apps or AppRegistry()
        self.tools = tools or build_default_registry()
        self.devices = get_device_registry()
        self.memory = MemoryStore()
        self.graph = ProjectGraph()
        self.audit = AuditLog()
        # Seal any records written before the audit chain existed, so the very
        # first verification does not report a break on legacy history.
        self.audit.seal_chain()
        self.tasks = TaskManager()
        self.recovery = RecoveryEngine()
        # v0.8: scheduled jobs. Started by the server lifespan; harmless
        # to construct without running.
        from ..scheduler.runner import SchedulerRunner

        self.scheduler = SchedulerRunner()

        self.classifier = RiskClassifier(paranoid=self.settings.paranoid)
        self.policy = PermissionEngine(self.tools, self.devices, self.classifier)
        self.agents: Dict[str, Agent] = build_agents(self.tools)
        self.planner = RulePlanner(self.tools, self.apps)
        self.router = ModelRouter()

        log.info(
            "jarvis core initialised",
            extra_fields={
                "tools": len(self.tools.names()),
                "agents": list(self.agents.keys()),
                "llm": self.settings.llm_enabled,
            },
        )

    # ------------------------------------------------------------------ utils
    def _context(self, request: CommandRequest) -> ToolContext:
        return ToolContext(
            device_id=request.device_id,
            session_id=request.session_id,
            request_id="",
            settings=self.settings,
            apps=self.apps,
            devices=self.devices,
            memory=self.memory,
            graph=self.graph,
            core=self,
        )

    def _log_message(self, session_id: str, role: str, content: str, intent: str = "") -> None:
        self.db.execute(
            """
            INSERT INTO messages (message_id, session_id, role, content, intent)
            VALUES (?, ?, ?, ?, ?)
            """,
            (new_id("MSG"), session_id, role, content, intent),
        )
        self.db.execute(
            "INSERT OR IGNORE INTO sessions (session_id, user_id) VALUES (?, 'user')",
            (session_id,),
        )

    @staticmethod
    def _audit_token(result: Any) -> str:
        """The status token the audit log stores for a result.

        One definition on purpose: there are two ``audit.record`` call sites
        (the interactive plan and confirmation resolution), and they had drift
        -- one emitted ``unverified`` while ``ActionResult.audit_result`` yields
        ``unknown`` for the same state, so any filter on the stored status would
        silently miss confirmed actions.
        """
        verification = getattr(result, "verification", Verification.UNKNOWN)
        if verification is Verification.SUCCESS:
            return "success"
        if verification is Verification.FAILED:
            return "failure"
        if verification is Verification.UNKNOWN:
            # A simulated actuation is its own fact. "unverified" would mean "we
            # do not know whether the world changed"; here we know -- nothing in
            # the real world changed, and the handler said so. Since Phase 13 the
            # dashboard has been the only place telling the truth about this while
            # the hash chain recorded success.
            if isinstance(getattr(result, "output", None), dict) \
                    and result.output.get("simulated") is True:
                return "simulated"
            return "unverified"
        # Anything unrecognised stays unverified rather than defaulting to success,
        # which is how this function behaved before the UNKNOWN branch was added.
        return "unverified"

    @staticmethod
    def _action_status(result: Any) -> ActionStatus:
        """Map an ActionResult onto a job-step status.

        Keyed on ``verification``, not ``ok``. A handler that answered without
        observing anything lands on UNVERIFIED -- neither the failure it was not
        nor the success it did not prove.
        """
        if not getattr(result, "ok", False):
            return ActionStatus.FAILED
        verification = getattr(result, "verification", Verification.UNKNOWN)
        if verification is Verification.SUCCESS:
            return ActionStatus.SUCCEEDED
        if verification is Verification.FAILED:
            return ActionStatus.FAILED
        return ActionStatus.UNVERIFIED

    @staticmethod
    def _note_unhandled(response: CommandResponse, plan: Plan) -> None:
        """Say which part of the sentence this plan did not cover.

        The planner computes a reason for every clause it could not turn into a
        step; until Phase 19 that text was discarded, so half a request could be
        refused while the answer reported only the half that ran.
        """
        for clause in getattr(plan, "unhandled", None) or []:
            response.reply = f"{response.reply}\n\nAnd I did not do that part: {clause}"

    def _settle_step(
        self,
        confirmation_id: str,
        status: ActionStatus,
        detail: str,
        response: CommandResponse,
    ) -> int:
        """Move the workflow step an approval was holding, and report what is left.

        The approval arrives in a different request from the plan that created it,
        so nothing else could find the step again. Measured before this existed: a
        two-step goal that halted for confirmation showed ``running`` with the
        waiting step ``pending``, and stayed that way after the approval expired.

        Returns how many steps are still waiting behind this one.
        """
        step = self.tasks.step_for_confirmation(confirmation_id)
        if step is None:
            return 0
        response.job_id = step["job_id"]
        self.tasks.update_step(step["job_id"], step["name"], status, detail, seq=step["seq"])
        self.tasks.refresh_status(step["job_id"])
        waiting = (ActionStatus.PENDING.value, ActionStatus.RUNNING.value)
        return sum(
            1 for s in self.tasks.step_statuses(step["job_id"]) if s in waiting
        )

    def _audit_action(
        self,
        action: PlannedAction,
        request: CommandRequest,
        result: Optional[Any],
        risk: str,
        confirmation: bool,
        request_id: str = "",
        unhandled: Optional[List[str]] = None,
        answered_by: str = "",
        reason: str = "",
    ) -> None:
        # ``result`` is a status token so AuditLog.failures() can filter on it;
        # the human-readable message goes into ``detail``.
        #
        # Derived from ``verification``, never from ``ok``. ``ok`` means "the
        # handler returned without raising", which an offline gateway no-op
        # satisfies just as happily as a real actuation -- so the previous
        # ``"success" if result.ok else "failure"`` logged "nothing was
        # switched" as a success. The existing ``success``/``failure`` tokens
        # are kept verbatim so ``AuditLog.failures()`` and every stored record
        # still read the same way; ``unverified`` is a third, honest state.
        if result is None:
            # A block has no handler result to summarise, so the gate's own reason is
            # the message. It used to be dropped on the floor here: the verdict carried
            # it, the caller had it, and the chain recorded ``{"ok": false,
            # "message": ""}`` -- which made the permission engine's refusals, the most
            # security-relevant rows in the ledger, the only ones with no stated cause.
            status, message = "blocked", reason
        else:
            status, message = self._audit_token(result), result.summary

        detail: Dict[str, Any] = {"ok": bool(result and result.ok), "message": message}
        if answered_by:
            # R-14: a successful fallback hands back another tool's result, and
            # this row is keyed on the *planned* tool. Without the attribution
            # the ledger said `search_files` had produced a directory listing.
            detail["answered_by"] = answered_by
        if unhandled:
            # Which parts of the same sentence this action did not cover. Recorded
            # rather than only spoken, so a later reader of the chain can see the
            # request was answered in part.
            detail["unhandled"] = list(unhandled)
        self.audit.record(
            intent=request.text,
            tool=action.tool,
            target=json.dumps(action.params, default=str),
            risk=risk,
            confirmation=confirmation,
            result=status,
            device=action.device,
            session_id=request.session_id,
            # ``CommandRequest`` has no request id of its own; the response's REQ-
            # id is what the user was answered with, so it has to be handed down.
            # This used to be ``request.session_id``, which put a SESS- value in
            # the request_id column for every executed action and made the ledger
            # unjoinable to the reply it described.
            request_id=request_id,
            detail=detail,
        )

    # ------------------------------------------------------------------ main
    async def handle(self, request: CommandRequest) -> CommandResponse:
        """Process one inbound command end-to-end."""
        started = time.perf_counter()
        response = CommandResponse(session_id=request.session_id, request_id=new_id("REQ"))
        self._log_message(request.session_id, "user", request.text)

        # --- Stage 0: hard safety guard on the raw phrasing ------------------
        guard = self.classifier.classify_command(request.text)
        if guard and guard.decision is PolicyDecision.BLOCK:
            # Attach an empty plan so callers always get a consistent shape.
            # The kind matters: this is a refusal, and an unlabelled one reads on the
            # dashboard like everything else that did nothing (19-B).
            response.plan = Plan(goal=request.text, intent="blocked", planner="guard",
                                 reply_kind="refusal")
            response.reply = (
                f"I will not do that automatically: {guard.reason}. "
                "If this is genuinely intended, do it deliberately and with explicit "
                "confirmation outside of me."
            )
            response.decision = PolicyDecision.BLOCK
            response.blocked_reason = guard.reason
            response.intent = "blocked"
            self.audit.record(
                intent=request.text,
                result="blocked",
                risk="blocked",
                device=request.device_id,
                session_id=request.session_id,
                request_id=response.request_id,
                detail={"reason": guard.reason, "reply_kind": "refusal"},
            )
            self._log_message(request.session_id, "assistant", response.reply, "blocked")
            response.duration_ms = int((time.perf_counter() - started) * 1000)
            return response

        # --- Stage 1: intent + planning -------------------------------------
        plan = self.planner.parse(request.text, request.context)
        response.intent = plan.intent
        response.plan = plan
        response.model_used = plan.planner

        if plan.direct_reply:
            response.reply = plan.direct_reply
            # ``decision`` is the permission outcome. Nothing was permitted to run
            # here -- the ledger row written just below says ``no_action`` -- so it
            # is left unset rather than claiming EXECUTE. It was EXECUTE for as
            # long as this branch existed, which made the answer contradict its own
            # audit record for every refusal, clarification and unrecognised goal.
            self._note_unhandled(response, plan)
            self.audit.record(
                intent=request.text,
                result="no_action",
                risk="none",
                device=request.device_id,
                session_id=request.session_id,
                request_id=response.request_id,
                detail={"stage": "planner", "reply": response.reply[:200],
                        # Which kind of non-action this was, so a refusal and a question
                        # stay distinguishable in the chain and not only on screen.
                        "reply_kind": plan.reply_kind,
                        "unhandled": list(plan.unhandled)},
            )
            self._log_message(request.session_id, "assistant", response.reply, plan.intent)
            response.duration_ms = int((time.perf_counter() - started) * 1000)
            return response

        if plan.needs_clarification or not plan.actions:
            response.reply = plan.clarification_question or (
                "I need a bit more detail before I act."
            )
            # needs_confirmation used to be set here, which advertised a question as a
            # pending approval: the dashboard would offer to confirm something with
            # nothing to execute, and the token would sit until it expired. A missing
            # parameter is not a decision waiting for consent. The branch was
            # unreachable from the rules planner until Phase 24 gave it a kind, so
            # nothing had ever shown that.
            self._note_unhandled(response, plan)
            self.audit.record(
                intent=request.text,
                result="clarification",
                risk="none",
                device=request.device_id,
                session_id=request.session_id,
                request_id=response.request_id,
                detail={"stage": "planner", "question": response.reply[:200],
                        "reply_kind": plan.reply_kind or "clarification",
                        "unhandled": list(plan.unhandled)},
            )
            self._log_message(request.session_id, "assistant", response.reply, plan.intent)
            response.duration_ms = int((time.perf_counter() - started) * 1000)
            return response

        # --- Stage 2+: permission -> execute -> verify ----------------------
        return await self._execute_plan(request, plan, response, started)

    async def _execute_plan(
        self,
        request: CommandRequest,
        plan: Plan,
        response: CommandResponse,
        started: float,
        job_id: Optional[str] = None,
        seqs: Optional[List[int]] = None,
    ) -> CommandResponse:
        """Run a plan through gate -> execute -> verify -> ledger -> job step.

        ``handle()`` and ``retry_job()`` share this on purpose: a re-run is not a
        privileged path. It gets the same permission verdict, mints its own
        confirmation for anything gated, and writes to the same chain. ``seqs``
        maps each action to the step it belongs to -- for a retry those are the
        failed steps' original positions, so the step that already succeeded is
        never written over by the one now taking its place.
        """
        ctx = self._context(request)
        if job_id is None and len(plan.actions) > 1:
            job_id = self.tasks.create(request.text, [a.tool for a in plan.actions],
                                       planned=list(plan.actions)).job_id
        if job_id:
            response.job_id = job_id
        step_seqs = list(seqs) if seqs is not None else list(range(len(plan.actions)))

        for index, action in enumerate(plan.actions):
            verdict = self.policy.evaluate(action, session_id=request.session_id)
            risk_label = self.classifier.risk_label(self.tools.get(action.tool).risk)  # type: ignore[union-attr]

            if verdict.decision is PolicyDecision.BLOCK:
                action.status = ActionStatus.BLOCKED
                response.decision = PolicyDecision.BLOCK
                response.blocked_reason = verdict.reason
                response.reply = f"I cannot do that: {verdict.reason.rstrip('.')}."
                self._audit_action(action, request, None, "blocked", False,
                                     request_id=response.request_id,
                                     unhandled=plan.unhandled,
                                     reason=verdict.reason)
                if job_id:
                    self.tasks.update_step(
                        job_id, action.tool, ActionStatus.BLOCKED, verdict.reason, seq=step_seqs[index]
                    )
                    self.tasks.refresh_status(job_id)
                self._note_unhandled(response, plan)
                self._log_message(request.session_id, "assistant", response.reply, plan.intent)
                response.duration_ms = int((time.perf_counter() - started) * 1000)
                return response

            if verdict.decision is PolicyDecision.CONFIRM:
                action.status = ActionStatus.AWAITING_CONFIRMATION
                response.decision = PolicyDecision.CONFIRM
                response.needs_confirmation = True
                response.confirmation_id = verdict.confirmation_id
                response.reply = self._confirmation_prompt(action, verdict.preview or "")
                response.reply += self._watch_confirmation_note(verdict.confirmation_id, action)
                if job_id:
                    # The step is waiting, and the steps behind it are not going
                    # to run until it is answered. Left unrecorded, the job sat at
                    # `running` for as long as the approval went unanswered --
                    # which, after it expired, was forever.
                    self.tasks.update_step(
                        job_id,
                        action.tool,
                        ActionStatus.AWAITING_CONFIRMATION,
                        "waiting for your approval",
                        seq=step_seqs[index],
                    )
                    self.tasks.attach_confirmation(
                        job_id, step_seqs[index], verdict.confirmation_id
                    )
                    self.tasks.refresh_status(job_id)
                self.audit.record(
                    intent=request.text,
                    tool=action.tool,
                    target=json.dumps(action.params, default=str),
                    risk=risk_label,
                    confirmation=True,
                    result="awaiting_confirmation",
                    device=action.device,
                    session_id=request.session_id,
                    request_id=response.request_id,
                    detail={"confirmation_id": verdict.confirmation_id,
                            "unhandled": list(plan.unhandled)},
                )
                self._note_unhandled(response, plan)
                self._log_message(request.session_id, "assistant", response.reply, plan.intent)
                response.duration_ms = int((time.perf_counter() - started) * 1000)
                return response

            # EXECUTE
            action.status = ActionStatus.RUNNING
            agent = find_agent(self.agents, action.tool)
            if agent is None:
                action.status = ActionStatus.FAILED
                response.reply = f"No agent can handle {action.tool!r}."
                if job_id:
                    self.tasks.update_step(
                        job_id, action.tool, ActionStatus.FAILED, "no agent owns this tool",
                        seq=step_seqs[index],
                    )
                    self.tasks.refresh_status(job_id)
                self._note_unhandled(response, plan)
                self._log_message(request.session_id, "assistant", response.reply, plan.intent)
                response.duration_ms = int((time.perf_counter() - started) * 1000)
                return response

            result = await agent.execute(action, ctx)

            # --- Stage: recovery (section 47) ------------------------------
            # A failed action is not the end of the plan. Try to recover, but
            # never silently -- the outcome is always reported.
            answered_by = ""
            if not result.ok:
                outcome = await self.recovery.recover(
                    action,
                    ctx,
                    agent.execute,
                    self.tools.get(action.tool),
                    result,
                )
                if outcome.recovered and outcome.result is not None:
                    result = outcome.result
                    if outcome.result.tool != action.tool:
                        answered_by = outcome.result.tool
                    response.recovery_notes.append(
                        f"{action.tool}: {outcome.note}"
                    )
                    self.audit.record(
                        intent=request.text,
                        tool=action.tool,
                        target=json.dumps(action.params, default=str),
                        risk="recovered",
                        result="recovered",
                        device=action.device,
                        session_id=request.session_id,
                        detail=outcome.as_dict(),
                    )

            action.result = result
            action.status = self._action_status(result)
            response.actions.append(result)
            self._audit_action(action, request, result, risk_label, False,
                                 request_id=response.request_id,
                                 unhandled=plan.unhandled,
                                 answered_by=answered_by)
            if job_id:
                self.tasks.update_step(
                    job_id,
                    action.tool,
                    self._action_status(result),
                    result.summary,
                    seq=step_seqs[index],
                )

        if job_id:
            # A step that never ran stays `pending`, and `pending` aggregates to
            # `incomplete` -- so a sequence that halted for an approval can never
            # be reported as finished work.
            self.tasks.refresh_status(job_id)

        # --- Stage: memory update -------------------------------------------
        self._remember_outcome(request, response)

        # --- Stage: response -------------------------------------------------
        response.reply = self._compose_reply(request.text, response)
        response.decision = response.decision or PolicyDecision.EXECUTE
        self._note_unhandled(response, plan)
        self._log_message(request.session_id, "assistant", response.reply, plan.intent)
        response.duration_ms = int((time.perf_counter() - started) * 1000)
        return response

    # -------------------------------------------------------------- re-run
    async def retry_job(self, job_id: str) -> JobRetry:
        """Re-issue a job's failed steps, through the same gates as the first run.

        Plan §31 lists ``retry`` as a task-manager capability, and Phase 26
        measured that this build had no re-dispatcher at all: a step row kept its
        tool's name and none of its parameters, so nothing could be re-issued
        honestly. The three rules this method exists to keep:

        * only the steps that actually failed are re-run -- a step whose effect
          was verified is never replayed;
        * each re-run action goes back through ``PermissionEngine``, so anything
          gated mints a **fresh** confirmation rather than spending the old one;
        * a step whose recorded parameters are missing (a job written before the
          ``params`` column existed) or whose tool is gone is refused. Filling in
          a plausible parameter for ``delete_file`` would be the old bug wearing
          a better face.
        """
        report = self.tasks.retry(job_id)
        if not report.accepted:
            # Unknown or cancelled: the recorder already knows why, and nothing
            # may be dispatched for a job the user cancelled.
            return JobRetry(job_id, report)
        # Read the steps after the recorder annotated them, so the trail this
        # method writes back carries the "re-attempt requested" mark with it.
        job = self.tasks.get(job_id)
        failed = [s for s in job["steps"] if s["status"] == ActionStatus.FAILED.value]
        if not failed:
            return JobRetry(job_id, report)

        gone = [s["name"] for s in failed if not self.tools.has(s["name"])]
        if gone:
            return JobRetry(job_id, LifecycleReport(
                job_id, "retry", False, status=report.status,
                reason=f"{', '.join(sorted(set(gone)))} is no longer a registered tool",
                message="Nothing ran: those steps name a tool this build no longer has, "
                        "so there is nothing to re-issue."))

        actions: List[PlannedAction] = []
        seqs: List[int] = []
        prior: Dict[int, str] = {}
        for step in failed:
            actions.append(PlannedAction(
                step_id=step["seq"] + 1,
                tool=step["name"],
                params=json.loads(step["params"] or "{}"),
                device=step["device"] or "laptop_main",
                reason="re-attempt of a failed step",
            ))
            seqs.append(step["seq"])
            prior[step["seq"]] = step["detail"]

        request = CommandRequest(
            text=job["goal"], source="retry", device_id=actions[0].device,
            session_id=f"retry:{job_id}")
        plan = Plan(goal=job["goal"], intent="retry", actions=actions, planner="retry")
        response = CommandResponse(session_id=request.session_id)
        response.intent = plan.intent
        response.plan = plan
        response.model_used = plan.planner
        started = time.perf_counter()
        await self._execute_plan(request, plan, response, started,
                                 job_id=job_id, seqs=seqs)
        # The attempt wrote its own summary over the step; put the failure that
        # prompted it back in front of the new one, so the record shows both.
        for seq in seqs:
            self.tasks.restore_attempt_trail(job_id, seq, prior[seq])
        # The status the caller hears is the one the steps now support, not the
        # one they had when the ask was recorded -- reading it from the
        # aggregation rather than carrying ``report.status`` forward is what
        # keeps a successful re-run from being reported as still failed.
        report.status = self.tasks.refresh_status(job_id)
        # The recorder's sentence describes the ask, not the outcome; once steps
        # have actually run the caller needs to hear that instead.
        ran = len(response.actions)
        awaiting = response.confirmation_id if response.needs_confirmation else ""
        report.executed = ran
        report.message = (
            f"{ran} step(s) re-ran." if ran and not awaiting else
            (f"{ran} step(s) re-ran; 1 is waiting for a fresh approval."
             if ran else
             "Nothing ran: the re-attempt needs your approval before it executes.")
        )
        return JobRetry(job_id, report, response)

    # --------------------------------------------------------- confirmation
    async def resolve_confirmation(
        self, confirmation_id: str, approve: bool, session_id: str = ""
    ) -> CommandResponse:
        """Execute or discard a previously pending action."""
        started = time.perf_counter()
        response = CommandResponse(session_id=session_id)
        record = self.policy.get_confirmation(confirmation_id)

        if record is None:
            response.reply = "I have no record of that confirmation request."
            return response

        if record["state"] != "pending":
            if record["state"] == "expired":
                response.reply = (
                    "That approval expired before it was answered, so I did nothing. "
                    "Ask me again if you still want it."
                )
            else:
                response.reply = f"That request was already {record['state']}."
            response.needs_confirmation = False
            response.decision = PolicyDecision.BLOCK
            return response

        self.policy.resolve(confirmation_id, approve)

        # resolve() may have expired the request rather than honouring it.
        resolved = self.policy.get_confirmation(confirmation_id)
        if resolved and resolved["state"] == "expired":
            response.reply = (
                "That approval expired before it was answered, so I did nothing. "
                "Ask me again if you still want it."
            )
            response.needs_confirmation = False
            response.decision = PolicyDecision.BLOCK
            self._settle_step(confirmation_id, ActionStatus.BLOCKED, "approval expired", response)
            self.audit.record(
                intent=record["tool"],
                tool=record["tool"],
                result="expired",
                risk="expired",
                confirmation=True,
                session_id=session_id,
                request_id=response.request_id,
            )
            return response

        if not approve:
            response.reply = "Understood - cancelled. Nothing was changed."
            response.decision = PolicyDecision.BLOCK
            self._settle_step(confirmation_id, ActionStatus.BLOCKED, "you declined", response)
            self.audit.record(
                intent=record["tool"],
                tool=record["tool"],
                result="denied",
                risk="denied",
                confirmation=True,
                session_id=session_id,
                request_id=response.request_id,
            )
            return response

        action = PlannedAction(
            step_id=1,
            tool=record["tool"],
            params=record["params"],
            device=record["device"],
            reason="confirmed by user",
        )

        # The approval answers "do you want this?", not "is this still
        # allowed?". Conditions can change while the prompt is unanswered, so
        # re-run the refusal gates against what is true right now.
        refusal = self.policy.pre_execute_check(action)
        if refusal:
            response.reply = (
                f"That is no longer allowed, so I did nothing: {refusal}"
            )
            response.decision = PolicyDecision.BLOCK
            response.needs_confirmation = False
            self._settle_step(confirmation_id, ActionStatus.BLOCKED, refusal, response)
            self.audit.record(
                intent=record["tool"],
                tool=record["tool"],
                target=json.dumps(record["params"], default=str),
                risk="blocked",
                confirmation=True,
                result="blocked",
                device=action.device,
                session_id=session_id,
                request_id=response.request_id,
                detail={"reason": refusal, "stage": "pre_execute_check"},
            )
            self._log_message(session_id, "assistant", response.reply, "confirmation")
            response.duration_ms = int((time.perf_counter() - started) * 1000)
            return response

        ctx = ToolContext(
            device_id=action.device,
            session_id=session_id,
            settings=self.settings,
            apps=self.apps,
            devices=self.devices,
            memory=self.memory,
            graph=self.graph,
            core=self,
        )
        agent = find_agent(self.agents, action.tool)
        if agent is None:
            response.reply = f"No agent can handle {action.tool!r}."
            self._settle_step(confirmation_id, ActionStatus.FAILED, "no agent owns this tool", response)
            return response

        result = await agent.execute(action, ctx)
        if not result.ok:
            # R-12: every gated tool ran outside the failure ledger. The
            # confirmation path never called ``recover()`` -- and it must not
            # start doing so silently, because one approval buys one attempt and
            # an automatic retry of a destructive action is the thing the retry
            # policy exists to refuse. What it must not do is fail *invisibly*:
            # measured, ``failures`` went 1 -> 1 for a failed approved delete, so
            # /metrics, the briefing and `explain_failures` described only the
            # ungated half of the system.
            self.recovery.record_failure(
                action,
                result,
                RecoveryOutcome(
                    strategy="none",
                    attempts=0,
                    note=(
                        f"{result.summary} — not retried: it ran under a "
                        "confirmation, and one approval authorises one attempt"
                    ),
                    answered_for=action.tool,
                ),
            )
        response.actions.append(result)
        response.decision = PolicyDecision.EXECUTE
        response.reply = result.summary if result.ok else f"That failed: {result.summary}"
        self.audit.record(
            intent=record["tool"],
            tool=record["tool"],
            target=json.dumps(record["params"], default=str),
            risk="confirmed",
            confirmation=True,
            # From verification, not `.ok` -- this is the second audit site and
            # Phase 10 P0-1 fixed only the first one, so every confirmed action
            # still logged `success` for a result the handler had honestly
            # downgraded to UNKNOWN.
            result=self._audit_token(result),
            device=action.device,
            session_id=session_id,
            request_id=response.request_id,
            detail={"ok": result.ok, "verification": result.verification.value},
        )
        unfinished = self._settle_step(confirmation_id, self._action_status(result), result.summary, response)
        if unfinished:
            # One approval buys one action. The steps behind it were never
            # offered to the user, so continuing would be JARVIS granting itself
            # authority it was never given.
            response.reply += (
                f"\n  {unfinished} later step(s) in that sequence have not run; "
                "ask me to continue when you want them."
            )
        self._log_message(session_id, "assistant", response.reply, "confirmation")
        response.duration_ms = int((time.perf_counter() - started) * 1000)
        return response

    # ------------------------------------------------------------- helpers
    def _confirmation_prompt(self, action: PlannedAction, preview: str) -> str:
        params = ", ".join(f"{k}={v}" for k, v in action.params.items()) or "no parameters"
        return (
            f"This needs your confirmation before I run it.\n"
            f"  Action: {action.tool}\n"
            f"  Device: {action.device}\n"
            f"  Details: {params}\n"
            f"Reply with /confirm {action.tool} to approve, or /cancel to stop."
        )

    def _watch_confirmation_note(self, confirmation_id: str, action: PlannedAction) -> str:
        """Honour ``confirm_channel = watch`` (plan §25) by putting the prompt on the wrist.

        The setting existed for a long time with nothing behind it, which is
        worse than not having it: it read as if approvals could already come from
        the watch. Now it genuinely routes there, and says how far the prompt got.
        An unpaired watch, or no bridge, still yields an honest "queued, not
        delivered" -- never a claim that something appeared on a screen.
        """
        if (self.settings.confirm_channel or "").strip().lower() != "watch":
            return ""
        from ..watch.gateway import get_watch_gateway

        gw = get_watch_gateway()
        if not gw.paired:
            return (
                "\n  Watch channel: your watch is not paired, so this prompt stayed here. "
                "Confirm above or on the dashboard."
            )
        item = gw.queue(
            "confirmation",
            f"Confirm {action.tool}?",
            body=action.tool,
            confirmation_id=confirmation_id,
        )
        if item.state == "queued":
            return "\n  Watch channel: queued for your watch; nothing has reached it yet."
        if not gw.delivery_is_real(item):
            # A simulated companion reports a confirmed delivery, which is a fact
            # about the record, not evidence that anyone is looking at a screen.
            # Claiming the prompt is answerable "there" would also promise an
            # interaction the gateway now refuses.
            return (
                "\n  Watch channel: handed to the simulated watch companion, not to a "
                "real watch, so nothing can be answered there. Confirm above or on the "
                "dashboard."
            )
        if item.state == "displayed":
            return "\n  Watch channel: shown on your watch -- answer CONFIRM or CANCEL there."
        return "\n  Watch channel: handed to your watch, not yet shown."

    #: Tools whose only effect is reading memory back. Echoing them stores a new row
    #: quoting the row just read, so checking whether a deletion worked writes another
    #: copy of the fact that was just forgotten -- and the next check writes one more.
    _MEMORY_READ_TOOLS = {"search_memory", "memory_overview"}

    def _remember_outcome(self, request: CommandRequest, response: CommandResponse) -> None:
        """Record a session-scoped memory of what happened."""
        if not response.actions:
            return
        if all(a.tool in self._MEMORY_READ_TOOLS for a in response.actions):
            return
        summary = "; ".join(f"{a.tool}: {a.summary}" for a in response.actions)
        self.memory.remember(
            f"User asked: {request.text}. Result: {summary}",
            scope=MemoryScope.SESSION,
            source="orchestrator",
        )

    def _compose_reply(self, text: str, response: CommandResponse) -> str:
        """Turn action results into a natural reply, honest about failures."""
        if not response.actions:
            return "Done."

        lines: List[str] = []
        for result in response.actions:
            if result.ok:
                lines.append(result.message or f"{result.tool} completed.")
            else:
                lines.append(f"That did not work: {result.summary}")

        if len(lines) == 1:
            reply = lines[0]
        else:
            reply = "\n".join(f"{i + 1}. {line}" for i, line in enumerate(lines))

        # If something failed first and was recovered, say so. Reporting only the
        # success would hide that the system is running degraded.
        if response.recovery_notes:
            reply += "\n\n(Recovered automatically — " + "; ".join(response.recovery_notes) + ")"
        return reply
