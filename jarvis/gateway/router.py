"""Pluggable model gateway and router (sections 28, 29, 40).

The architecture must not depend on one AI vendor. This module:

* routes a task type to a suitable model role,
* exposes a single ``complete`` call for planners,
* degrades gracefully to the deterministic rule-based planner when no endpoint
  is configured or the network is unavailable (section 27, offline mode).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ..config import get_settings
from ..logging_setup import get_logger

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
    """Chooses which model should handle a task."""

    def __init__(self) -> None:
        self.settings = get_settings()

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

        model = {
            "fast": self.settings.llm_fast_model,
            "general": self.settings.llm_model,
            "reasoning": self.settings.llm_reasoning_model,
        }.get(role, self.settings.llm_model)

        return ModelChoice(
            role=role,
            model=model,
            provider="openai-compatible",
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
        """Call the configured model. Returns None when unavailable."""
        choice = self.route(task_type, complexity)
        if choice.provider == "local":
            return None

        try:
            import httpx
        except ImportError:  # pragma: no cover
            log.info("httpx unavailable; falling back to rules")
            return None

        payload: Dict[str, Any] = {
            "model": choice.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.1,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        url = self.settings.llm_base_url.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {self.settings.llm_api_key}"}

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(url, json=payload, headers=headers)
                response.raise_for_status()
                data = response.json()
            log.info("model call ok", extra_fields={"model": choice.model})
            return data["choices"][0]["message"]["content"]
        except Exception as exc:
            # Offline mode: never crash, always fall back.
            log.info("model call failed, falling back", extra_fields={"error": str(exc)})
            return None

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
