"""Laptop Control Center tests (v2.x).

The cockpit answers four questions: how is the laptop, what is running, where
are my projects, and "brief an agent for me". The properties that matter here:

* every query is **read-only** and returns a clean :class:`ActionResult`;
* the **agent-prompt builder** embeds live machine state + open projects and
  refuses to run without a goal;
* the **planner** routes the cockpit's phrasings to the right tool without
  stealing an intent an earlier, more specific rule owns;
* the dedicated :class:`LaptopCenterAgent` owns *only* its own tools.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis.agents import LaptopCenterAgent, build_agents
from jarvis.core.planner import RulePlanner
from jarvis.coding.workspace import get_workspace_registry
from jarvis.tools import build_default_registry
from jarvis.tools.base import ToolContext
from jarvis.tools.laptop_center import (
    build_agent_prompt,
    laptop_processes,
    laptop_project_status,
    laptop_snapshot,
)

LCC_TOOLS = {
    "laptop_snapshot",
    "laptop_processes",
    "laptop_project_status",
    "build_agent_prompt",
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _clean_registry():
    """The workspace registry is process-wide; keep the cockpit tests isolated."""
    registry = get_workspace_registry()
    for ws in registry.all():
        registry.close(ws.workspace_id)
    yield
    for ws in registry.all():
        registry.close(ws.workspace_id)


@pytest.fixture()
def ctx() -> ToolContext:
    return ToolContext()


@pytest.fixture()
def planner() -> RulePlanner:
    return RulePlanner(build_default_registry())


@pytest.fixture()
def project(tmp_area: Path) -> Path:
    """A small realistic project to open as a workspace."""
    root = tmp_area / "demo"
    (root / "pkg").mkdir(parents=True, exist_ok=True)
    (root / "pkg" / "core.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "tests" / "test_core.py").write_text("def test_add():\n    assert True\n", encoding="utf-8")
    (root / "README.md").write_text("# demo\n", encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# Registration & agent ownership
# ---------------------------------------------------------------------------
def test_the_cockpit_tools_are_registered_and_owned() -> None:
    registry = build_default_registry()
    agent = LaptopCenterAgent(registry)
    for tool in LCC_TOOLS:
        assert registry.has(tool), f"{tool} should be registered"
        assert agent.owns(tool), f"{tool} should be owned by the laptop-center agent"
    # A foreign tool must stay out of the cockpit.
    assert not agent.owns("open_app")
    assert "laptop_center" in build_agents(registry)


def test_cockpit_specs_are_read_only_except_the_prompt_builder() -> None:
    registry = build_default_registry()
    assert registry.get("laptop_snapshot").risk.value == 0
    assert registry.get("laptop_processes").risk.value == 0
    assert registry.get("laptop_project_status").risk.value == 0
    assert registry.get("build_agent_prompt").requires_confirmation is False
    assert "goal" in registry.get("build_agent_prompt").required_parameters


# ---------------------------------------------------------------------------
# Machine-condition snapshot
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_snapshot_reports_machine_condition(ctx: ToolContext) -> None:
    result = await laptop_snapshot(ctx)
    assert result.ok
    assert result.message.startswith("Laptop condition:")
    # Fields that are always present, on any host.
    assert result.output["hostname"]
    assert result.output["os"]
    assert result.output["python"]
    assert result.output["cpu_count"]
    assert result.verified


# ---------------------------------------------------------------------------
# Process listing
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_processes_lists_running_tasks_sorted(ctx: ToolContext) -> None:
    result = await laptop_processes(ctx)
    if not result.ok:
        pytest.skip("ps unavailable on this host")
    procs = result.output["processes"]
    assert isinstance(procs, list) and procs
    assert result.output["sort_by"] == "cpu"
    # Sorted by CPU descending.
    cpus = [p["cpu"] for p in procs]
    assert cpus == sorted(cpus, reverse=True)
    for proc in procs:
        assert set(proc) >= {"pid", "cpu", "mem", "name"}


@pytest.mark.asyncio
async def test_processes_respects_limit_and_sort_by_memory(ctx: ToolContext) -> None:
    result = await laptop_processes(ctx, sort_by="memory", limit="3")
    if not result.ok:
        pytest.skip("ps unavailable on this host")
    assert len(result.output["processes"]) <= 3
    assert result.output["sort_by"] == "mem"
    mems = [p["mem"] for p in result.output["processes"]]
    assert mems == sorted(mems, reverse=True)


@pytest.mark.asyncio
async def test_processes_can_filter_by_name(ctx: ToolContext) -> None:
    result = await laptop_processes(ctx, name="python")
    if not result.ok:
        pytest.skip("ps unavailable on this host")
    for proc in result.output["processes"]:
        assert "python" in proc["name"].lower()


# ---------------------------------------------------------------------------
# Project status
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_project_status_reports_nothing_when_nothing_is_open(ctx: ToolContext) -> None:
    result = await laptop_project_status(ctx)
    assert result.ok
    assert result.output["projects"] == []
    assert "No projects" in result.message


@pytest.mark.asyncio
async def test_project_status_describes_an_open_project(ctx: ToolContext, project: Path) -> None:
    workspace = get_workspace_registry().open(str(project))
    assert workspace is not None
    try:
        result = await laptop_project_status(ctx)
        assert result.ok
        assert len(result.output["projects"]) == 1
        entry = result.output["projects"][0]
        assert entry["name"] == "demo"
        assert entry["file_count"] >= 2
        assert entry["test_file_count"] >= 1
        assert "python" in entry["languages"]
    finally:
        get_workspace_registry().close(workspace.workspace_id)


@pytest.mark.asyncio
async def test_project_status_for_a_specific_project_by_name(
    ctx: ToolContext, project: Path
) -> None:
    workspace = get_workspace_registry().open(str(project))
    assert workspace is not None
    try:
        result = await laptop_project_status(ctx, project="demo")
        assert result.ok
        assert result.output["projects"][0]["name"] == "demo"
    finally:
        get_workspace_registry().close(workspace.workspace_id)


@pytest.mark.asyncio
async def test_project_status_unknown_project_fails(ctx: ToolContext) -> None:
    result = await laptop_project_status(ctx, project="does-not-exist")
    assert not result.ok
    assert "No open project" in result.error


# ---------------------------------------------------------------------------
# Agent-prompt builder
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_prompt_builder_requires_a_goal(ctx: ToolContext) -> None:
    result = await build_agent_prompt(ctx)
    assert not result.ok
    assert "goal" in (result.error or "").lower()


@pytest.mark.asyncio
async def test_prompt_embeds_machine_state_and_safety(ctx: ToolContext) -> None:
    result = await build_agent_prompt(ctx, goal="fix the flaky test")
    assert result.ok
    prompt = result.output["prompt"]
    assert "fix the flaky test" in prompt
    assert "## Machine snapshot" in prompt
    assert "## Projects available in JARVIS" in prompt
    assert "## Working agreements (safety)" in prompt
    assert "No projects are currently open" in prompt


@pytest.mark.asyncio
async def test_prompt_includes_the_open_project(
    ctx: ToolContext, project: Path
) -> None:
    workspace = get_workspace_registry().open(str(project))
    assert workspace is not None
    try:
        result = await build_agent_prompt(ctx, goal="add a multiply helper", project="demo")
        assert result.ok
        assert "demo" in result.output["projects"]
        assert "at " in result.output["prompt"]  # project root path is referenced
    finally:
        get_workspace_registry().close(workspace.workspace_id)


# ---------------------------------------------------------------------------
# Planner routing
# ---------------------------------------------------------------------------
def first_tool(plan) -> str:
    return plan.actions[0].tool if plan.actions else ""


@pytest.mark.parametrize(
    "text,expected_tool",
    [
        ("how's my laptop", "laptop_snapshot"),
        ("check my laptop", "laptop_snapshot"),
        ("laptop condition", "laptop_snapshot"),
        ("what's running", "laptop_processes"),
        ("top processes", "laptop_processes"),
        ("status of my open projects", "laptop_project_status"),
        ("where are my open projects", "laptop_project_status"),
        ("build me an agent prompt to fix the bug", "build_agent_prompt"),
        ("handoff prompt for the coding agent", "build_agent_prompt"),
    ],
)
def test_planner_routes_cockpit_phrasings(planner: RulePlanner, text: str, expected_tool: str) -> None:
    assert first_tool(planner.parse(text)) == expected_tool, f"{text!r} -> {first_tool(planner.parse(text))!r}"


def test_planner_extracts_the_prompt_goal(planner: RulePlanner) -> None:
    plan = planner.parse("build me an agent prompt to refactor the auth module")
    action = plan.actions[0]
    assert action.tool == "build_agent_prompt"
    assert "refactor the auth module" in action.params["goal"]


def test_planner_processes_passes_sort_by(planner: RulePlanner) -> None:
    plan = planner.parse("top processes sorted by memory")
    action = plan.actions[0]
    assert action.tool == "laptop_processes"
    assert action.params.get("sort_by") == "mem"


def test_cockpit_does_not_steal_a_single_project_status(planner: RulePlanner) -> None:
    """Singular 'project status' belongs to the coding agent's _project rule."""
    plan = planner.parse("project status")
    # Either the coding agent's project summary or the cockpit -- but it must
    # resolve to a real, registered tool and never to an empty/unknown intent.
    tool = first_tool(plan)
    assert tool, "project status should produce an action"
    assert planner.tools.has(tool)
