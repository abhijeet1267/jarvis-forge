"""n8n workflows as Vantage tools: list, run.

The adapter (``integrations/n8n``) owns configuration, the registry, validation
and execution. This module is the thin layer that makes those operations
addressable by the planner and the chat path, without widening what may happen:

* **One tool per registered workflow**, named ``n8n__<key>``. A workflow the
  registry does not contain does not exist as a tool, so a model cannot invoke
  an n8n workflow by guessing a name -- and cannot reach n8n's admin API at all,
  because no tool calls it.
* **Risk comes from the registry, not from the tool.** A read workflow is
  ``READ``; anything that leaves the machine is ``EXTERNAL`` with
  ``requires_confirmation``, so the existing permission engine asks and the
  phone still cannot approve its own request.
* **Arguments are the declared schema**, surfaced as typed parameters so the
  model knows what a workflow needs. Validation still happens in the adapter:
  a model that invents an extra parameter gets the adapter's refusal, not a
  half-validated call.
* **The result is the execution result.** ``emit_execution`` reports the
  adapter's own status; a timeout, a workflow failure and an unreachable server
  are three different ``ok``/``error`` outcomes here, never one "failed".
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..core.models import RiskLevel, ToolSpec
from ..integrations import n8n as n8n_mod
from ..integrations.n8n_client import ExecutionStatus, client_from_settings
from .base import ToolContext, fail, ok, unverified

def tool_name(key: str) -> str:
    return f"n8n__{key}"

def _level(spec: n8n_mod.WorkflowSpec) -> RiskLevel:
    return {
        n8n_mod.RiskClass.READ: RiskLevel.READ,
        n8n_mod.RiskClass.MUTATING: RiskLevel.EXTERNAL,
        n8n_mod.RiskClass.EXTERNAL: RiskLevel.EXTERNAL,
    }[spec.risk]

def _parameters(spec: n8n_mod.WorkflowSpec) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for field in spec.fields:
        if field.kind == "integer":
            out[field.name] = "int"
        elif field.kind == "boolean":
            out[field.name] = "bool"
        else:
            out[field.name] = "string"
    return out

def _runner(spec: n8n_mod.WorkflowSpec):
    """A handler that runs one registered workflow through the adapter."""

    async def handler(ctx: ToolContext, **kwargs: Any) -> Any:
        # A confirmation that already happened is the permission engine's; the
        # adapter's own `confirmed` flag is about *leaving the machine*, and an
        # approved tool call is exactly that. Everything a read workflow needs
        # is validated inside `execute`.
        inputs = {k: v for k, v in kwargs.items() if v is not None}
        result = client_from_settings(ctx.settings).execute(
            spec.key, inputs,
            confirmed=spec.risk.requires_confirmation)
        return emit_execution(result, device=ctx.device_id)

    return handler

def emit_execution(result: Any, *, device: str) -> Any:
    """Turn an ``ExecutionResult`` into a tool result without overstating it.

    Kept public so a test can assert the mapping without a network call, and so
    there is exactly one place that decides what each status means to the
    conversational layer.
    """
    payload = result.as_dict()
    if result.status is ExecutionStatus.SUCCEEDED:
        return ok(
            f"n8n__{result.workflow_key}", result.message, device=device,
            provider="n8n", workflow=result.workflow_key,
            correlation_id=result.correlation_id,
            output=result.output,
            # The workflow answered. Whether its *side effects* happened is the
            # workflow's claim; for a read that is the answer itself, for a write
            # it is not observed here. The registry's own `idempotent` flag is
            # the closest thing to "this only reads" that exists.
            verified=bool(getattr(result, "read_only", False)),
        )
    if result.status is ExecutionStatus.FAILED:
        return fail(
            f"n8n__{result.workflow_key}", result.message, device=device,
            provider="n8n", workflow=result.workflow_key,
            correlation_id=result.correlation_id,
            provider_status="failed", output=result.output,
        )
    if result.status is ExecutionStatus.TIMEOUT:
        return fail(
            f"n8n__{result.workflow_key}", result.message, device=device,
            provider="n8n", workflow=result.workflow_key,
            correlation_id=result.correlation_id,
            provider_status="timeout", retry_safe=result.retry_safe,
        )
    if result.status is ExecutionStatus.UNREACHABLE:
        return fail(
            f"n8n__{result.workflow_key}", result.message, device=device,
            provider="n8n", workflow=result.workflow_key,
            provider_status="unreachable",
        )
    # REFUSED: nothing left Vantage. Say that, so it is not read as an outage.
    return fail(
        f"n8n__{result.workflow_key}", result.message, device=device,
        provider="n8n", workflow=result.workflow_key,
        provider_status="refused",
    )

async def list_workflows(ctx: ToolContext) -> Any:
    """The approved workflows and what each one needs."""
    config = n8n_mod.config_from_settings(ctx.settings)
    entries = [dict(w, enabled=config.permits(w["key"])) for w in n8n_mod.catalog()]
    enabled = [e for e in entries if e["enabled"]]
    if not entries:
        return ok("list_n8n_workflows", "No n8n workflows are approved in Vantage.",
                  device=ctx.device_id, workflows=[], verified=True)
    if not enabled:
        return ok("list_n8n_workflows",
                  "n8n is configured, but none of the approved workflows is enabled.",
                  device=ctx.device_id, workflows=entries, verified=True)
    lines = []
    for e in enabled:
        needs = ", ".join(f["name"] for f in e["fields"])
        lines.append(f"• {e['title']} ({e['key']}): {e['description']}"
                     + (f" Needs: {needs}." if needs else ""))
    return ok("list_n8n_workflows", "\n".join(lines), device=ctx.device_id,
              workflows=entries, verified=True)

def specs_for() -> List[tuple]:
    """One ``(ToolSpec, handler)`` per registered workflow, plus the list tool.

    The registry is static, so this does not depend on n8n being reachable.
    A workflow that is disabled by the allowlist is still registered as a tool
    and refuses with a sentence when called — the alternative would make a
    capability appear and disappear with a setting, which the model cannot see.
    """
    specs: List[tuple] = [
        (
            ToolSpec(
                name="list_n8n_workflows",
                description="List the approved n8n automation workflows and the "
                            "inputs each one needs.",
                agent="automation",
                risk=RiskLevel.READ,
                allowed_devices=["laptop_main"],
                parameters={},
                tags=["n8n", "automation", "read"],
            ),
            list_workflows,
        )
    ]
    for spec in n8n_mod.REGISTRY:
        specs.append((
            ToolSpec(
                name=tool_name(spec.key),
                description=spec.description,
                agent="automation",
                risk=_level(spec),
                requires_confirmation=spec.risk.requires_confirmation,
                reversible=spec.idempotent,
                allowed_devices=["laptop_main"],
                parameters=_parameters(spec),
                required_parameters=[f.name for f in spec.fields if f.required],
                tags=["n8n", "automation", spec.risk.value],
            ),
            _runner(spec),
        ))
    return specs