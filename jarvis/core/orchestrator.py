"""The orchestrator (sections 4.1, 9, 34, 47, 60).

Implements the full loop:

    Intent -> Plan -> Permission -> Tool -> Execution -> Verification
           -> Memory -> Response

It is the only place that sequences those stages. Agents execute; the policy
engine authorises; the orchestrator coordinates and reports.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..agents.base import Agent, build_agents, find_agent
from ..config import get_settings
from ..db.database import get_db
from ..gateway.router import ModelRouter
from ..gateway.providers import ProviderUnknown, select_provider
from ..logging_setup import get_logger
from ..memory.graph import ProjectGraph
from ..memory.store import MemoryStore
from ..net import request_budget
from ..registry.apps import AppRegistry
from ..registry.devices import get_device_registry
from ..registry.tools import ToolRegistry
from ..security.audit import AuditLog
from ..security.policy import PermissionEngine
from .models import RiskLevel
from ..security.providers import CapabilityRegistry, CapabilityType
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
from .conversation import ConversationService
from .missions import MissionService, MissionState
from .model_planner import ModelPlanner
from .planner import RulePlanner
from .recovery import RecoveryEngine, RecoveryOutcome
from .tasks import LifecycleReport, TaskManager

#: Ceiling on the network time one typed sentence may consume, across every read it
#: triggers. Wider than any single call's own budget on purpose -- a 60s vision read is
#: allowed to use the whole thing -- but it is the number that is true about the wait.
SENTENCE_BUDGET = 60.0

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
        # Populate the Control Tower from the agents just built, so a pause set
        # through the API gates the same worker the pipeline dispatches to. Read
        # from the registry's own `agent` field rather than a second mapping.
        from .control_tower import ControlTower

        self.control_tower = ControlTower.get()
        self.control_tower.register_from_registry(self.tools)
        self.planner = RulePlanner(self.tools, self.apps)
        # R2.3: the model may *propose* an action the rules could not route. The
        # proposal is validated here and then runs through the same permission
        # gate as any rule-planned action -- a model is not a grant.
        self.model_planner = ModelPlanner(self.tools)
        # R2.2: the one place a conversation is read and written. The pipeline
        # and the chat surfaces share it, so a follow-up resolves the same way
        # on either.
        self.conversations = ConversationService(self.db)
        # The mission ledger. A command that actually executes tools records
        # itself here, so the work a person asked for in the Command Center is
        # visible in Mission Control instead of vanishing into a reply string.
        self.missions = MissionService(self.db)
        # The message a conversation has just been about, per session. A
        # demonstrative follow-up ("read that one", "summarize the second one")
        # resolves against this instead of a fresh search of nothing.
        from .email_context import EmailContext

        self.email_context = EmailContext(self.db)
        # The user's own task list and the daily plan derived from it. Distinct
        # from TaskManager (workflow runs): this is what the *person* wrote down.
        from .planner_service import PlannerService

        self.planner_service = PlannerService(self.db)
        # V2 (flagged): what each session has actually *verified* an action
        # against, so a follow-up "close it" can name the application the last
        # mission observed instead of the planner refusing the pronoun. In
        # process only -- never persisted -- so turning the flag off leaves no
        # state behind and no migration to undo. Recording an entity is not a
        # grant: the resolved sentence still runs through the same gate.
        from ..voice.entities import EntityMemory

        self.entity_memory = EntityMemory()
        self.router = ModelRouter()
        self.capability_registry = CapabilityRegistry()
        
        log.info(
            "jarvis core initialised",
            extra_fields={
                "tools": len(self.tools.names()),
                "agents": list(self.agents.keys()),
                "llm": self.settings.llm_enabled,
            },
        )
    
    def check_ai_capability(self, provider_id: str, required_capabilities: List[CapabilityType]) -> bool:
        """Check if AI provider supports required capabilities for multimodal requests."""
        return self.capability_registry.can_handle_request(provider_id, "", required_capabilities)

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

    def _settle_mission(self, mission_id: str, *, outcome: str,
                        reason: str = "") -> None:
        """Move a command's mission to a final state.

        Every exit from the action loop has to call this. Without it a mission
        stays `running` forever after its command finished -- which is exactly
        what Mission Control showed: three commands that had completed, listed
        as work still in progress. `running` means "something is executing
        right now", and nothing was.

        `outcome` is one of the mission service's transitions from RUNNING
        (partially_completed / failed / blocked). A failure to settle is logged
        and never raised: the person's answer matters more than the ledger.
        """
        if not mission_id:
            return
        from .missions import MissionState

        target = {
            "completed": MissionState.COMPLETED,
            "partially_completed": MissionState.PARTIALLY_COMPLETED,
            "failed": MissionState.FAILED,
            "blocked": MissionState.BLOCKED,
            "cancelled": MissionState.CANCELLED,
        }.get(outcome)
        if target is None:
            log.info(f"unknown mission outcome {outcome!r}; leaving the mission as is")
            return
        try:
            mission = self.missions.get(mission_id)
            if mission is None:
                return
            if mission.state in ("completed", "failed", "cancelled",
                                 "partially_completed"):
                return
            self.missions.transition(mission_id, target, actor="pipeline",
                                     reason=reason[:300])
        except Exception as exc:
            log.info(f"could not settle mission {mission_id}: "
                     f"{type(exc).__name__}: {str(exc)[:160]}")

    def _log_message(self, session_id: str, role: str, content: str, intent: str = "") -> None:
        # R2.2: the conversation service is the one place a turn is written, so
        # the pipeline and the chat surfaces share one history model. The
        # `messages` table stays the pipeline's own ledger -- it carries the
        # intent and the session index -- and the service writes it.
        #
        # R2.2b: the session is also registered in `ai_chats`, keyed by its own
        # id. Without that row the conversation exists on the server but no
        # listing route can name it, so the app's history was invisible to the
        # dashboard and to every client that lists conversations. The chat row
        # is what makes the session addressable; the turn itself stays in the
        # pipeline's ledger, which is authoritative for these turns.
        self.conversations.append(role, content, session_id=session_id, intent=intent)
        self.conversations.register_session(session_id, opening_text=content if role == "user" else "")
        self.db.execute(
            "INSERT OR IGNORE INTO sessions (session_id, user_id) VALUES (?, 'user')",
            (session_id,),
        )

    #: How many recent turns a multi-turn follow-up may see. Bounded so a long
    #: session never inflates the prompt toward the context window.
    CONVERSATION_HISTORY_LIMIT = 8

    def _recent_history(self, session_id: str, current_text: str) -> List[Dict[str, str]]:
        """The session's recent ``{"role","content"}`` turns, oldest first.

        Excludes the in-flight user message (this call's own text) so it is not fed
        twice. Returns ``[]`` when there is no session id or the table is empty —
        single-turn behaviour then applies unchanged.

        R2.2: the read goes through the shared conversation service, so this
        surface and the chat surface see the same turns. The bound used to be
        this class's own ``CONVERSATION_HISTORY_LIMIT``; it is now the
        service's, so one limit governs both.
        """
        return self.conversations.history(
            session_id=session_id,
            current_text=current_text,
            limit=self.CONVERSATION_HISTORY_LIMIT,
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

    #: How many candidates a question is allowed to name. Bounded because a list of
    #: forty is a wall of text that answers nothing, and the reply is also what gets
    #: remembered -- the same reason ``MAX_READ_EXCERPT`` exists for file reads.
    IDENTIFIER_OFFER_LIMIT = 4

    async def _advise_from_model(self, request: CommandRequest, rules_reply: str,
                                  conversational: bool = False):
        """Ask the configured model about a sentence the rules did not recognise.

        The model may speak and may name one thing it would suggest. It cannot plan,
        cannot execute, and cannot raise a confirmation card -- those stay behind the gate
        a typed sentence goes through, and a suggestion is only ever reported as one.

        When ``conversational`` is true the model's answer stands on its own rather than
        being appended to the rule router's sentence: smalltalk greetings, open questions,
        and clarifications are natural conversation, not pipeline failures to annotate.
        """
        model = self.settings.llm_model or "the configured model"
        # Named because "the model answered" is not the same fact as "gemini answered", and
        # since Phase 60 more than one vendor can be the one standing behind that word.
        try:
            # Resolved from the settings rather than from `self.router.provider`, so that a
            # double standing where the router stands (Phase 42's tests) does not read as an
            # unusable configuration. The name is only a label; the call still goes through
            # the router.
            provider = select_provider(self.settings).name
            refusal = ""
        except ProviderUnknown as exc:  # an unusable provider name is its own answer, told below
            provider, refusal = "", str(exc)
        label = f"{provider}:{model}" if provider else model
        note: Dict[str, Any] = {"model": model, "provider": provider, "label": label}
        if refusal:
            # Nothing was sent anywhere: the name could not be turned into a vendor. Saying
            # "I asked the model and it was unavailable" would describe a call that never
            # happened, and `model_used` would name a model that never answered.
            note["stage"] = "provider_refused"
            note["error"] = refusal[:200]
            return (f"{rules_reply}\n\nI did not ask a model about that: {refusal}. So that "
                    "answer is the rule router's own.", note)
        system = (
            "You are Vantage, an AI assistant running inside a macOS desktop app. "
            "Answer naturally and conversationally. Keep responses concise but helpful. "
            "You can discuss any topic — the user wants a ChatGPT-like conversational experience. "
            "If the user asks you to perform an action (check files, run commands, control "
            "devices), name the tool you would use and explain what you'd do — the app handles "
            "execution. Be warm and direct, not robotic."
        ) if conversational else (
            "You are advising a local assistant on the user's own Mac. Its rule router did "
            "not recognise the sentence, so answer in at most two short sentences of plain "
            "words. You have no ability to run anything and must never say that a thing was "
            "done, opened, deleted or sent. If the sentence looks like a request the "
            "assistant could carry out, name at most one tool from this list that you would "
            "suggest, and say the user has to ask for it themselves. If you cannot tell what "
            "was meant, say so and ask one question.\nTools: "
            + ", ".join(sorted(self.tools.names()))
        )
        try:
            # 30s — the same documented chat budget as `/ai/chat/send` and the
            # provider Test route. This used to be 20s, which fired before a slow
            # but legitimate cold-route completion (measured 20.1s on 9Router) and
            # silently dropped to the rule planner. The provider adapter's total
            # deadline now ends the call only when the model truly stalls; the
            # sentence budget (SENTENCE_BUDGET) still caps the turn as a whole.
            #
            # Multi-turn context: thread the session's recent turns so a follow-up
            # ("and the second one?") resolves against what was just said. Bounded
            # to the last few messages and scoped to this session, so a different
            # session or user never leaks another's conversation.
            history = self._recent_history(request.session_id, request.text)
            answer = await self.router.complete(system, request.text,
                                                task_type="conversation", timeout=30.0,
                                                history=history)
        except Exception as exc:  # a transport failure is itself an answer about transport
            answer = None
            note["error"] = str(exc)[:120]

        if not answer:
            note["stage"] = "model_unavailable"
            if conversational:
                return (f"{rules_reply}\n\n(The AI model is currently unavailable — "
                        "check your network or provider configuration.)", note)
            return (f"{rules_reply}\n\nI also asked the model you configured ({label}), and "
                    "it was unavailable, so that answer is the rule router's own.", note)

        text = " ".join(str(answer).split())[:600]
        suggested = next((name for name in sorted(self.tools.names())
                          if re.search(rf"\b{re.escape(name)}\b", text)), "")
        note["stage"] = "model"
        if conversational:
            # The model's answer stands on its own — natural conversation, not
            # a pipeline annotation. But if the model suggested running a tool
            # in its response, the user must know it was NOT executed.
            if suggested:
                return (
                    f"{text}\n\n(I did not run {suggested}. Ask me to do it "
                    f"directly and it will go through the usual approval.)",
                    note,
                )
            return text, note

        if suggested:
            note["suggested_tool"] = suggested
            closer = (f"It suggested {suggested}. I did not run anything on that -- ask me "
                      "for it yourself and it goes through the usual approval.")
        else:
            closer = "I did not run anything on its answer."
        return f"{rules_reply}\n\n{label} said: {text}\n\n{closer}", note

    @staticmethod
    def _request_surface(request: CommandRequest) -> str:
        """Which surface raised this request.

        The source is the surface the request came from -- "mobile" for the
        phone, "native" for the desktop app, "dashboard" for the web. A
        request that arrived through the companion gateway is a phone request
        however it was written, and the gateway is what forwards it.
        """
        source = (request.source or "").strip().lower()
        if source in ("mobile", "phone", "android"):
            return "phone"
        if source in ("native", "macos", "app"):
            return "native"
        if source in ("dashboard", "web", "browser"):
            return "dashboard"
        return source or "unknown"

    async def _propose_action(self, request: CommandRequest) -> Optional[Plan]:
        """Ask the configured model to propose an action for this sentence.

        Returns the validated plan, or ``None`` when the model had nothing to
        propose, was unavailable, or proposed something this build will not
        originate. A refusal is reported to the user by the caller; here it is
        only a reason not to act.

        The model's own answer to the sentence is not repeated here: the
        conversational reply has already been given, and a proposal that adds
        "I could also run X" on top of an answer is noise.
        """
        try:
            history = self._recent_history(request.session_id, request.text)
            return await self.model_planner.propose(
                request.text,
                complete_json=self.router.complete_json,
                history=history,
            )
        except Exception as exc:  # a planner failure must not take the turn down
            log.info("model proposal failed", extra_fields={"error": str(exc)[:120]})
            return None

    async def propose_and_execute(self, request: CommandRequest) -> CommandResponse:
        """Ask the model to propose an action for this sentence, and run it.

        R2.2: this is how the chat surface gains the ability to *do* something
        rather than only answer. The proposal is validated by the model
        planner, then goes through exactly the same gate -> execute -> verify
        path as a pipeline action, so a chat proposal is never a grant and a
        risky one still waits for the person's approval.

        The response is the ordinary :class:`CommandResponse`, so the chat
        surface reads the same fields the pipeline surfaces read: what was
        proposed, whether it ran, whether it is waiting for approval, and what
        the verified result was.
        """
        started = time.perf_counter()
        response = CommandResponse(session_id=request.session_id,
                                   request_id=new_id("REQ"))

        # A demonstrative ("read that one") is not something to ask the model
        # to guess: the per-session selection is a local fact the pipeline
        # holds, and resolving it here keeps this surface on the same
        # behaviour as /command. The model still proposes everything else.
        #
        # V2 (flagged): a pronoun may also refer to what the last mission
        # actually verified. The rewrite happens *before* planning, so the
        # resolved sentence takes the ordinary path through the same ToolSpec
        # validation and permission gate. Nothing is authorized here; when the
        # memory has no verified entity (or more than one could fit) the text
        # is passed through untouched and the planner refuses the pronoun
        # exactly as it always has.
        request_text = request.text
        if getattr(self.settings, "voice_operator_v2_enabled", False):
            from ..voice.resolve import resolve_reference

            resolved = resolve_reference(request_text, self.entity_memory,
                                         request.session_id, kind="app")
            if resolved:
                log.info("voice operator v2: resolved a pronoun reference")
                request_text = resolved
        plan = self.planner.parse(request_text, request.context)
        if plan.referent is not None:
            self._resolve_email_referent(request, plan)
            if plan.direct_reply:
                response.reply = plan.direct_reply
                response.intent = plan.intent
                response.plan = plan
                self._log_message(request.session_id, "user", request.text)
                self._log_message(request.session_id, "assistant", plan.direct_reply,
                                  plan.intent)
                return response
            if plan.actions:
                proposal = plan
            else:
                proposal = None
        else:
            proposal = None

        if proposal is None or not proposal.actions:
            proposal = await self._propose_action(request)
        if proposal is None or not proposal.actions:
            return response

        response.plan = proposal
        response.intent = proposal.intent
        response.model_used = "llm"
        planned = proposal.actions[0]
        response.reply = (
            f"I can {planned.tool.replace('_', ' ')} — let me do that."
        )
        self._log_message(request.session_id, "user", request.text)
        return await self._execute_plan(request, proposal, response, started)

    #: Ordinal words a person uses to pick from a list, by position.
    _REFERENT_ORDINALS = {
        "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
        "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
    }

    def _resolve_email_referent(self, request: CommandRequest, plan: Plan) -> None:
        """Turn a demonstrative into a read of the message it points at.

        The planner saw "that one" but has no memory of the conversation; the
        selection does. Two shapes: a bare referent reads the message the
        session selected; an ordinal ("the second one") picks from the list of
        candidates the reply just showed, in the order it showed them.

        The rules that keep an answer honest:

        * the lookup is by the session's own row -- a fresh session has none,
          so a brand-new conversation cannot inherit another one's message,
          and neither can a session that never dealt with an email;
        * a referent with no selection behind it is a clarification naming
          what is missing, never a search of the whole inbox and never a read
          of a message the person did not point at;
        * a bare referent whose last search matched several messages is asked
          which one, with that list -- the same rule as a named search that
          matched several: several matches identify none of them on their own;
        * an ordinal that runs past the candidate list is reported as such --
          "that list had three messages" -- rather than silently wrapping or
          picking the first;
        * a selection recorded against one provider is not spent against
          another: a uid is only meaningful in the mailbox that minted it;
        * the read is rebuilt with the registry's own tool, risk and
          confirmation (``read_email`` is READ-tier, so it runs through the
          normal gate), which is what keeps the result verified and audited
          like any other action.
        """
        if plan.referent is None:
            return

        def _clarify(message: str) -> None:
            plan.direct_reply = message
            plan.actions = []
            plan.reply_kind = "clarification"
            plan.needs_clarification = False
            plan.referent = None

        selection = self.email_context.get(request.session_id)
        if selection is None or (not selection.uid and not selection.candidates):
            _clarify(
                "There is no email in this conversation for me to point at. "
                'Find one first -- for example: "find my email about the report".'
            )
            return

        provider = self._current_email_provider_name()
        if selection.provider and provider and selection.provider != provider:
            _clarify(
                f"That selection was made against the {selection.provider} mailbox, "
                f"and the email backend is now {provider}. I will not read one "
                "mailbox's message id against another; find it again here."
            )
            return

        # Which message the referent names. The shared outcome is the row the
        # selection holds for it -- a uid plus whatever display labels were
        # recorded with it -- and the finish depends on what the person asked
        # for: a read wants the uid, a reply wants the sender's address.
        row = self._referent_row(plan, selection, plan.referent)
        if row is None:
            return  # the clarifying reply was already set

        if plan.intent == "draft_email":
            self._finish_reply_draft(plan, row)
        elif plan.intent == "task_from_email":
            self._finish_task_from_email(plan, row)
        else:
            self._finish_referent_read(plan, row)

    def _clarify_referent(self, plan: Plan, message: str) -> None:
        """Answer a referent that cannot be resolved, honestly, and run nothing."""
        plan.direct_reply = message
        plan.actions = []
        plan.reply_kind = "clarification"
        plan.needs_clarification = False
        plan.referent = None

    def _clear_referent_clarification(self, plan: Plan) -> None:
        plan.direct_reply = None
        plan.clarification_question = None
        plan.needs_clarification = False
        plan.reply_kind = ""
        plan.referent = None

    def _referent_row(self, plan: Plan, selection, referent: str):
        """The selection row a referent names, or ``None`` after clarifying.

        Shared by the read and the reply finish, so both resolve "the second
        one" and "that one" against the same list, in the same order, and
        both refuse out-of-range and missing references the same way.
        """
        if referent == "":
            if selection.uid:
                return {"uid": selection.uid,
                        "subject": selection.subject,
                        "sender": selection.sender}
            if not selection.candidates:
                self._clarify_referent(
                    plan,
                    "I do not have a message to point that at. Find one first, or "
                    "name it: for example, \"read the invoice email\".")
                return None
            shown = "; ".join(
                f"{i + 1}. {c.get('subject') or '(no subject)'} from "
                f"{c.get('from') or 'unknown sender'}"
                for i, c in enumerate(selection.candidates[:5])
            )
            more = (f" (+{len(selection.candidates) - 5} more)"
                    if len(selection.candidates) > 5 else "")
            self._clarify_referent(
                plan,
                "That search matched more than one message, so I did not pick "
                f"on my own. Here is what I have: {shown}{more}. Tell me the "
                "number, or say it by name.")
            return None

        index = self._REFERENT_ORDINALS.get(referent)
        if index is None:
            try:
                index = int(referent)
            except ValueError:
                index = None
        if index is None:
            self._clarify_referent(
                plan,
                "I could not work out which message you meant. Say it by "
                "name or by number from the list I showed you.")
            return None
        if not selection.candidates:
            self._clarify_referent(
                plan,
                "I no longer have the list of messages in hand, so I cannot "
                f"pick the {referent} one from it. Search again -- for "
                'example: "find my email about that" -- and I will number them.')
            return None
        if not 1 <= index <= len(selection.candidates):
            self._clarify_referent(
                plan,
                f"That list had {len(selection.candidates)} message(s); there is "
                f"no {referent} one to read.")
            return None
        candidate = selection.candidates[index - 1]
        return {"uid": str(candidate.get("uid") or ""),
                "subject": str(candidate.get("subject") or ""),
                "sender": str(candidate.get("from") or "")}

    def _finish_referent_read(self, plan: Plan, row: Dict[str, str]) -> None:
        """A referent that asks to see the message: read it by its real uid."""
        uid = row["uid"]
        if not uid:
            self._clarify_referent(
                plan,
                "I do not have a message to point that at. Find one first, or "
                "name it: for example, \"read the invoice email\".")
            return
        # Rebuild the plan as a read of the real uid. The tool, its risk tier
        # and its confirmation come from the registry, so the read goes
        # through the same gate and leaves the same audit trail as one the
        # rules planned directly.
        spec = self.tools.get("read_email")
        problems = self.tools.validate_params("read_email", {"uid": uid})
        if spec is None or problems:
            self._clarify_referent(plan, "I could not look that message up; try naming it.")
            return
        plan.actions = [
            PlannedAction(
                step_id=1,
                tool="read_email",
                params={"uid": uid},
                reason="the person referred to the message the conversation "
                       "selected",
            )
        ]
        plan.intent = "read_email"
        self._clear_referent_clarification(plan)
        plan.identifier_sources_used.append("email_selection")

    def _finish_task_from_email(self, plan: Plan, row: Dict[str, str]) -> None:
        """A referent that asks for a task: file one linked to the message."""
        uid = row["uid"]
        if not uid:
            self._clarify_referent(
                plan,
                "I do not have a message to build a task from. Find one first, or "
                "name it: for example, \"task from the email about the invoice\".")
            return
        spec = self.tools.get("task_from_email")
        problems = self.tools.validate_params("task_from_email", {"uid": uid})
        if spec is None or problems:
            self._clarify_referent(plan, "I could not link that message; try naming it.")
            return
        plan.actions = [
            PlannedAction(
                step_id=1,
                tool="task_from_email",
                params={"uid": uid},
                reason="the person asked for a task from the message the "
                       "conversation selected",
            )
        ]
        plan.intent = "task_from_email"
        self._clear_referent_clarification(plan)
        plan.identifier_sources_used.append("email_selection")

    def _finish_reply_draft(self, plan: Plan, row: Dict[str, str]) -> None:
        """A referent that asks for a reply: draft to the sender, never send.

        The recipient is the address recorded with the selected message. A
        sender that carries no address cannot be drafted to -- guessing one
        would turn a reply into a message to the wrong person -- so the reply
        is asked for rather than invented. Drafting is the LOW, reversible
        step; the send rule (explicit verb + confirmation) is untouched.
        """
        address = self._address_from(row["sender"] or "")
        if not address:
            subject = row.get("subject") or "the message"
            self._clarify_referent(
                plan,
                f"That message ({subject}) did not carry an email address I "
                "could reply to. Tell me the address, or draft to someone: "
                f'"reply to that one to someone@example.com".')
            return
        plan.actions = [
            PlannedAction(
                step_id=1,
                tool="draft_email",
                params={"to": address, "subject": "", "body": plan.referent_body},
                reason="a reply to the message the conversation selected; "
                       "drafted, not sent",
            )
        ]
        plan.intent = "draft_email"
        self._clear_referent_clarification(plan)
        plan.identifier_sources_used.append("email_selection")

    @staticmethod
    def _address_from(value: str) -> str:
        """The address inside a display name ("Billing <a@b.c>"), or the bare
        address, or ``""`` when there is none."""
        from ..integrations.email_provider import looks_like_address

        value = (value or "").strip()
        if not value:
            return ""
        match = re.search(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", value)
        candidate = (match.group(0) if match else value).strip()
        return candidate if looks_like_address(candidate) else ""

    def _current_email_provider_name(self) -> str:
        """The name of the email backend in use now, without forcing a connection.

        The selection records which mailbox it came from; a uid is only
        meaningful in the mailbox that minted it, so a referent must not be
        resolved after the backend changed. ``describe`` is the provider's own
        answer and does not open an IMAP session.
        """
        try:
            from ..integrations.email_provider import get_email_provider
            return str(get_email_provider(self.settings).name or "")
        except Exception as exc:
            log.info("could not name the email provider",
                     extra_fields={"error": str(exc)[:120]})
            return ""

    async def _offer_identifiers(self, request: CommandRequest, plan: Plan) -> None:
        """Fill in the identifiers a question asks for, from the registry's own reads.

        Only a tool declared as an ``identifier_source`` is run, and the registry admits
        only a READ-tier one that needs no arguments of its own. What comes back is
        offered, never chosen: this emits no action, and the sentence still ends in a
        question. A lookup that fails or finds nothing says so, rather than leaving the
        bare request for an id or inventing a row to fill the space.

        The one exception is a lookup that found exactly one row matching the name
        the person gave. Then their own words identify it, and the plan is completed
        rather than asked about -- but the completion is still a *proposal*, and it
        still goes through the permission gate like any other action.
        """
        if not plan.missing_identifiers:
            return
        asker = self.tools.get(plan.intent)
        agent = asker.agent if asker is not None else ""
        notes: List[str] = []

        for param in plan.missing_identifiers:
            spec = self.tools.identifier_source(param, agent)
            if spec is None:
                continue
            plan.identifier_sources_used.append(spec.name)
            shape = (spec.provides_identifiers or {}).get(param, {})
            result = await self._run_identifier(
                spec, request, lookup=plan.named_thing,
                lookup_param=str(shape.get("lookup") or ""))
            if result is None or not getattr(result, "ok", False):
                reason = getattr(result, "error", None) or "the check did not answer"
                notes.append(f"I asked {spec.name} and it could not tell me: {reason}.")
                continue
            rows = list((getattr(result, "output", None) or {}).get(shape.get("items", "")) or [])
            if not rows:
                notes.append("I checked, and there is nothing of that kind right now.")
                continue
            if self._resolve_from_lookup(plan, param, shape, rows):
                # Exactly one row matched what they said. Their words identified
                # it; the question is answered, so it is not asked.
                continue
            shown = rows[: self.IDENTIFIER_OFFER_LIMIT]
            listed = ", ".join(
                "%s (%s)" % (row.get(shape.get("label", ""), ""),
                             row.get(shape.get("id", ""), "?"))
                for row in shown
            )
            more = f" (+{len(rows) - len(shown)} more)" if len(rows) > len(shown) else ""
            notes.append(f"Here is what I have: {listed}{more}.")

        if not notes:
            return
        addition = " ".join(notes)
        if plan.clarification_question:
            plan.clarification_question = f"{plan.clarification_question} {addition}"
        if plan.direct_reply:
            plan.direct_reply = f"{plan.direct_reply} {addition}"

    def _resolve_from_lookup(self, plan: Plan, param: str, shape: Dict[str, Any],
                             rows: List[Dict[str, Any]]) -> bool:
        """Fill one identifier from the lookup, when their own words named it.

        Two conditions, both required. The lookup must have been *searched* by
        name -- a source that lists a window and was not given a lookup
        parameter has not answered "which one did you mean", only "what is on
        the calendar". And exactly one row must have come back: two events
        called "Review" are still ambiguous, and picking the first would be a
        coin toss presented as an answer.
        """
        if not plan.named_thing or not shape.get("lookup"):
            return False
        if len(rows) != 1:
            return False
        row = rows[0]
        identifier = row.get(shape.get("id", ""), "")
        if not identifier:
            return False
        plan.resolved_identifier[param] = str(identifier)
        return True

    def _complete_from_resolved(self, plan: Plan) -> bool:
        """Turn a plan that asked for an id into the action that id names.

        The planner built a question, not an action, because the sentence did
        not carry the identifier. The lookup found it. The action is rebuilt
        here, with the identifier filled in and nothing else changed -- the
        tool, the risk and the confirmation are the ones the registry declares,
        so this completes the plan rather than substituting a different one.

        Returns ``False`` when the plan cannot be completed (no tool, or the
        filled-in parameters do not validate), and the plan then stays a
        question.
        """
        spec = self.tools.get(plan.intent)
        if spec is None:
            return False
        params: Dict[str, Any] = {}
        for param, value in plan.resolved_identifier.items():
            params[param] = value
        problems = self.tools.validate_params(spec.name, params)
        if problems:
            log.info("identifier did not complete the plan",
                     extra_fields={"tool": spec.name, "problems": problems})
            return False
        plan.actions = [
            PlannedAction(
                step_id=1,
                tool=spec.name,
                params=params,
                reason=f"{plan.intent} with the {', '.join(sorted(plan.resolved_identifier))} "
                       "the person named",
            )
        ]
        return True

    async def _run_identifier(self, spec, request: CommandRequest,
                              lookup: str = "", lookup_param: str = ""):
        """Run one enumerator for the purpose of phrasing a question.

        Failures come back rather than raising: a lookup that cannot answer should leave
        the question standing, not replace it with an error nobody asked about.

        ``lookup`` is the name the person gave and ``lookup_param`` is the
        parameter of this source that takes it. A source that lists "today" by
        default must not be run that way: "cancel my Design Review event" asked
        for an event six days out, and a lookup that only looked at today
        reported that nothing of that kind existed.
        """
        handler = self.tools.handler(spec.name)
        if handler is None:
            return None
        kwargs = self._lookup_params(spec, lookup, lookup_param)
        try:
            return await handler(self._context(request), **kwargs)
        except Exception as exc:  # a read that throws must not become the user's problem
            log.warning("identifier lookup raised",
                        extra_fields={"tool": spec.name,
                                      "error": f"{type(exc).__name__}: {exc}"})
            return None

    @staticmethod
    def _lookup_params(spec, lookup: str, lookup_param: str) -> Dict[str, Any]:
        """The arguments an identifier source is called with.

        The source declares, per identifier, which of its parameters takes the
        name to search for (``provides_identifiers[...]["lookup"]``). Without
        that declaration the source is run with no arguments, which for a
        windowed enumerator means its default window.
        """
        if not lookup or not lookup_param:
            return {}
        if lookup_param not in (spec.parameters or {}):
            return {}
        return {lookup_param: lookup}

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
        # Who asked, next to what was asked to change. The ``device`` column below is
        # the *target* device, which Phase 31 made deliberate and must stay deliberate;
        # before this, a command that arrived from a phone in another room was written
        # identically to one typed on the laptop, and nothing was faked but nothing was
        # traceable either. Found by running the Phase 59 relay and reading the ledger.
        detail["requested_by"] = request.device_id
        if getattr(request, "source", ""):
            detail["surface"] = request.source
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
        # One sentence, one deadline. Each read already bounds itself; without a ceiling
        # over the whole request those bounds add up -- measured, "remember that ..."
        # made two 30s reads, so the wait a person faces was 60s and the number in the
        # code meant nothing. A confirmation held for this long is still an answer.
        with request_budget(SENTENCE_BUDGET):
            return await self._handle(request)

    async def _handle(self, request: CommandRequest) -> CommandResponse:
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
        # Same V2 pronoun rewrite as the other command surface, so "close it"
        # behaves identically whether it arrives by voice or by typing. The
        # resolved name is planned and gated exactly like a typed one.
        request_text = request.text
        if getattr(self.settings, "voice_operator_v2_enabled", False):
            from ..voice.resolve import resolve_reference

            resolved = resolve_reference(request_text, self.entity_memory,
                                         request.session_id, kind="app")
            if resolved:
                log.info("voice operator v2: resolved a pronoun reference")
                request_text = resolved
        plan = self.planner.parse(request_text, request.context)
        response.intent = plan.intent
        response.plan = plan
        response.model_used = plan.planner

        # A demonstrative ("read that one", "summarize the second one") names no
        # message: it points at what this conversation just dealt with. Resolve
        # it against the per-session selection before anything else -- a
        # referent that resolves becomes a real read of a real uid, and one
        # that does not is answered with an honest "there is nothing to point
        # at", never a guess and never another session's selection.
        self._resolve_email_referent(request, plan)

        # A question that asks for an identifier the system already holds is asking the
        # user to do a lookup the product has already done (Phase 33). Answering that
        # part is done before either reply branch, because a route's question arrives as
        # a ``direct_reply`` and the planner's own clarification as a
        # ``clarification_question`` -- both are questions, and both deserve the same
        # help.
        await self._offer_identifiers(request, plan)

        # The lookup may have answered the question the planner asked. When the
        # person's own words named exactly one thing, the plan can be completed
        # instead of asked about -- and the completed plan goes to the gate
        # like every other action, so "cancel my Design Review event" now asks
        # for approval to cancel that event instead of asking which event.
        if plan.resolved_identifier and self._complete_from_resolved(plan):
            plan.direct_reply = None
            plan.clarification_question = None
            plan.needs_clarification = False
            plan.reply_kind = ""

        if plan.direct_reply:
            response.reply = plan.direct_reply
            stage = "planner"
            model_note: Dict[str, Any] = {}
            if plan.reply_kind in ("not_understood", "smalltalk", "clarification_question",
                                     "question", "info_request") \
                    and self.settings.llm_enabled:
                response.reply, model_note = await self._advise_from_model(
                    request, plan.direct_reply, conversational=True)
                response.model_used = (model_note.get("label") or "rules") \
                    if model_note.get("stage") == "model" else "rules"
                stage = model_note.get("stage", "planner")
                # R2.3: the model may know how to *act* on a sentence the rules
                # could not route at all. Ask it to propose, and if the proposal
                # validates, run it through the same gate a rule-planned action
                # goes through. The model proposes; the permission engine
                # decides.
                #
                # Only for `not_understood`: that is the one reply_kind that
                # says the rules found nothing. A greeting or a recognised
                # question is a question, and asking the model to plan an
                # action for it would double the latency of every "hello" to
                # catch a case the rules already classified.
                if model_note.get("stage") == "model" and plan.reply_kind == "not_understood":
                    proposal = await self._propose_action(request)
                    if proposal is not None and proposal.actions:
                        response.reply = ""
                        response.plan = proposal
                        response.intent = proposal.intent
                        response.model_used = model_note.get("label") or "llm"
                        self._note_unhandled(response, proposal)
                        self.audit.record(
                            intent=request.text,
                            result="no_action",
                            risk="none",
                            device=request.device_id,
                            session_id=request.session_id,
                            request_id=response.request_id,
                            detail={"stage": "model_proposal",
                                    "tool": proposal.actions[0].tool,
                                    "reply_kind": proposal.reply_kind,
                                    "model": model_note},
                        )
                        return await self._execute_plan(request, proposal, response, started)
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
                detail={"stage": stage,
                        "reply": response.reply[:200],
                        # Which kind of non-action this was, so a refusal and a question
                        # stay distinguishable in the chain and not only on screen.
                        "reply_kind": plan.reply_kind,
                        "unhandled": list(plan.unhandled),
                        # A question that names candidates got them from a read, and the
                        # chain says which one (Phase 33 -- the same attribution debt R-14
                        # settled for a fallback that answers with another tool's result).
                        "identifier_sources_used": list(plan.identifier_sources_used),
                        **({"model": model_note} if model_note else {})},
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
                        "identifier_sources_used": list(plan.identifier_sources_used),
                        "reply_kind": plan.reply_kind or "clarification",
                        "unhandled": list(plan.unhandled)},
            )
            self._log_message(request.session_id, "assistant", response.reply, plan.intent)
            response.duration_ms = int((time.perf_counter() - started) * 1000)
            return response

        # --- Stage 2+: permission -> execute -> verify ----------------------
        return await self._execute_plan(request, plan, response, started)

    def _plan_changes_the_world(self, plan: Plan) -> bool:
        """Whether any action in *plan* is more than a read.

        Mission mode exists to make a *side effect* verifiable: it records steps
        and conditions so the mission can say whether the world changed. A plan
        made only of READ-tier actions changes nothing, so diverting it adds no
        verification and destroys the answer instead.

        Measured: with the flag on, "what app is in front" (list_apps, READ)
        answered "I ran that, but could not confirm it: steps ran, but the
        outcome is unknown" where the inline path answers "I know 16
        applications on this macos machine." The read produced its data; mission
        mode replaced it with a caveat about an outcome that was never at stake.

        So a read-only plan always takes the inline path, whatever the flag says.
        An unknown tool is treated as world-changing (the conservative choice):
        the permission engine and the verifier decide, rather than this guessing
        a tool is harmless.
        """
        for action in (getattr(plan, "actions", None) or []):
            try:
                spec = self.tools.get(getattr(action, "tool", ""))
            except Exception:
                spec = None
            if spec is None:
                return True
            if getattr(spec, "risk", None) != RiskLevel.READ:
                return True
        return False

    async def _run_plan_as_mission(self, request: CommandRequest, plan: Plan,
                                  response: CommandResponse,
                                  started: float) -> Optional[CommandResponse]:
        """Flagged path: execute the plan through MissionRunner, then report it.

        Returns ``None`` when the mission could not be created or had nothing to
        run, so the caller falls back to the inline loop rather than reporting a
        result that did not happen.

        The reply is built from the mission's *own* record -- its state and its
        condition verdicts -- not from the action messages. That is the whole
        point: the mission is the thing that can say "verified", and if it says
        unknown the reply says unknown.
        """
        from .voice_mission import run_as_mission

        mission = await run_as_mission(self, request, plan)
        if mission is None:
            return None

        response.mission_id = mission.mission_id
        response.plan = plan
        response.intent = plan.intent
        verdict = mission.verdict()
        state = str(getattr(mission, "state", "") or "")

        if state == MissionState.AWAITING_APPROVAL.value:
            response.needs_confirmation = True
            response.decision = PolicyDecision.CONFIRM
            response.reply = (
                "This needs your approval before it runs. "
                "Open Mission Control to review the step and decide.")
        elif verdict == "verified":
            response.decision = PolicyDecision.EXECUTE
            checked = ", ".join(
                c.statement for c in mission.conditions
                if getattr(c, "status", "") == "verified") or "its conditions"
            response.reply = f"Done, and I checked it: {checked}."
        elif state == MissionState.BLOCKED.value:
            response.decision = PolicyDecision.BLOCK
            response.blocked_reason = (getattr(mission, "reason", "") or
                                       "the permission engine refused a step")
            response.reply = f"I did not do that: {response.blocked_reason}".rstrip(".")
            response.reply += "."
        else:
            # Failed, cancelled, or ran-but-unverified. The mission cannot claim
            # a verification it did not make, so neither does the reply.
            response.decision = PolicyDecision.EXECUTE
            detail = (getattr(mission, "reason", "") or
                      "the outcome could not be confirmed")
            response.reply = f"I ran that, but could not confirm it: {detail}".rstrip(".")
            response.reply += "."

        self._note_unhandled(response, plan)
        self._log_message(request.session_id, "assistant", response.reply, plan.intent)
        self.audit.record(
            intent=request.text, result="mission",
            device=getattr(request, "device_id", "laptop_main"),
            session_id=request.session_id, request_id=response.request_id,
            detail={"mission_id": mission.mission_id, "state": state,
                    "verdict": verdict})
        response.duration_ms = int((time.perf_counter() - started) * 1000)
        return response

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
        # Flagged alternative: run these actions as a real mission so the record
        # carries step outcomes and an independent verification. The inline loop
        # below is left completely untouched -- it is still what runs when the
        # flag is off, which is the default. `seqs`/`job_id` are a retry's
        # bookkeeping and mean the inline path was already chosen; a retry is
        # therefore never diverted here.
        if (getattr(self.settings, "command_mission_mode", False)
                and plan.actions and job_id is None and not seqs
                and self._plan_changes_the_world(plan)):
            diverted = await self._run_plan_as_mission(request, plan, response, started)
            if diverted is not None:
                return diverted
        if job_id is None and len(plan.actions) > 1:
            job_id = self.tasks.create(request.text, [a.tool for a in plan.actions],
                                       planned=list(plan.actions)).job_id
        if job_id:
            response.job_id = job_id
        # Record the executed command as a mission so it is inspectable later.
        # The mission is created here, before the actions run, with one
        # condition per action: the reply a person sees and the ledger Mission
        # Control reads are then the same work, not two parallel accounts.
        mission_id = ""
        if plan.actions:
            try:
                mission = self.missions.create(
                    (request.text or "").strip() or "command",
                    scope={}, conditions=[
                        {"condition_id": f"S{index + 1}",
                         "statement": f"{action.tool} on {action.device}",
                         "verifier": f"tool:{action.tool}"}
                        for index, action in enumerate(plan.actions)],
                    steps=[{"step_id": f"S{index + 1}", "title": a.tool,
                            "tool": a.tool, "params": dict(a.params or {})}
                           for index, a in enumerate(plan.actions)])
                self.missions.transition(mission.mission_id, MissionState.PLANNED,
                                         actor="pipeline", reason="the planner routed this command")
                self.missions.transition(mission.mission_id, MissionState.RUNNING,
                                         actor="pipeline", reason="actions are executing")
                mission_id = mission.mission_id
            except Exception as exc:
                log.info(f"could not record a mission for this command: "
                         f"{type(exc).__name__}: {str(exc)[:200]}")
        if mission_id:
            response.mission_id = mission_id

        step_seqs = list(seqs) if seqs is not None else list(range(len(plan.actions)))

        for index, action in enumerate(plan.actions):
            # The surface that raised the request goes to the gate, so a
            # confirmation records who asked for it. The self-approval rule
            # needs that: a narrow surface approving its own request is
            # refused, and "whose request was this" is the question.
            verdict = self.policy.evaluate(
                action,
                session_id=request.session_id,
                requested_surface=self._request_surface(request),
            )
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
                self._settle_mission(mission_id, outcome="blocked",
                                     reason=f"policy refused: {verdict.reason}")
                return response

            if verdict.decision is PolicyDecision.CONFIRM:
                action.status = ActionStatus.AWAITING_CONFIRMATION
                response.decision = PolicyDecision.CONFIRM
                response.needs_confirmation = True
                response.confirmation_id = verdict.confirmation_id
                self._settle_mission(
                    mission_id, outcome="blocked",
                    reason="waiting for approval before any action ran")
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
            # V2 (flagged): remember what an action *observed*, not what it was
            # told to do. A launch that reported verified=True watched the
            # process table and found it; a launch that could not confirm the
            # application is deliberately NOT remembered, because a later
            # "close it" must not name an app nobody saw running. Recording is
            # not authorization -- the resolved sentence is gated like any other.
            if getattr(self.settings, "voice_operator_v2_enabled", False):
                self._remember_verified_entity(action, result, request)
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

        # --- Stage: selection ---------------------------------------------
        # A mailbox read just happened: record what it actually returned so a
        # follow-up ("read that one", "the second one") can point at it. Only
        # real results set anything -- a failed read, a draft or a send never
        # touches the selection, and an empty search clears it rather than
        # leaving an earlier pointer standing.
        self._record_email_selection(request, response)

        # --- Stage: memory update -------------------------------------------
        self._remember_outcome(request, response)

        # --- Stage: response -------------------------------------------------
        response.reply = self._compose_reply(request.text, response)
        response.decision = response.decision or PolicyDecision.EXECUTE
        self._note_unhandled(response, plan)
        self._log_message(request.session_id, "assistant", response.reply, plan.intent)
        response.duration_ms = int((time.perf_counter() - started) * 1000)
        # The actions ran. Whether the command is *verified* is a separate
        # question -- a tool returning success is not an observation -- so the
        # mission closes as partially_completed unless a verifier said otherwise.
        failed = [a for a in response.actions if not getattr(a, "ok", False)]
        self._settle_mission(
            mission_id,
            outcome="failed" if failed else "partially_completed",
            reason=("one or more actions failed" if failed else
                    "actions ran; their outcome was not independently verified"))
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
        self, confirmation_id: str, approve: bool, session_id: str = "", 
        request_id: str = "", approver_session: str = "", approver_surface: str = ""
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
        
        # Get approver surface from session for provenance tracking — but respect a
        # surface the caller already supplied. A local phone self-approval test passes
        # approver_surface="phone" directly; overwriting it with the session-derived
        # "unknown" would silently disarm the guard.
        if not approver_surface:
            from jarvis.api.app import _surface_of
            approver_surface = _surface_of(session_id) or "unknown"
        
        # Reject self-approval at server side. Three conditions keep this honest:
        # (1) `approve` -- a *denial* can never be a self-approval, so "no"/"cancel"
        #     must never be swallowed by this guard;
        # (2) `session_id` -- an empty approver session (a cross-device approval that
        #     carries no session identity, e.g. a wrist CONFIRM) has no "who" to
        #     compare, so comparing "" == "" would deny a legitimate approval;
        # (3) narrow-surface -- the Phase 71 rule is about a *remote* device approving
        #     itself. A phone session must not approve its own request; a local
        #     (CLI/voice) session and the dashboard are the operator, not a narrow
        #     handset. The surface that minted the session is the authority here.
        requested_session = record.get("requested_by_session", record.get("session_id", ""))
        # Only a narrowed surface (the phone) is forbidden from approving its own
        # request. A local CLI/voice session and the dashboard are the operator, not a
        # narrow handset, so their surface is NOT in NARROW_SURFACES and they approve
        # their own requests legitimately. The previous `!= "dashboard"` test wrongly
        # blocked local voice too (its surface is "unknown" because a voice session is a
        # local JarvisCore session, not a gateway session) — voice "yes" was rejected
        # as self-approval and the pending delete never ran.
        from jarvis.api.app import NARROW_SURFACES
        # Two independent facts say "this is a phone": the surface the
        # *approver* is on, and the surface the *request* was raised on.
        # Either one being narrow is enough to refuse a self-approval --
        # they are separate facts, and either being wrong must not be enough
        # to let the approval through.
        requested_surface = record.get("requested_surface", "")
        # The invariant is "a device may not approve its own risky request".
        #
        # The previous condition required `requested_session == session_id`, and
        # those two values are never the same kind of thing: the confirmation
        # stores the *conversation* id (SESS-...), while the approver presents
        # the *HTTP session token*. Measured on a live server: a phone raised a
        # Google task write and approved it itself; the guard never fired.
        #
        # Two rules now, and the first needs no matching identifier at all:
        #
        # (a) A request raised on a narrow surface must be answered from a
        #     non-narrow one. The phone is the only narrow surface, so this
        #     says "approve it on the laptop" -- which is exactly what the
        #     refusal text has always told people to do. It is deliberately
        #     conservative: it does not try to tell two handsets apart, because
        #     doing that would mean storing session tokens in the database.
        # (b) The session-identity rule stays for callers whose ids *are*
        #     comparable (a raw API caller that passes both the same way).
        raised_on_narrow = requested_surface in NARROW_SURFACES
        approver_is_narrow = approver_surface in NARROW_SURFACES
        identity_match = bool(session_id) and requested_session == session_id

        # (a) needs no identifier: a narrow surface may never answer a request
        #     that a narrow surface raised.
        # (b) preserves the original rule for callers with comparable ids.
        self_approval = (raised_on_narrow and approver_is_narrow) or \
                        (identity_match and (approver_is_narrow or raised_on_narrow))
        if approve and self_approval:
            log.warning(
                "self-approval rejected",
                extra_fields={
                    "confirmation_id": confirmation_id,
                    "session": session_id,
                    "approver_surface": approver_surface,
                },
            )
            # Refuse the *attempt*, not the request.
            #
            # This used to resolve the confirmation as denied, which destroyed
            # the very thing the message points at: the phone was told "approve
            # this on the laptop" while the record had just been marked denied,
            # so the laptop could not approve it either. Measured. A
            # self-approval attempt leaves the request pending and answerable by
            # a wider surface; otherwise the refusal is a silent cancellation.
            response.reply = ("You cannot approve your own requests. This is still "
                              "waiting — approve it on the laptop instead.")
            response.needs_confirmation = True
            response.decision = PolicyDecision.BLOCK
            self.audit.record(
                intent=record["tool"],
                tool=record["tool"],
                result="denied",
                risk="denied_self_approval",
                confirmation=True,
                session_id=session_id,
                request_id=request_id or response.request_id,
                detail={"reason": "self-approval prohibited"},
            )
            return response

        # A confirmation can only be approved while its requester is still a live session.
        # If the session that raised it was explicitly ended (the device signed out, or the
        # laptop revoked it), the approval would be acting on a request nobody is standing
        # behind -- so it is refused. This is checked by the requester's session id, not the
        # approver's: a live dashboard approving a *live* phone's request is legitimate; a
        # dashboard approving a *signed-out* phone's stale request is not.
        if approve and requested_session:
            from jarvis.api.app import _is_session_revoked
            if _is_session_revoked(requested_session):
                log.warning(
                    "approval refused: requesting session was revoked",
                    extra_fields={
                        "confirmation_id": confirmation_id,
                        "requester_session": requested_session,
                    },
                )
                response.reply = (
                    "That request was raised by a session that has since signed out, so it "
                    "can no longer be approved. Ask again from a live session."
                )
                response.needs_confirmation = False
                response.decision = PolicyDecision.BLOCK
                self.policy.resolve(confirmation_id, approve=False,
                                    approver_session=session_id, approver_surface=approver_surface)
                self.audit.record(
                    intent=record["tool"],
                    tool=record["tool"],
                    result="denied",
                    risk="denied_revoked_requester",
                    confirmation=True,
                    session_id=session_id,
                    request_id=request_id or response.request_id,
                    detail={"reason": "requesting session was revoked"},
                )
                return response

        # Resolve with provenance tracking
        self.policy.resolve(confirmation_id, approve, 
                          approver_session=session_id, approver_surface=approver_surface)

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
            # The tier of the action that actually ran, read from the stored
            # confirmation. This said "confirmed" -- a workflow state sitting in
            # the risk column -- so a passport counting actions by risk reported
            # one called "confirmed", and the real tier of a confirmed write
            # was unrecoverable from the ledger. The confirmation flag is
            # already the `confirmation=True` field below; the risk column is
            # for risk.
            risk=self.classifier.risk_label(RiskLevel(record["risk"])),
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
        """What the user is asked to approve.

        The engine already built a ``preview`` for this confirmation and the dashboard
        renders that string; this prompt used to ignore its own argument and re-derive a
        line from the raw parameters. Two renderings of one consent question is how they
        start to disagree, and whichever one the user reads, only one of them is stored.
        """
        what = preview or ", ".join(f"{k}={v}" for k, v in action.params.items()) or "no parameters"
        return (
            f"This needs your confirmation before I run it.\n"
            f"  Will do: {what}\n"
            f"  Tool: {action.tool}\n"
            f"  On device: {action.device}\n"
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

    def _is_observation(self, result) -> bool:
        """Whether the action *reported* state rather than changing it.

        A read-only tool's ``verification=UNKNOWN`` does not mean "the outcome is
        unknown" -- it means the tool never claimed to alter anything. Its
        message is the observation itself, so the "could not verify it happened"
        caveat is both unnecessary and contradictory there. A tool that changes
        the world keeps the caveat, because for those the caveat is the truth.

        Read from the registry's own spec, which the permission engine also
        trusts, rather than from a hand-maintained list that would drift.
        """
        try:
            spec = self.tools.get(getattr(result, "tool", ""))
        except Exception:
            return False
        if spec is None:
            return False
        return getattr(spec, "risk", None) == RiskLevel.READ

    def _record_email_selection(self, request: CommandRequest, response: CommandResponse) -> None:
        """Point the session at the message a mailbox read just returned.

        This is the only place the per-session selection moves, and it moves
        only from **real results**: an ``ActionResult`` that the agent actually
        ran and reported. The rules, matched to how the person talks:

        * a search that found exactly one message selects it, so "read that
          one" / "summarize it" has something to point at;
        * a search that found several keeps the **list** (in the order the
          reply showed it) but selects none of them -- "the second one" can
          then be answered from the list the person saw, without ever
          silently reading the first;
        * a search that found nothing clears the selection rather than
          leaving an earlier pointer standing and answering a stale one;
        * a successful ``read_email`` makes the message it actually read the
          current selection, and leaves the list in place so an ordinal
          still resolves against it;
        * a failed read/search (an unreadable mailbox, a bad uid) touches
          nothing -- the last real result stays the selection.

        Only ``uid`` plus a subject and sender are stored; the body is always
        re-fetched through the read path. The row is keyed by ``session_id``,
        so a selection made here cannot be reached from another session.
        """
        from .email_context import EmailSelection

        for result in response.actions or []:
            if not getattr(result, "ok", False):
                continue
            output = getattr(result, "output", None) or {}
            if not isinstance(output, dict):
                continue
            provider = str(output.get("provider") or "")

            if result.tool == "read_email":
                message = output.get("email") or {}
                if not isinstance(message, dict):
                    continue
                uid = str(message.get("uid") or "")
                if not uid:
                    continue
                prior = self.email_context.get(request.session_id)
                self.email_context.set(request.session_id, EmailSelection(
                    uid=uid,
                    subject=str(message.get("subject") or ""),
                    sender=str(message.get("from") or ""),
                    provider=provider,
                    candidates=prior.candidates if prior else [],
                ))

            elif result.tool == "search_email":
                rows = [r for r in (output.get("results") or []) if isinstance(r, dict)]
                if len(rows) == 1:
                    row = rows[0]
                    self.email_context.set(request.session_id, EmailSelection(
                        uid=str(row.get("uid") or ""),
                        subject=str(row.get("subject") or ""),
                        sender=str(row.get("from") or ""),
                        provider=provider,
                        candidates=rows,
                    ))
                elif rows:
                    # A list, not a choice: several matches identify none of
                    # them on their own, so only the list is kept.
                    self.email_context.set(request.session_id, EmailSelection(
                        provider=provider, candidates=rows,
                    ))
                else:
                    # Nothing matched: clear, so a stale pointer cannot answer
                    # a follow-up about a message this search just ruled out.
                    self.email_context.clear(request.session_id)

    def _remember_verified_entity(self, action, result, request: CommandRequest) -> None:
        """V2: remember the application an action *observed*, for later pronouns.

        Only a result that reports ``verified=True`` is recorded. A launch whose
        own handler could not confirm the process (``unverified``) is skipped on
        purpose: "close it" must never resolve to an application nobody saw
        running. The name comes from the result's own ``display_name``/``app``
        field, never from the user's wording, so a misheard sentence cannot seed
        a false target.
        """
        try:
            if not getattr(result, "ok", False) or not getattr(result, "verified", False):
                return
            if action.tool not in ("open_application", "open_app"):
                return
            payload = getattr(result, "output", None) or {}
            if isinstance(payload, dict):
                name = str(payload.get("display_name") or payload.get("app")
                           or payload.get("application") or "").strip()
            else:
                name = ""
            if not name:
                name = str((action.params or {}).get("app") or "").strip()
            if not name:
                return
            self.entity_memory.record(
                request.session_id or "", "app", name, verified=True,
                evidence=str(getattr(result, "message", "") or "")[:200])
        except Exception as exc:  # memory must never break a command that worked
            log.info("voice operator v2: could not record an entity: %s", type(exc).__name__)

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
                # Check verification state for truthful reporting
                if result.verification == Verification.SUCCESS:
                    lines.append(result.message or f"{result.tool} completed.")
                elif result.verification == Verification.UNKNOWN:
                    # Executed but couldn't observe outcome - be explicit.
                    #
                    # Only when the tool was describing a *change it made*. A
                    # tool whose answer IS the observation -- "Calculator is
                    # installed (version 12.0) and is running" -- reports
                    # verification=UNKNOWN because it is not claiming to have
                    # altered the world, and appending "but I could not verify it
                    # happened" to that sentence contradicted the fact it had
                    # just stated. Measured: "is it running?" answered
                    # "Calculator ... is running, but I could not verify it
                    # happened." The fields were honest; the composed sentence
                    # was not. Reads are marked by a tool tag, so a change still
                    # gets the caveat and an observation does not.
                    base_msg = (result.message or f"{result.tool} executed").rstrip(". ")
                    described_state = self._is_observation(result)
                    if described_state:
                        lines.append(f"{base_msg}.")
                    else:
                        lines.append(f"{base_msg}, but I could not verify it happened.")
                else:
                    # Should not happen when ok=True, but handle gracefully
                    lines.append(result.message or f"{result.tool} completed.")
            else:
                # A tool's own summary is usually a sentence written for a
                # person -- "I would need an AI provider for that one" says
                # more than "That did not work:" does. The prefix is only
                # added when the tool said nothing useful.
                summary = str(result.summary or "").strip()
                if summary and not summary.endswith((".", "!", "?")):
                    summary += "."
                lines.append(summary or f"That did not work: {result.summary}")

        if len(lines) == 1:
            reply = lines[0]
        else:
            reply = "\n".join(f"{i + 1}. {line}" for i, line in enumerate(lines))

        # If something failed first and was recovered, say so. Reporting only the
        # success would hide that the system is running degraded.
        if response.recovery_notes:
            reply += "\n\n(Recovered automatically — " + "; ".join(response.recovery_notes) + ")"
        return reply
