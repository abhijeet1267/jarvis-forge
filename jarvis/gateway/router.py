"""Pluggable model gateway and router (sections 28, 29, 40).

The architecture must not depend on one AI vendor, and since Phase 60 it does not: the wire format
lives in `providers.py` behind one selection point, and this module keeps the two things a caller
actually needs -- *which model role answers this kind of task*, and *ask it a question*.

What is deliberately unchanged: the router answers, it does not act. `JarvisCore` calls
`complete()` only for a sentence the rule planner did not recognise, and the reply is reported as
a suggestion. Planning, permission and execution stay where Phase 42 put them.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ..config import get_settings
from ..logging_setup import get_logger
from .providers import ProviderUnknown, select_provider

log = get_logger("gateway.router")


@dataclass
class ModelChoice:
    """A routing decision."""

    role: str
    model: str
    provider: str
    reason: str


# Task type -> model role.
ROUTING_TABLE: Dict[str, str] = {
    "simple_command": "fast",
    "conversation": "general",
    "planning": "reasoning",
    "complex_planning": "reasoning",
    "coding": "coding",
    "vision": "vision",
    "research": "research",
    "memory": "fast",
}


class ModelRouter:
    """Chooses which model should handle a task, and asks it."""

    def __init__(self, settings=None) -> None:
        self.settings = settings or get_settings()

    # -- the vendor the settings name --------------------------------------

    @property
    def provider(self):
        """The adapter this router will use. Re-resolved, because `llm.env` can change at startup.

        An unusable name is not swallowed here: `ProviderUnknown` is what makes a misconfigured
        install say so instead of quietly calling a vendor the operator never chose.
        """
        return select_provider(self.settings)

    def route(self, task_type: str, complexity: str = "normal") -> ModelChoice:
        role = ROUTING_TABLE.get(task_type, "general")
        if complexity == "high":
            role = "reasoning"

        if not self.settings.llm_enabled:
            return ModelChoice(
                role="rules",
                model="deterministic-planner",
                provider="local",
                reason="no model endpoint configured; using offline rule planner",
            )

        try:
            provider = self.provider
        except ProviderUnknown as exc:
            return ModelChoice(role="rules", model="deterministic-planner",
                               provider="refused", reason=str(exc))

        model = {
            "fast": self.settings.llm_fast_model,
            "general": self.settings.llm_model,
            "reasoning": self.settings.llm_reasoning_model,
        }.get(role, self.settings.llm_model)

        # A role model the configured vendor cannot serve is not a plan B -- it is a
        # dead end. `llm_fast_model` and `llm_reasoning_model` default to OpenAI
        # names (`gpt-4o-mini`, `o4-mini`), so an install that points at a gateway
        # (9Router), Groq or Gemini sends those names to a vendor that has never
        # heard of them, and the call fails with "No active credentials for
        # provider: openai".
        #
        # That failure was invisible on the conversational path, because
        # `conversation` routes to `general` -- the one role whose model *is* the
        # configured one. The planner routes to `reasoning`, so every
        # model-proposed action died at the first request: the assistant said "I can
        # set that up for you!", the user said "yes, go ahead", and nothing happened
        # and nothing was asked for approval.
        model, substituted = self._servable_model(provider, model)

        reason = f"task_type={task_type} complexity={complexity}"
        if substituted:
            reason += (f"; {provider.name} cannot serve that role's model, "
                       f"using {model!r} instead")
            log.info("role model not offered by provider", extra_fields={
                "provider": provider.name, "role": role, "model": model})

        return ModelChoice(
            role=role,
            model=model,
            provider=provider.name,
            reason=reason,
        )

    def _servable_model(self, provider, model: str) -> tuple:
        """The model to actually call for a role, and whether it was substituted.

        The role models are *hints* about which model suits which kind of task.
        They are not the vendor's own catalogue: the same settings object names
        ``gpt-4o-mini`` for the fast role whether the vendor is OpenAI, Groq,
        Gemini or a local gateway. A hint the vendor cannot honour is worth
        nothing, and worse than nothing, because it turns a working provider into
        a dead route.

        The configured model (``llm_model``) is the one the operator chose *for
        this provider*, so it is the safe answer for any role the provider cannot
        serve specifically.

        Two sources of truth about what the vendor can serve, in order:

        1. **The vendor's own catalogue**, when it publishes one (OpenAI, Groq,
           Gemini). A role model that is not in it will be refused.
        2. **Recorded evidence**, when it does not (9Router and most compatible
           gateways publish no static list). A model the route-health ledger has
           already seen refused with "model not found" is not tried again -- the
           ledger is exactly the record of what this gateway has already told us.
        """
        if not model:
            return model, False

        configured = str(getattr(self.settings, "llm_model", "") or "").strip()

        offered = getattr(provider, "SUPPORTED_MODELS", None) or []
        if offered:
            if model in offered:
                return model, False
            if configured and configured in offered:
                return configured, True
            # Neither the role model nor the configured model is one of this
            # vendor's -- the operator's model is not one of this vendor's. Fall
            # back to the vendor's own first model rather than to a name it will
            # refuse.
            return offered[0], True

        # No catalogue: trust the ledger. A model this gateway already refused
        # with "model not found" is known-dead, and calling it again would spend a
        # round trip to learn the same thing.
        if self._known_unservable(provider.name, model):
            if configured and configured != model:
                return configured, True
            return model, False

        return model, False

    @staticmethod
    def _known_unservable(provider_id: str, model: str) -> bool:
        """Whether the ledger has already seen this provider refuse this model.

        ``model_not_found`` is the only category that counts here. A timeout or a
        rate limit says something about the route *right now*, not about the name
        of the model, and treating a flaky route as a dead name would silently
        downgrade a working model.
        """
        if not model:
            return False
        try:
            from .route_health import get_route_health

            entry = get_route_health().snapshot(provider_id, model)
        except Exception:  # the ledger is best-effort evidence, never a failure
            return False
        if entry.get("last_error_category") != "model_not_found":
            return False
        # A later success for the same name overrules an earlier refusal.
        last_failure = entry.get("last_failure_at") or 0
        last_success = entry.get("last_success_at") or 0
        return last_failure >= last_success

    # -- completion ----------------------------------------------------------
    async def complete(
        self,
        system: str,
        user: str,
        *,
        task_type: str = "conversation",
        complexity: str = "normal",
        json_mode: bool = False,
        timeout: float = 30.0,
        history: Optional[List[Dict[str, str]]] = None,
    ) -> Optional[str]:
        """Call the configured model. Returns None when there is no answer.

        `None` is the contract the orchestrator was written against: it is the signal to say "I
        asked the model you configured and it was unavailable" rather than to pass a failure up
        through a command pipeline that can still answer on its own rules.
        """
        choice = self.route(task_type, complexity)
        if choice.provider in ("local", "refused"):
            return None
        reply = await self.reply(system, user, model=choice.model, provider_name=choice.provider,
                                 json_mode=json_mode, timeout=timeout, history=history)
        if reply.ok:
            return reply.text

        # The route's model was refused outright ("model not found" / "no active
        # credentials for provider: openai"). The role model is only a hint, and the
        # provider is the one that decides which names it serves -- so the
        # configured model is tried once before the turn is given up.
        #
        # One retry, on a *different* model, for a request that provably never
        # reached a model at all: the first attempt was refused at the routing
        # layer, so nothing was generated, billed or persisted, and there is no
        # double-answer risk. This is not the "no retries" rule in
        # `AIProvider._post` (which is about re-sending a *non-idempotent*
        # completion whose outcome is unknown); here the first call is known not
        # to have produced a completion.
        fallback = self._fallback_model(choice, reply)
        if fallback:
            log.info("retrying with the configured model", extra_fields={
                "provider": choice.provider, "tried": choice.model, "using": fallback,
                "error": reply.error[:120]})
            second = await self.reply(system, user, model=fallback,
                                      provider_name=choice.provider, json_mode=json_mode,
                                      timeout=timeout, history=history)
            if second.ok:
                return second.text
            return None
        return None

    def _fallback_model(self, choice: ModelChoice, reply) -> str:
        """The model to try after the route's model was refused, or "" for none.

        Only a refusal *of the model name* qualifies. A timeout, a rate limit or a
        server error says something about the route at that moment, and retrying a
        different model would hide a transient failure behind a different bill.
        """
        category = str((reply.notes or {}).get("category") or "")
        if category not in ("model_not_found", "route_unavailable", "auth_failed"):
            return ""
        if choice.model == str(getattr(self.settings, "llm_model", "") or "").strip():
            return ""
        fallback = str(getattr(self.settings, "llm_model", "") or "").strip()
        return fallback if fallback else ""

    async def reply(self, system: str, user: str, *, model: str,
                    provider_name: str = "", json_mode: bool = False,
                    timeout: float = 30.0,
                    history: Optional[List[Dict[str, str]]] = None):
        """The same call, with its attribution and its failure kept.

        `complete()` answers a question; this answers "what happened when you asked it", which is
        what a status line, a log row and a person reading a reply all need.
        """
        try:
            provider = self.provider
        except ProviderUnknown as exc:
            from .providers import ProviderReply

            return ProviderReply(False, provider_name or "unknown", model, error=str(exc))
        started = time.perf_counter()
        answer = await provider.chat(system, user, model=model, json_mode=json_mode,
                                     timeout=timeout, history=history or ())
        answer.notes["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        if not answer.ok:
            log.info("model call failed", extra_fields={"provider": answer.provider,
                                                         "model": answer.model,
                                                         "error": answer.error[:160]})
        return answer

    async def complete_json(
        self,
        system: str,
        user: str,
        *,
        task_type: str = "planning",
        complexity: str = "normal",
        timeout: float = 30.0,
        history: Optional[List[Dict[str, str]]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Complete and parse a JSON object, returning None on any failure.

        The failure here is deliberately lossy in one direction only: an answer
        that is not a JSON object becomes ``None`` (the caller decides what to
        do), never a partially-parsed object. A caller that needs to know
        *why* it got nothing should use :meth:`reply`, which keeps the
        transport's own account of the failure.
        """
        raw = await self.complete(
            system, user, task_type=task_type, complexity=complexity,
            json_mode=True, timeout=timeout, history=history,
        )
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            log.info("model returned invalid JSON")
            return None

    def available_roles(self) -> List[str]:
        if not self.settings.llm_enabled:
            return ["rules"]
        return ["fast", "general", "reasoning", "coding", "vision", "research"]
