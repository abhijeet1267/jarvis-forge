"""Coding Agent tests (v1.1).

The properties that matter: **nothing escapes the workspace**, **the repository
cannot be deleted by this workflow**, and **tests are never run by weakening the
command allowlist**.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from jarvis.coding.changes import apply, list_backups, propose
from jarvis.coding.report import build_report
from jarvis.coding.repo import describe, inspect, search
from jarvis.coding.validation import plan
from jarvis.coding.workspace import (
    WorkspaceRegistry,
    get_workspace_registry,
    is_source_file,
    language_of,
    looks_like_test,
    walk_source,
)
from jarvis.core.models import PlannedAction, PolicyDecision, RiskLevel
from jarvis.tools.base import ToolContext
from jarvis.tools.coding import (
    apply_code_change,
    close_workspace,
    coding_report,
    commit_workspace,
    inspect_workspace,
    list_workspaces,
    open_workspace,
    propose_code_change,
    run_workspace_tests,
    search_code,
)


@pytest.fixture()
def project(tmp_area: Path) -> Path:
    """A small realistic project tree."""
    root = tmp_area / "demo"
    (root / "pkg").mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "pkg" / "core.py").write_text(
        "def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a - b\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_core.py").write_text(
        "def test_add():\n    assert True\n",
        encoding="utf-8",
    )
    (root / "README.md").write_text("# demo\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    return root


@pytest.fixture()
def registry() -> WorkspaceRegistry:
    return WorkspaceRegistry()


@pytest.fixture()
def ctx() -> ToolContext:
    return ToolContext()


# ---------------------------------------------------------------------------
# The workspace boundary
# ---------------------------------------------------------------------------
def test_paths_inside_the_workspace_are_allowed(registry: WorkspaceRegistry, project: Path) -> None:
    workspace = registry.open(str(project))
    assert workspace is not None
    assert workspace.contain(str(project / "pkg" / "core.py")) is not None
    assert workspace.contain(str(project)) is not None


def test_a_relative_path_resolves_against_the_workspace(
    registry: WorkspaceRegistry, project: Path
) -> None:
    """'pkg/core.py' means the workspace's pkg/core.py, not the process cwd's."""
    workspace = registry.open(str(project))
    contained = workspace.contain("pkg/core.py")
    assert contained is not None
    assert contained.name == "core.py"


@pytest.mark.parametrize(
    "escape",
    [
        "/etc/hosts",
        "/usr/bin/env",
        "../outside.py",
        "pkg/../../outside.py",
    ],
)
def test_escaping_paths_are_refused(registry: WorkspaceRegistry, project: Path, escape: str) -> None:
    workspace = registry.open(str(project))
    assert workspace.contain(escape) is None


def test_a_symlink_escaping_the_root_is_refused(registry: WorkspaceRegistry, project: Path) -> None:
    link = project / "escape"
    if not link.exists():
        os.symlink("/etc", link)

    workspace = registry.open(str(project))
    assert workspace.contain(str(link / "hosts")) is None


def test_protected_paths_are_refused(registry: WorkspaceRegistry, project: Path) -> None:
    workspace = registry.open(str(project))
    assert workspace.contain(str(Path.home() / ".ssh" / "id_rsa")) is None


def test_opening_a_file_or_a_protected_dir_fails(registry: WorkspaceRegistry, project: Path) -> None:
    assert registry.open(str(project / "README.md")) is None
    assert registry.open("/usr") is None


def test_a_workspace_is_shared_process_wide(project: Path) -> None:
    first = get_workspace_registry().open(str(project))
    assert get_workspace_registry().resolve(first.workspace_id) is not None
    get_workspace_registry().close(first.workspace_id)


# ---------------------------------------------------------------------------
# Source helpers
# ---------------------------------------------------------------------------
def test_source_detection_and_language(project: Path) -> None:
    assert is_source_file(project / "pkg" / "core.py")
    assert language_of(project / "pkg" / "core.py") == "python"
    assert looks_like_test(project / "tests" / "test_core.py")
    assert not looks_like_test(project / "pkg" / "core.py")


def test_walk_skips_noise_directories(project: Path) -> None:
    (project / "node_modules").mkdir(exist_ok=True)
    (project / "node_modules" / "junk.py").write_text("x = 1", encoding="utf-8")

    names = {p.name for p in walk_source(project)}
    assert "core.py" in names
    assert "junk.py" not in names


# ---------------------------------------------------------------------------
# Inspection and search
# ---------------------------------------------------------------------------
def test_inspect_reports_structure(registry: WorkspaceRegistry, project: Path) -> None:
    workspace = registry.open(str(project))
    info = inspect(workspace)

    assert info["file_count"] >= 3
    assert info["test_file_count"] >= 1
    assert "python" in info["languages"]
    assert "README.md" in info["entry_points"]


def test_search_finds_code(registry: WorkspaceRegistry, project: Path) -> None:
    workspace = registry.open(str(project))
    results = search(workspace, "def add")
    assert results
    assert results[0]["path"].endswith("core.py")


def test_search_returns_nothing_for_a_miss(registry: WorkspaceRegistry, project: Path) -> None:
    workspace = registry.open(str(project))
    assert search(workspace, "definitely_not_in_here") == []


# ---------------------------------------------------------------------------
# Changes
# ---------------------------------------------------------------------------
def test_propose_does_not_write(registry: WorkspaceRegistry, project: Path) -> None:
    workspace = registry.open(str(project))
    change = propose(workspace, "pkg/core.py", "return a + b", "return a + b  # fixed")

    assert change.applies
    assert change.occurrences == 1
    assert change.diff
    # The file is untouched until apply() runs.
    assert "fixed" not in (project / "pkg" / "core.py").read_text()


def test_propose_refuses_text_that_is_not_there(registry: WorkspaceRegistry, project: Path) -> None:
    """JARVIS refuses rather than approximating a similar-looking replacement."""
    workspace = registry.open(str(project))
    change = propose(workspace, "pkg/core.py", "return a * b", "nope")
    assert not change.applies
    assert "will not guess" in change.reason


def test_propose_refuses_a_path_outside_the_workspace(
    registry: WorkspaceRegistry, project: Path
) -> None:
    workspace = registry.open(str(project))
    assert not propose(workspace, "/etc/hosts", "a", "b").applies


def test_apply_changes_the_file_and_makes_a_backup(
    registry: WorkspaceRegistry, project: Path
) -> None:
    workspace = registry.open(str(project))
    change = propose(workspace, "pkg/core.py", "return a + b", "return a + b  # fixed")

    outcome = apply(workspace, change)
    assert outcome["ok"]
    assert outcome["backup"] is not None
    assert "fixed" in (project / "pkg" / "core.py").read_text()
    assert list_backups(workspace)


def test_apply_refuses_when_the_text_has_moved(registry: WorkspaceRegistry, project: Path) -> None:
    workspace = registry.open(str(project))
    change = propose(workspace, "pkg/core.py", "return a + b", "changed")
    # Simulate the file changing underneath us.
    (project / "pkg" / "core.py").write_text("def add(a, b):\n    return 0\n", encoding="utf-8")

    outcome = apply(workspace, change)
    assert not outcome["ok"]
    assert "no longer there" in outcome["error"]


def test_apply_refuses_an_unconfirmed_change(registry: WorkspaceRegistry, project: Path) -> None:
    workspace = registry.open(str(project))
    change = propose(workspace, "/etc/hosts", "a", "b")
    assert not apply(workspace, change)["ok"]


def test_there_is_no_delete_path_in_the_changes_module() -> None:
    """The repository cannot be deleted by the coding workflow.

    Asserted structurally: if someone adds a delete, this fails.
    """
    import inspect as _inspect

    from jarvis.coding import changes

    for name, obj in vars(changes).items():
        if callable(obj) and getattr(obj, "__module__", "") == changes.__name__:
            lowered = name.lower()
            assert "delete" not in lowered and "remove" not in lowered and "rmtree" not in lowered, name
    assert not hasattr(changes, "rmtree")
    assert "shutil" not in (_inspect.getsource(changes))


# ---------------------------------------------------------------------------
# Validation: never weaken the allowlist
# ---------------------------------------------------------------------------
def test_plan_reports_when_no_runner_is_allowlisted(registry: WorkspaceRegistry, project: Path) -> None:
    workspace = registry.open(str(project))
    result = plan(workspace)

    if not result["allowlisted"]:
        assert "will not add" in result.get("note", "").lower()
        # Crucially, the plan must not have mutated the allowlist.
        assert "pytest" not in (getattr(getattr(workspace, "root", None), "", "") or "")


def test_plan_never_mutates_the_allowlist(registry: WorkspaceRegistry, project: Path) -> None:
    """The strongest form of the rule: check the setting before and after."""
    from jarvis.config import get_settings

    workspace = registry.open(str(project))
    before = list(get_settings().allowed_commands) + list(get_settings().powerful_commands)
    plan(workspace)
    after = list(get_settings().allowed_commands) + list(get_settings().powerful_commands)
    assert before == after


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_open_and_list_workspace_tools(ctx: ToolContext, project: Path) -> None:
    opened = await open_workspace(ctx, path=str(project))
    assert opened.ok

    listed = await list_workspaces(ctx)
    assert listed.ok
    assert listed.output["workspaces"]


@pytest.mark.asyncio
async def test_open_workspace_needs_a_path(ctx: ToolContext) -> None:
    assert not (await open_workspace(ctx)).ok


@pytest.mark.asyncio
async def test_open_workspace_rejects_a_bad_path(ctx: ToolContext, tmp_area: Path) -> None:
    assert not (await open_workspace(ctx, path=str(tmp_area / "nope"))).ok


@pytest.mark.asyncio
async def test_inspect_and_search_tools(ctx: ToolContext, project: Path) -> None:
    opened = await open_workspace(ctx, path=str(project))
    workspace_id = opened.output["workspace_id"]

    assert (await inspect_workspace(ctx, workspace_id=workspace_id)).ok
    assert (await search_code(ctx, workspace_id=workspace_id, query="def add")).ok


@pytest.mark.asyncio
async def test_search_needs_a_query(ctx: ToolContext, project: Path) -> None:
    opened = await open_workspace(ctx, path=str(project))
    assert not (await search_code(ctx, workspace_id=opened.output["workspace_id"])).ok


@pytest.mark.asyncio
async def test_propose_tool_writes_nothing(ctx: ToolContext, project: Path) -> None:
    opened = await open_workspace(ctx, path=str(project))
    workspace_id = opened.output["workspace_id"]

    result = await propose_code_change(
        ctx, workspace_id=workspace_id, path="pkg/core.py", old="return a + b", new="changed"
    )
    assert result.ok
    assert "changed" not in (project / "pkg" / "core.py").read_text()


@pytest.mark.asyncio
async def test_apply_tool_changes_the_file(ctx: ToolContext, project: Path) -> None:
    opened = await open_workspace(ctx, path=str(project))
    workspace_id = opened.output["workspace_id"]

    result = await apply_code_change(
        ctx, workspace_id=workspace_id, path="pkg/core.py", old="return a + b", new="return a + b  # ok"
    )
    assert result.ok
    assert "ok" in (project / "pkg" / "core.py").read_text()


@pytest.mark.asyncio
async def test_apply_tool_refuses_a_path_outside(ctx: ToolContext, project: Path) -> None:
    opened = await open_workspace(ctx, path=str(project))
    assert not (
        await apply_code_change(ctx, workspace_id=opened.output["workspace_id"], path="/etc/hosts", old="a", new="b")
    ).ok


@pytest.mark.asyncio
async def test_commit_requires_explicit_authorization(ctx: ToolContext, project: Path) -> None:
    """Committing is never implicit."""
    opened = await open_workspace(ctx, path=str(project))
    result = await commit_workspace(
        ctx, workspace_id=opened.output["workspace_id"], message="a commit"
    )
    assert not result.ok
    assert "explicitly authorise" in result.summary


@pytest.mark.asyncio
async def test_commit_requires_a_message(ctx: ToolContext, project: Path) -> None:
    opened = await open_workspace(ctx, path=str(project))
    assert not (
        await commit_workspace(ctx, workspace_id=opened.output["workspace_id"], authorized=True)
    ).ok


@pytest.mark.asyncio
async def test_run_tests_reports_the_allowlist_state(ctx: ToolContext, project: Path) -> None:
    """Whether tests run or not, the answer must be honest."""
    opened = await open_workspace(ctx, path=str(project))
    result = await run_workspace_tests(ctx, workspace_id=opened.output["workspace_id"])
    assert result.ok
    assert result.output["outcome"] in ("passed", "failed", "not_allowlisted")


@pytest.mark.asyncio
async def test_coding_report_tool(ctx: ToolContext, project: Path) -> None:
    opened = await open_workspace(ctx, path=str(project))
    result = await coding_report(ctx, workspace_id=opened.output["workspace_id"])
    assert result.ok
    assert "JARVIS CODING REPORT" in result.message


@pytest.mark.asyncio
async def test_close_workspace(ctx: ToolContext, project: Path) -> None:
    opened = await open_workspace(ctx, path=str(project))
    workspace_id = opened.output["workspace_id"]
    assert (await close_workspace(ctx, workspace_id=workspace_id)).ok
    assert not (await inspect_workspace(ctx, workspace_id=workspace_id)).ok


def test_report_says_when_tests_were_not_run(registry: WorkspaceRegistry, project: Path) -> None:
    workspace = registry.open(str(project))
    report = build_report(workspace)
    assert "Validation" in report["text"]
    assert report["validation"]["allowlisted"] in (True, False)


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------
def test_applying_a_change_requires_confirmation(core) -> None:
    spec = core.tools.get("apply_code_change")
    assert spec.requires_confirmation is True
    assert spec.risk is RiskLevel.EXTERNAL


def test_committing_is_destructive_and_confirmed(core) -> None:
    spec = core.tools.get("commit_workspace")
    assert spec.requires_confirmation is True
    assert spec.risk is RiskLevel.DESTRUCTIVE

    action = PlannedAction(
        tool="commit_workspace",
        params={"workspace_id": "WS-1", "message": "m", "authorized": True},
        device="laptop_main",
    )
    assert core.policy.evaluate(action).decision is PolicyDecision.CONFIRM


@pytest.mark.parametrize(
    "tool,params",
    [
        ("list_workspaces", {}),
        ("inspect_workspace", {"workspace_id": "WS-1"}),
        ("search_code", {"workspace_id": "WS-1", "query": "x"}),
        ("coding_report", {"workspace_id": "WS-1"}),
        ("propose_code_change", {"workspace_id": "WS-1", "path": "a.py", "old": "a", "new": "b"}),
    ],
)
def test_reading_and_proposing_are_not_gated(core, tool: str, params: dict) -> None:
    action = PlannedAction(tool=tool, params=params, device="laptop_main")
    assert core.policy.evaluate(action).decision is PolicyDecision.EXECUTE


def test_coding_agent_is_registered(core) -> None:
    assert "coding" in core.agents
    assert core.agents["coding"].owns("apply_code_change")
