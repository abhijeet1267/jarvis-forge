"""Validation (v1.1).

Running tests means invoking an interpreter, and the plan is explicit that the
Coding Agent must **never execute arbitrary unrestricted shell commands** and
must **never install arbitrary software**.

So this module does not have its own execution path. It delegates to the
existing ``run_allowlisted_command`` tool, which is already gated by the
allowlist and requires confirmation. If the test runner is not allowlisted, this
reports that plainly and says how to opt in — it does **not** silently add the
interpreter to the allowlist, because doing so would make the allowlist
decorative (see the comment in ``config.py``).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..logging_setup import get_logger
from .repo import inspect
from .workspace import Workspace

log = get_logger("coding.validation")

#: Candidate runners in preference order, with the args to use.
DEFAULT_RUNNERS = [
    ("pytest", "-q"),
    ("npm", "test"),
    ("cargo", "test"),
    ("go", "test ./..."),
]


def _allowlist(settings) -> List[str]:
    return list(getattr(settings, "allowed_commands", []) or []) + list(
        getattr(settings, "powerful_commands", []) or []
    )


def plan(workspace: Workspace, settings=None, runner: str = "") -> Dict[str, Any]:
    """Work out what running tests would involve, without running anything."""
    info = inspect(workspace)

    if runner:
        chosen = runner.strip()
        args = ""
        for name, default_args in DEFAULT_RUNNERS:
            if chosen == name:
                args = default_args
                break
        return {
            "runner": chosen,
            "args": args,
            "allowlisted": chosen in _allowlist(settings),
            "workspace": workspace.display,
        }

    allowlisted = _allowlist(settings)
    for name, args in DEFAULT_RUNNERS:
        if name in allowlisted:
            return {
                "runner": name,
                "args": args,
                "allowlisted": True,
                "workspace": workspace.display,
            }

    suggested = info.get("likely_test_runners") or [
        name for name, _ in DEFAULT_RUNNERS
    ]
    return {
        "runner": suggested[0] if suggested else "pytest",
        "args": dict(DEFAULT_RUNNERS).get(suggested[0], "") if suggested else "-q",
        "allowlisted": False,
        "workspace": workspace.display,
        "note": (
            "No test runner is on the command allowlist. I will not add one "
            "myself — that would make the allowlist decorative. Add it to "
            "JARVIS_POWERFUL_COMMANDS if you want me to run tests here."
        ),
    }


async def run_tests(
    workspace: Workspace,
    ctx=None,
    runner: str = "",
) -> Dict[str, Any]:
    """Run the workspace's tests through the allowlisted command path."""
    from ..tools.system import run_allowlisted_command

    if ctx is None:
        return {
            "ok": False,
            "outcome": "cannot_run",
            "detail": "I need a tool context to run tests.",
        }

    plan_result = plan(workspace, ctx.settings, runner=runner)

    if not plan_result["allowlisted"]:
        return {
            "ok": False,
            "outcome": "not_allowlisted",
            "detail": plan_result.get("note", f"{plan_result['runner']!r} is not allowlisted."),
            "plan": plan_result,
        }

    result = await run_allowlisted_command(
        ctx, command=plan_result["runner"], args=plan_result["args"]
    )

    if not result.ok:
        return {
            "ok": False,
            "outcome": "failed",
            "detail": result.summary,
            "plan": plan_result,
        }

    return {
        "ok": True,
        "outcome": "passed",
        "detail": result.message,
        "plan": plan_result,
    }


def describe_validation(workspace: Workspace, settings=None) -> Dict[str, Any]:
    return {"workspace": workspace.display, **plan(workspace, settings)}
