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

        return ModelChoice(
            role=role,
            model=model,
            provider=provider.name,
            reason=f"task_type={task_type} complexity={complexity}",
        )

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
                                 json_mode=json_mode, timeout=timeout)
        return reply.text if reply.ok else None

    async def reply(self, system: str, user: str, *, model: str,
                    provider_name: str = "", json_mode: bool = False,
                    timeout: float = 30.0):
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
                                     timeout=timeout)
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
    ) -> Optional[Dict[str, Any]]:
        """Complete and parse a JSON object, returning None on any failure."""
        raw = await self.complete(
            system, user, task_type=task_type, complexity=complexity, json_mode=True
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
