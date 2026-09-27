"""Tool registry (section 7).

Every capability JARVIS can perform is declared here as a :class:`ToolSpec`
bound to an async handler. The planner may only emit tool names that exist in
this registry, and the policy engine reads the spec to decide whether the call
is allowed.
"""

from __future__ import annotations

import inspect
from typing import Any, Awaitable, Callable, Dict, List, Optional

from ..core.models import ActionResult, RiskLevel, ToolSpec
from ..logging_setup import get_logger

log = get_logger("registry.tools")

# A handler receives (ctx, **params) and returns an ActionResult.
ToolHandler = Callable[..., Awaitable[ActionResult]]


class ToolRegistry:
    """In-memory registry mapping tool names to specs and handlers."""

    def __init__(self) -> None:
        self._specs: Dict[str, ToolSpec] = {}
        self._handlers: Dict[str, ToolHandler] = {}

    # -- registration --------------------------------------------------------
    def register(self, spec: ToolSpec, handler: ToolHandler) -> None:
        if not callable(handler):
            raise TypeError(f"handler for {spec.name!r} must be callable")
        if spec.name in self._specs:
            log.info("tool re-registered", extra_fields={"tool": spec.name})
        self._specs[spec.name] = spec
        self._handlers[spec.name] = handler

    def register_many(self, items: List[tuple[ToolSpec, ToolHandler]]) -> None:
        for spec, handler in items:
            self.register(spec, handler)

    # -- lookup --------------------------------------------------------------
    def get(self, name: str) -> Optional[ToolSpec]:
        return self._specs.get(name)

    def handler(self, name: str) -> Optional[ToolHandler]:
        return self._handlers.get(name)

    def has(self, name: str) -> bool:
        return name in self._specs

    def all(self) -> List[ToolSpec]:
        return sorted(self._specs.values(), key=lambda s: (int(s.risk), s.name))

    def names(self) -> List[str]:
        return sorted(self._specs.keys())

    # -- validation ----------------------------------------------------------
    def validate_params(self, name: str, params: Dict[str, Any]) -> List[str]:
        """Return a list of human-readable parameter problems (empty == valid)."""
        spec = self._specs.get(name)
        if spec is None:
            return [f"unknown tool {name!r}"]

        problems: List[str] = []
        for required in spec.required_parameters:
            if required not in params or params[required] in (None, ""):
                problems.append(f"missing required parameter {required!r}")
        for key in params:
            if spec.parameters and key not in spec.parameters:
                problems.append(f"unexpected parameter {key!r}")
        return problems

    def describe_for_prompt(self) -> str:
        """Compact tool catalogue used when prompting an LLM planner."""
        lines = []
        for spec in self.all():
            params = ", ".join(f"{k}:{v}" for k, v in spec.parameters.items()) or "none"
            lines.append(
                f"- {spec.name}({params}) risk={spec.risk_label()} "
                f"confirm={spec.requires_confirmation} :: {spec.description}"
            )
        return "\n".join(lines)


def spec(
    name: str,
    description: str,
    *,
    risk: RiskLevel = RiskLevel.READ,
    requires_confirmation: bool = False,
    reversible: bool = True,
    allowed_devices: Optional[List[str]] = None,
    parameters: Optional[Dict[str, str]] = None,
    required: Optional[List[str]] = None,
    agent: str = "laptop",
    tags: Optional[List[str]] = None,
) -> ToolSpec:
    """Ergonomic helper for declaring a :class:`ToolSpec`."""
    return ToolSpec(
        name=name,
        description=description,
        agent=agent,
        risk=risk,
        requires_confirmation=requires_confirmation,
        reversible=reversible,
        allowed_devices=allowed_devices or ["laptop_main"],
        parameters=parameters or {},
        required_parameters=required or [],
        tags=tags or [],
    )


def signature_params(func: Callable[..., Any]) -> List[str]:
    """Return non-context parameter names of a handler (handy in tests)."""
    return [
        p.name
        for p in inspect.signature(func).parameters.values()
        if p.name not in {"ctx", "self"}
    ]
