"""Coding tools (v1.1).

Workflow: ``Inspect → Plan → Permission → Modify → Test → Review → Report →
Optional commit``. Proposing is separate from applying, and applying is
confirmed by the permission engine.

The safety rules the plan sets out, and how each is met:

* *never execute arbitrary unrestricted shell commands* — tests and commits go
  through ``run_allowlisted_command``; this package has no execution path
* *never delete the repository automatically* — there is no delete path at all;
  edits replace content with a backup
* *never modify protected paths* — every path goes through the workspace boundary
* *never install arbitrary software without permission* — no package manager is
  ever invoked
* *never push code remotely without authorization* — push is not implemented
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..core.models import RiskLevel, ToolSpec
from ..logging_setup import get_logger
from ..coding.changes import apply, propose
from ..coding.report import build_report
from ..coding.repo import describe, inspect, search
from ..coding.validation import plan, run_tests
from ..coding.workspace import get_workspace_registry
from .base import ToolContext, fail, ok

log = get_logger("tools.coding")


def _registry():
    return get_workspace_registry()


def _workspace(ctx: ToolContext, ref: str):
    """Resolve a workspace, or None. Names are never invented."""
    if not ref:
        return None
    return _registry().resolve(ref)


# ---------------------------------------------------------------------------
# Workspace lifecycle
# ---------------------------------------------------------------------------
async def open_workspace(ctx: ToolContext, path: str = "", name: str = "") -> Any:
    """Open a directory as a controlled workspace."""
    if not path:
        return fail("open_workspace", "Which directory should I open?", device=ctx.device_id)

    workspace = _registry().open(path, name=name)
    if workspace is None:
        return fail(
            "open_workspace",
            (
                f"I could not open {path!r}. It must be a real directory that is not "
                "protected."
            ),
            device=ctx.device_id,
        )

    return ok(
        "open_workspace",
        f"Opened {workspace.display} as a workspace ({workspace.workspace_id}).",
        device=ctx.device_id,
        workspace_id=workspace.workspace_id,
        root=str(workspace.root),
    )


async def list_workspaces(ctx: ToolContext) -> Any:
    """List open workspaces."""
    workspaces = _registry().all()
    if not workspaces:
        return ok(
            "list_workspaces",
            "No workspaces are open.",
            device=ctx.device_id,
            workspaces=[],
        )
    return ok(
        "list_workspaces",
        f"{len(workspaces)} workspace(s) open.",
        device=ctx.device_id,
        workspaces=[
            {"workspace_id": w.workspace_id, "name": w.display, "root": str(w.root)}
            for w in workspaces
        ],
    )


async def close_workspace(ctx: ToolContext, workspace_id: str = "") -> Any:
    """Close a workspace."""
    if not workspace_id:
        return fail("close_workspace", "Which workspace should I close?", device=ctx.device_id)
    if not _registry().close(workspace_id):
        return fail("close_workspace", f"No open workspace {workspace_id!r}.", device=ctx.device_id)
    return ok("close_workspace", "Closed.", device=ctx.device_id, workspace_id=workspace_id)


# ---------------------------------------------------------------------------
# Inspect and search
# ---------------------------------------------------------------------------
async def inspect_workspace(ctx: ToolContext, workspace_id: str = "") -> Any:
    """Describe a workspace's structure."""
    workspace = _workspace(ctx, workspace_id)
    if workspace is None:
        return fail(
            "inspect_workspace",
            f"I have no open workspace {workspace_id!r}.",
            device=ctx.device_id,
        )

    info = inspect(workspace)
    return ok(
        "inspect_workspace",
        describe(workspace),
        device=ctx.device_id,
        inspection=info,
    )


async def search_code(
    ctx: ToolContext,
    workspace_id: str = "",
    query: str = "",
    limit: int = 20,
) -> Any:
    """Search source files inside a workspace."""
    workspace = _workspace(ctx, workspace_id)
    if workspace is None:
        return fail("search_code", f"I have no open workspace {workspace_id!r}.", device=ctx.device_id)
    if not query:
        return fail("search_code", "What should I search for?", device=ctx.device_id)

    results = search(workspace, query, limit=max(1, min(int(limit), 100)))
    if not results:
        return ok(
            "search_code",
            f"No matches for {query!r} in {workspace.display}.",
            device=ctx.device_id,
            results=[],
        )

    first = results[0]
    return ok(
        "search_code",
        f"{len(results)} match(es). First: {first['path']}:{first['line']}",
        device=ctx.device_id,
        results=results,
    )


# ---------------------------------------------------------------------------
# Modify
# ---------------------------------------------------------------------------
async def propose_code_change(
    ctx: ToolContext,
    workspace_id: str = "",
    path: str = "",
    old: str = "",
    new: str = "",
) -> Any:
    """Show what a change would do. Writes nothing."""
    workspace = _workspace(ctx, workspace_id)
    if workspace is None:
        return fail(
            "propose_code_change",
            f"I have no open workspace {workspace_id!r}.",
            device=ctx.device_id,
        )
    if not path:
        return fail("propose_code_change", "Which file should I change?", device=ctx.device_id)

    change = propose(workspace, path, old, new)
    if not change.applies:
        return fail("propose_code_change", change.reason, device=ctx.device_id)

    return ok(
        "propose_code_change",
        f"Proposed change to {change.relative} ({change.occurrences} occurrence(s)). Nothing written yet.",
        device=ctx.device_id,
        change=change.as_dict(),
    )


async def apply_code_change(
    ctx: ToolContext,
    workspace_id: str = "",
    path: str = "",
    old: str = "",
    new: str = "",
) -> Any:
    """Apply a change, with a backup. Confirmed by the permission engine."""
    workspace = _workspace(ctx, workspace_id)
    if workspace is None:
        return fail(
            "apply_code_change", f"I have no open workspace {workspace_id!r}.", device=ctx.device_id
        )
    if not path:
        return fail("apply_code_change", "Which file should I change?", device=ctx.device_id)

    change = propose(workspace, path, old, new)
    if not change.applies:
        return fail("apply_code_change", change.reason, device=ctx.device_id)

    outcome = apply(workspace, change)
    if not outcome.get("ok"):
        return fail("apply_code_change", outcome.get("error", "could not apply"), device=ctx.device_id)

    return ok(
        "apply_code_change",
        f"Changed {change.relative}"
        + (f" (backup at {outcome['backup']})" if outcome.get("backup") else ""),
        device=ctx.device_id,
        change=outcome,
    )


# ---------------------------------------------------------------------------
# Test and report
# ---------------------------------------------------------------------------
async def run_workspace_tests(ctx: ToolContext, workspace_id: str = "", runner: str = "") -> Any:
    """Run the workspace's tests through the allowlisted command path."""
    workspace = _workspace(ctx, workspace_id)
    if workspace is None:
        return fail(
            "run_workspace_tests", f"I have no open workspace {workspace_id!r}.", device=ctx.device_id
        )

    outcome = await run_tests(workspace, ctx=ctx, runner=runner)
    if not outcome.get("ok"):
        # Not a hard failure to report: an unallowlisted runner is a policy
        # state, and the user needs to know rather than see a bare error.
        return ok(
            "run_workspace_tests",
            outcome.get("detail", "tests did not run"),
            device=ctx.device_id,
            outcome=outcome["outcome"],
            plan=outcome.get("plan"),
        )

    return ok(
        "run_workspace_tests",
        f"Tests passed: {outcome.get('detail', '')[:120]}",
        device=ctx.device_id,
        outcome=outcome["outcome"],
        plan=outcome.get("plan"),
    )


async def coding_report(ctx: ToolContext, workspace_id: str = "") -> Any:
    """Generate a workspace report."""
    workspace = _workspace(ctx, workspace_id)
    if workspace is None:
        return fail("coding_report", f"I have no open workspace {workspace_id!r}.", device=ctx.device_id)

    report = build_report(workspace, settings=ctx.settings)
    return ok(
        "coding_report",
        report["text"],
        device=ctx.device_id,
        inspection=report["inspection"],
        validation=report["validation"],
    )


async def commit_workspace(
    ctx: ToolContext,
    workspace_id: str = "",
    message: str = "",
    authorized: bool = False,
) -> Any:
    """Commit changes. Requires ``authorized=True`` — it is never implicit.

    Push is deliberately not implemented: the plan requires remote actions to be
    authorised, and there is no push path here at all.
    """
    workspace = _workspace(ctx, workspace_id)
    if workspace is None:
        return fail("commit_workspace", f"I have no open workspace {workspace_id!r}.", device=ctx.device_id)

    if not authorized:
        return fail(
            "commit_workspace",
            "I will not commit until you explicitly authorise it. Say so and pass "
            "authorized=true.",
            device=ctx.device_id,
        )
    if not message:
        return fail("commit_workspace", "What should the commit message be?", device=ctx.device_id)

    from ..tools.system import run_allowlisted_command

    allowlist = list(ctx.settings.allowed_commands) + list(ctx.settings.powerful_commands)
    if "git" not in allowlist:
        return fail(
            "commit_workspace",
            (
                "git is not on the command allowlist, so I cannot commit. I will not "
                "add it myself — add it to JARVIS_ALLOWED_COMMANDS if you want this."
            ),
            device=ctx.device_id,
        )

    add = await run_allowlisted_command(ctx, command="git", args=f"-C {workspace.root} add -A")
    if not add.ok:
        return fail("commit_workspace", f"could not stage: {add.summary}", device=ctx.device_id)

    commit = await run_allowlisted_command(
        ctx, command="git", args=f"-C {workspace.root} commit -m {message!r}"
    )
    if not commit.ok:
        return fail("commit_workspace", f"could not commit: {commit.summary}", device=ctx.device_id)

    log.info("workspace committed", extra_fields={"workspace": workspace.display})
    return ok(
        "commit_workspace",
        f"Committed in {workspace.display}. Nothing was pushed.",
        device=ctx.device_id,
        message=message,
        pushed=False,
    )


SPECS = [
    (
        ToolSpec(
            name="open_workspace",
            description="Open a directory as a controlled workspace.",
            agent="coding",
            risk=RiskLevel.LOW,
            reversible=True,
            allowed_devices=["laptop_main"],
            parameters={"path": "string", "name": "string"},
            required_parameters=["path"],
            tags=["coding", "workspace"],
        ),
        open_workspace,
    ),
    (
        ToolSpec(
            name="list_workspaces",
            description="List open workspaces.",
            agent="coding",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={},
            tags=["coding", "read"],
        ),
        list_workspaces,
    ),
    (
        ToolSpec(
            name="close_workspace",
            description="Close a workspace.",
            agent="coding",
            risk=RiskLevel.LOW,
            reversible=True,
            allowed_devices=["laptop_main"],
            parameters={"workspace_id": "string"},
            required_parameters=["workspace_id"],
            tags=["coding", "workspace"],
        ),
        close_workspace,
    ),
    (
        ToolSpec(
            name="inspect_workspace",
            description="Describe a workspace's structure and languages.",
            agent="coding",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={"workspace_id": "string"},
            required_parameters=["workspace_id"],
            tags=["coding", "read"],
        ),
        inspect_workspace,
    ),
    (
        ToolSpec(
            name="search_code",
            description="Search source files inside a workspace.",
            agent="coding",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={"workspace_id": "string", "query": "string", "limit": "int"},
            required_parameters=["workspace_id", "query"],
            tags=["coding", "read"],
        ),
        search_code,
    ),
    (
        ToolSpec(
            name="propose_code_change",
            description="Show a diff for a change. Writes nothing.",
            agent="coding",
            risk=RiskLevel.LOW,
            reversible=True,
            allowed_devices=["laptop_main"],
            parameters={"workspace_id": "string", "path": "string", "old": "string", "new": "string"},
            required_parameters=["workspace_id", "path", "old", "new"],
            tags=["coding"],
        ),
        propose_code_change,
    ),
    (
        ToolSpec(
            name="apply_code_change",
            description="Apply a change with a backup. Requires confirmation.",
            agent="coding",
            risk=RiskLevel.EXTERNAL,
            requires_confirmation=True,
            reversible=True,
            allowed_devices=["laptop_main"],
            parameters={"workspace_id": "string", "path": "string", "old": "string", "new": "string"},
            required_parameters=["workspace_id", "path", "old", "new"],
            tags=["coding", "write"],
        ),
        apply_code_change,
    ),
    (
        ToolSpec(
            name="run_workspace_tests",
            description="Run the workspace's tests via the allowlisted command path.",
            agent="coding",
            risk=RiskLevel.EXTERNAL,
            requires_confirmation=True,
            reversible=True,
            allowed_devices=["laptop_main"],
            parameters={"workspace_id": "string", "runner": "string"},
            required_parameters=["workspace_id"],
            tags=["coding", "validation"],
        ),
        run_workspace_tests,
    ),
    (
        ToolSpec(
            name="coding_report",
            description="Generate a workspace report.",
            agent="coding",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={"workspace_id": "string"},
            required_parameters=["workspace_id"],
            tags=["coding", "read"],
        ),
        coding_report,
    ),
    (
        ToolSpec(
            name="commit_workspace",
            description="Commit changes when explicitly authorised. Never pushes.",
            agent="coding",
            risk=RiskLevel.DESTRUCTIVE,
            requires_confirmation=True,
            reversible=False,
            allowed_devices=["laptop_main"],
            parameters={"workspace_id": "string", "message": "string", "authorized": "bool"},
            required_parameters=["workspace_id", "message"],
            tags=["coding", "git", "destructive"],
        ),
        commit_workspace,
    ),
]
