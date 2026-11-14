"""Allowlisted MCP tools, registered as Vantage tools.

The connector (``integrations/mcp_client``) owns the transport and the
allowlist. This module is the thin adapter that makes each allowlisted tool
addressable by the planner and the phone, without widening what may happen:

* One tool is registered **per allowlisted name**, and none at all otherwise.
  The registry therefore never gains a capability the person did not write
  down, and ``/tools`` cannot show a server's whole catalogue.
* Every one is ``RiskLevel.EXTERNAL``. An MCP tool can send mail, create a
  ticket, post a message -- Vantage cannot know which, so it assumes the
  worst tier that still has a confirmation. The policy engine then does its
  normal work: a confirmation card, and the rule that a phone may not approve
  its own request.
* The argument object is passed through as ``arguments``. Vantage does not
  validate it against a schema it learned at runtime: a stale schema would
  block a valid call, and a fabricated one is worse. The server is the
  authority on its own arguments, and its refusal is reported as a refusal.

The tool names are stable: ``mcp__<server>__<tool>``. That shape cannot
collide with a built-in name, and it makes the server obvious in the approval
card a person reads.
"""

from __future__ import annotations

from typing import Any, Dict, List

from ..core.models import RiskLevel, ToolSpec
from ..integrations import mcp_client
from .base import ToolContext, fail, ok

def tool_name(server: str, remote: str) -> str:
    """The Vantage name for one remote tool."""
    clean_server = "".join(c if c.isalnum() or c in "_-" else "_" for c in (server or "mcp"))
    clean_remote = "".join(c if c.isalnum() or c in "_-" else "_" for c in (remote or "tool"))
    return f"mcp__{clean_server}__{clean_remote}"

def _connector(ctx: ToolContext):
    """The process connector, else one built from these settings."""
    return mcp_client.active_connector() or mcp_client.connector_from_settings(ctx.settings)

def _caller(remote: str):
    """A handler that forwards to one named remote tool."""

    async def handler(ctx: ToolContext, arguments: Dict[str, Any] = None,
                      **kwargs: Any) -> Any:
        connector = _connector(ctx)
        args: Dict[str, Any] = dict(arguments or {})
        # A model that already knows the remote schema sometimes sends the
        # remote's own parameter names directly. Fold them in rather than
        # discarding them, but do not let the two spellings disagree silently.
        for key, value in kwargs.items():
            args.setdefault(key, value)
        try:
            result = connector.call_tool(remote, args)
        except mcp_client.MCPError as exc:
            return fail(f"mcp__{remote}", str(exc), device=ctx.device_id,
                        provider="mcp", state=exc.state.value, verified=False)
        text = mcp_client.result_text(result)
        return ok(tool_name(connector.config.name, remote),
                  text or f"{remote} returned no text.",
                  device=ctx.device_id, provider="mcp", remote_tool=remote,
                  structured=result.get("structuredContent"),
                  # The *call* succeeded; whether the world changed is the
                  # server's claim, not something Vantage observed. Reporting
                  # verified=True here would overstate a tuple of text from a
                  # remote system -- the same "200 is not proof" trap this
                  # project has hit before.
                  verified=False)

    return handler

def specs_for(connector: mcp_client.MCPConnector) -> List[tuple]:
    """One ``(ToolSpec, handler)`` per allowlisted tool. Empty when not configured."""
    if not connector.config.configured:
        return []
    out: List[tuple] = []
    for remote in connector.config.allowed_tools:
        name = tool_name(connector.config.name, remote)
        out.append((
            ToolSpec(
                name=name,
                description=(f"Call the MCP tool {remote!r} on "
                             f"{connector.config.name}. It runs on the remote server "
                             "and needs approval."),
                agent="integrations",
                risk=RiskLevel.EXTERNAL,
                requires_confirmation=True,
                reversible=False,
                allowed_devices=["laptop_main"],
                parameters={"arguments": "object"},
                tags=["mcp", "external", "write"],
            ),
            _caller(remote),
        ))
    return out