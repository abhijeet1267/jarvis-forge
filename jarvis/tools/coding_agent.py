"""Driving a coding agent from a sentence, through the gate.

Claude Code is a real program on this machine (``~/.local/bin/claude``, 2.1.260), so the ask
"give it the next instruction, continue, next prompt for that project" is answerable today
rather than left hypothetical. It is confirmation-gated for the same reason a light switch is:
run non-interactively it skips its own workspace-trust prompt and works with file access in
whatever directory it is pointed at, which is an action on the laptop, not a question to a
chatbot.

What it reports is the agent's own answer. An exit code of 0 with nothing parseable is
recorded as unverified, because "a process finished" and "the work happened" are the two
claims this project exists to keep apart.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..core.models import RiskLevel, ToolSpec
from ..logging_setup import get_logger
from .base import ToolContext, fail, ok, run_command, unverified

log = get_logger("tools.coding_agent")

TOOL = "run_coding_agent"

#: A coding agent is minutes, not milliseconds; the sentence budget covers network
#: reads, and this is a local process. Kept bounded anyway so a wedged agent cannot
#: hold a phone's request open indefinitely.
AGENT_TIMEOUT_SECONDS = 180.0

#: Where the CLI hides. ``claude`` is not on the PATH of a process started by
#: Finder, which is the difference between "works in my terminal" and "works".
CLI_LOCATIONS = ("claude", "~/.local/bin/claude", "~/.claude/local/claude",
                 "/usr/local/bin/claude", "/opt/homebrew/bin/claude")


def agent_cli_path() -> Optional[str]:
    found = shutil.which("claude")
    if found:
        return found
    for candidate in CLI_LOCATIONS[1:]:
        path = Path(candidate).expanduser()
        if path.exists():
            return str(path)
    return None


def _answer(payload: Any) -> Tuple[str, bool]:
    """(the agent's own words, whether it says it failed)."""
    if isinstance(payload, dict):
        text = str(payload.get("result") or payload.get("message") or "").strip()
        return text, bool(payload.get("is_error"))
    return (str(payload).strip(), False)


async def run_coding_agent(ctx: ToolContext, prompt: str = "", path: str = "",
                           resume: bool = False) -> Any:
    """Send a instruction to the coding agent and report what it answered."""
    asked = str(prompt).strip()
    if not asked:
        return fail(TOOL, "What should I ask it to do?", device=ctx.device_id)

    cli = agent_cli_path()
    if cli is None:
        return fail(
            TOOL,
            "Claude Code is not installed on this laptop, so there is nothing for me to "
            "give the instruction to. Install it and ask again.",
            device=ctx.device_id,
        )

    from .apps import resolve_folder

    root, problem = (Path.cwd(), None) if not str(path).strip() else resolve_folder(path, ctx)
    if problem:
        return fail(TOOL, problem, device=ctx.device_id)

    argv: List[str] = [cli, "-p", asked, "--output-format", "json", "--add-dir", str(root)]
    if resume:
        argv.insert(1, "--continue")
    # `--dangerously-skip-permissions` exists and is deliberately never added: the
    # confirmation that gated this call is the user's, and a second gate should not be
    # removed by the thing being gated.

    code, out, err = await run_command(argv, cwd=str(root), timeout=AGENT_TIMEOUT_SECONDS)

    payload = None
    try:
        payload = json.loads(out) if out.strip() else None
    except ValueError:
        payload = None

    if payload is None and code != 0:
        return fail(TOOL, err or "The coding agent returned nothing.", device=ctx.device_id)

    if payload is None:
        return unverified(
            TOOL,
            f"I sent {asked[:60]!r} to Claude Code and it exited without an answer I could "
            "read, so I cannot tell you what it did.",
            device=ctx.device_id, workspace=str(root),
        )

    said, errored = _answer(payload)
    detail = {
        "workspace": str(root),
        "turns": (payload or {}).get("num_turns") if isinstance(payload, dict) else None,
        "cost_usd": (payload or {}).get("total_cost_usd") if isinstance(payload, dict) else None,
    }

    # The agent exits 0 with an empty result when it could not reach its model.
    # Saying only "it failed" leaves a person with nothing to act on, so the
    # agent's own terminal reason is turned into a sentence naming the fix.
    reason = str((payload or {}).get("terminal_reason") or "") if isinstance(payload, dict) else ""
    if reason == "api_error" or (not said and not errored and not payload.get("num_turns")):
        return fail(
            TOOL,
            "Claude Code is installed but could not reach its model, so it did no work. "
            "Sign in to the CLI (run `claude` in a terminal) or set its API key, then try "
            "again. Nothing was changed in the workspace.",
            device=ctx.device_id, terminal_reason=reason, **detail)

    if errored or code != 0:
        return fail(TOOL, said or err or "The coding agent reported an error.",
                    device=ctx.device_id, **detail)

    return ok(TOOL, said or "The coding agent answered without saying anything.",
              device=ctx.device_id, verified=True, **detail)


SPECS: List[Tuple[ToolSpec, Any]] = [
    (
        ToolSpec(
            name=TOOL,
            description=("Give an instruction to the coding agent (Claude Code) inside a "
                         "workspace and report what it answers."),
            agent="coding",
            risk=RiskLevel.EXTERNAL,
            requires_confirmation=True,
            reversible=False,
            allowed_devices=["laptop_main"],
            parameters={"prompt": "string", "path": "string", "resume": "boolean"},
            required_parameters=["prompt"],
            tags=["coding", "agent", "external"],
        ),
        run_coding_agent,
    ),
]
