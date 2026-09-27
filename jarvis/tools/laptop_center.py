"""Laptop Control Center (LCC).

A single cockpit for operating the developer's own machine. It answers four
questions the rest of JARVIS scatters across agents:

* **How is the laptop?**       -> :func:`laptop_snapshot`
* **What is running?**         -> :func:`laptop_processes`
* **Where are my projects?**   -> :func:`laptop_project_status`
* **Brief an agent for me.**   -> :func:`build_agent_prompt`

Every machine query is a *fixed, read-only* argv invocation routed through
:func:`tools.base.run_command` -- the same discipline as
``tools.system``.  There is no shell, and no path where an arbitrary user
string reaches the process table.  The one user-facing "run a command"
capability stays on the existing, allowlist-gated ``run_allowlisted_command``
tool; this module deliberately does not widen that surface.
"""

from __future__ import annotations

import os
import platform
import shutil
import time
from typing import Any, Dict, List, Optional

from ..core.models import RiskLevel, ToolSpec
from ..logging_setup import get_logger
from .base import ToolContext, fail, human_size, ok, run_command

log = get_logger("tools.laptop_center")

_IS_MAC = platform.system() == "Darwin"
_IS_LINUX = platform.system() == "Linux"

# ---------------------------------------------------------------------------
# Machine-condition collection (read-only)
# ---------------------------------------------------------------------------


def _format_uptime(seconds: float) -> str:
    """Render ``372345`` seconds as ``4d 9h 35m``."""
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts: List[str] = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


async def _uptime_seconds() -> Optional[float]:
    if _IS_MAC:
        code, out, _err = await run_command(["sysctl", "-n", "kern.boottime"], timeout=5)
        if code == 0:
            try:
                sec = int(out.split("sec = ", 1)[1].split()[0])
                return max(0.0, time.time() - sec)
            except (ValueError, IndexError):
                return None
    if _IS_LINUX:
        try:
            with open("/proc/uptime", encoding="utf-8") as fh:
                return float(fh.read().split()[0])
        except (OSError, ValueError, IndexError):
            return None
    return None


async def _memory_info() -> Dict[str, Any]:
    info: Dict[str, Any] = {}
    if _IS_MAC:
        code, out, _err = await run_command(["sysctl", "-n", "hw.memsize"], timeout=5)
        if code == 0 and out.strip().isdigit():
            total = int(out.strip())
            info["total"] = human_size(total)
            pcode, pout, _ = await run_command(["sysctl", "-n", "hw.pagesize"], timeout=5)
            page = int(pout.strip()) if (pcode == 0 and pout.strip().isdigit()) else 4096
            vcode, vout, _ = await run_command(["vm_stat"], timeout=5)
            if vcode == 0:
                counts: Dict[str, int] = {}
                for line in vout.splitlines()[1:]:
                    if ":" in line:
                        key, _, value = line.partition(":")
                        value = value.strip().rstrip(".")
                        if value.isdigit():
                            counts[key.strip()] = int(value)
                avail_pages = counts.get("Pages free", 0) + counts.get("Pages inactive", 0)
                available = avail_pages * page
                used = max(0, total - available)
                info["available"] = human_size(available)
                info["used"] = human_size(used)
                if total:
                    info["used_pct"] = round(used / total * 100, 1)
    elif _IS_LINUX:
        try:
            data: Dict[str, int] = {}
            with open("/proc/meminfo", encoding="utf-8") as fh:
                for line in fh:
                    key, _, rest = line.partition(":")
                    value = rest.strip().split()
                    if value and value[0].isdigit():
                        data[key.strip()] = int(value[0])  # kB
            total = data.get("MemTotal", 0) * 1024
            available = data.get("MemAvailable", data.get("MemFree", 0)) * 1024
            used = max(0, total - available)
            info["total"] = human_size(total)
            info["available"] = human_size(available)
            info["used"] = human_size(used)
            if total:
                info["used_pct"] = round(used / total * 100, 1)
        except (OSError, ValueError):
            pass
    return info


async def _disk_info() -> Dict[str, Any]:
    info: Dict[str, Any] = {}
    code, out, _err = await run_command(["df", "-h", "/"], timeout=5)
    if code == 0:
        lines = out.splitlines()
        if len(lines) >= 2:
            parts = lines[1].split()
            if len(parts) >= 5:
                info["size"] = parts[1]
                info["used"] = parts[2]
                info["available"] = parts[3]
                info["used_pct"] = parts[4].rstrip("%")
    return info


async def _battery_info() -> Dict[str, Any]:
    info: Dict[str, Any] = {}
    if _IS_MAC:
        code, out, _err = await run_command(["pmset", "-g", "batt"], timeout=5)
        if code == 0:
            percent = None
            for token in out.replace(";", " ").split():
                if token.endswith("%"):
                    try:
                        percent = int(token.rstrip("%"))
                        break
                    except ValueError:
                        pass
            lowered = out.lower()
            if "discharging" in lowered:
                state = "discharging"
            elif "charging" in lowered:
                state = "charging"
            elif "charged" in lowered:
                state = "fully charged"
            else:
                state = "on AC power" if "ac" in lowered else "unknown"
            if percent is not None:
                info["percent"] = percent
                info["state"] = state
    elif _IS_LINUX:
        for name in ("BAT0", "BAT1", "battery"):
            base = f"/sys/class/power_supply/{name}"
            try:
                with open(f"{base}/capacity", encoding="utf-8") as fh:
                    info["percent"] = int(fh.read().strip())
                with open(f"{base}/status", encoding="utf-8") as fh:
                    info["state"] = fh.read().strip().lower()
                break
            except (OSError, ValueError):
                continue
    return info


async def collect_snapshot(ctx: ToolContext) -> Dict[str, Any]:
    """Assemble the full machine-condition snapshot.

    ``ctx`` is accepted for symmetry with the other tool entry points and for
    future device-scoped variants; collection is host-global today.
    """
    snap: Dict[str, Any] = {
        "hostname": platform.node() or os.uname().nodename,
        "os": platform.platform(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
    }
    try:
        snap["load_avg"] = [round(v, 2) for v in os.getloadavg()]
    except (OSError, AttributeError):
        pass
    up = await _uptime_seconds()
    if up is not None:
        snap["uptime"] = _format_uptime(up)
    for key, collector in (
        ("memory", _memory_info),
        ("disk", _disk_info),
        ("battery", _battery_info),
    ):
        value = await collector()
        if value:
            snap[key] = value
    return snap


def _snapshot_message(snap: Dict[str, Any]) -> str:
    bits = [f"{snap.get('hostname', 'this machine')} ({snap.get('os', 'unknown OS')})"]
    if snap.get("cpu_count"):
        load = ""
        if snap.get("load_avg"):
            load = f", load {snap['load_avg'][0]:.2f}"
        bits.append(f"{snap['cpu_count']} CPUs{load}")
    if snap.get("memory"):
        m = snap["memory"]
        bits.append(f"memory {m.get('used', '?')}/{m.get('total', '?')} ({m.get('used_pct', '?')}%)")
    if snap.get("disk"):
        d = snap["disk"]
        bits.append(f"disk {d.get('used_pct', '?')}% used, {d.get('available', '?')} free")
    if snap.get("battery"):
        b = snap["battery"]
        bits.append(f"battery {b.get('percent', '?')}% ({b.get('state', 'n/a')})")
    if snap.get("uptime"):
        bits.append(f"up {snap['uptime']}")
    return "Laptop condition: " + ", ".join(bits) + "."


# ---------------------------------------------------------------------------
# Process listing (read-only)
# ---------------------------------------------------------------------------


async def _collect_processes() -> List[Dict[str, Any]]:
    argv = ["ps", "-Axo", "pid,pcpu,pmem,comm"] if _IS_MAC else ["ps", "-eo", "pid,pcpu,pmem,comm"]
    code, out, _err = await run_command(argv, timeout=15)
    procs: List[Dict[str, Any]] = []
    if code != 0:
        return procs
    for line in out.splitlines()[1:]:
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            procs.append(
                {
                    "pid": int(parts[0]),
                    "cpu": float(parts[1]),
                    "mem": float(parts[2]),
                    "name": parts[3].strip(),
                }
            )
        except ValueError:
            continue
    return procs


# ---------------------------------------------------------------------------
# Project status (open JARVIS workspaces)
# ---------------------------------------------------------------------------


async def _git_status(root: str) -> Dict[str, Any]:
    """Best-effort read-only git state for a project root; ``{}`` if not a repo."""
    if not shutil.which("git"):
        return {}
    code, out, _err = await run_command(
        ["git", "-C", root, "rev-parse", "--abbrev-ref", "HEAD"], timeout=8, cwd=root
    )
    if code != 0 or not out.strip():
        return {}
    info: Dict[str, Any] = {"branch": out.splitlines()[0].strip()}
    code, out, _err = await run_command(["git", "-C", root, "status", "--porcelain"], timeout=8, cwd=root)
    if code == 0:
        changed = [ln for ln in out.splitlines() if ln.strip()]
        info["changed_files"] = len(changed)
        info["dirty"] = bool(changed)
    return info


async def _project_entry(workspace: Any) -> Dict[str, Any]:
    from ..coding.repo import inspect as inspect_workspace

    info = inspect_workspace(workspace)
    entry: Dict[str, Any] = {
        "workspace_id": workspace.workspace_id,
        "name": workspace.display,
        "root": str(workspace.root),
        "is_git_repo": info["is_git_repo"],
        "file_count": info["file_count"],
        "test_file_count": info["test_file_count"],
        "languages": info["languages"],
        "entry_points": info["entry_points"],
        "likely_test_runners": info["likely_test_runners"],
    }
    if info["is_git_repo"]:
        git = await _git_status(str(workspace.root))
        if git:
            entry["git"] = git
    return entry


def _project_line(entry: Dict[str, Any]) -> str:
    langs = ", ".join(list(entry["languages"].keys())[:4]) or "unknown"
    git_note = ""
    git = entry.get("git") or {}
    if git.get("branch"):
        state = "dirty" if git.get("dirty") else "clean"
        git_note = f" [git: {git['branch']}, {state}]"
    return (
        f"  {entry['name']}: {entry['file_count']} files, "
        f"{entry['test_file_count']} tests, {langs}{git_note}"
    )


# ---------------------------------------------------------------------------
# Agent-prompt composition
# ---------------------------------------------------------------------------


def _snapshot_lines(snap: Dict[str, Any]) -> List[str]:
    lines = [
        f"- Host: {snap.get('hostname', 'n/a')} running {snap.get('os', 'unknown OS')} "
        f"(Python {snap.get('python', '?')})"
    ]
    if snap.get("cpu_count"):
        load = ", ".join(str(v) for v in snap["load_avg"]) if snap.get("load_avg") else "n/a"
        lines.append(f"- CPU: {snap['cpu_count']} cores, load avg {load}")
    if snap.get("memory"):
        m = snap["memory"]
        lines.append(f"- Memory: {m.get('used', '?')} of {m.get('total', '?')} used ({m.get('used_pct', '?')}%)")
    if snap.get("disk"):
        d = snap["disk"]
        lines.append(f"- Disk (/): {d.get('used_pct', '?')}% used, {d.get('available', '?')} free")
    if snap.get("battery"):
        b = snap["battery"]
        lines.append(f"- Battery: {b.get('percent', '?')}% ({b.get('state', 'n/a')})")
    if snap.get("uptime"):
        lines.append(f"- Uptime: {snap['uptime']}")
    return lines


def _project_lines(projects: List[Dict[str, Any]]) -> List[str]:
    if not projects:
        return ["- No projects are currently open in JARVIS."]
    lines: List[str] = []
    for p in projects:
        langs = ", ".join(f"{k} ({v})" for k, v in list(p["languages"].items())[:4]) or "unknown"
        runners = ", ".join(p.get("likely_test_runners", [])) or "none detected"
        git = p.get("git") or {}
        git_note = ""
        if git.get("branch"):
            state = "dirty" if git.get("dirty") else "clean"
            git_note = f"; git branch `{git['branch']}` ({state})"
        lines.append(
            f"- **{p['name']}** at `{p['root']}` -- {p['file_count']} source files, "
            f"{p['test_file_count']} tests. Languages: {langs}. Test runner: {runners}{git_note}"
        )
    return lines


def compose_agent_prompt(goal: str, snap: Dict[str, Any], projects: List[Dict[str, Any]]) -> str:
    """Compose the copy-paste prompt handed to an external coding agent."""
    out: List[str] = []
    out.append("# Agent task -- generated by JARVIS Laptop Control Center")
    out.append("")
    out.append("You are taking over a task on the developer's laptop. Here is the live state of the machine and the open projects. Work from this context.")
    out.append("")
    out.append("## Goal")
    out.append(goal)
    out.append("")
    out.append("## Machine snapshot")
    out.extend(_snapshot_lines(snap))
    out.append("")
    out.append("## Projects available in JARVIS")
    out.extend(_project_lines(projects))
    out.append("")
    out.append("## Working agreements (safety)")
    out.append("- Work only inside the project roots listed above; never touch other paths.")
    out.append("- Do not delete the repository or run destructive git commands (no force-push, no hard reset).")
    out.append("- Do not commit or push unless the developer explicitly asks for it.")
    out.append("- If you change code, run the project's tests before declaring the work done and report the result.")
    out.append("- Prefer small, reversible changes and explain what you changed and why.")
    out.append("")
    out.append("Start by reading the relevant files, then make the changes that accomplish the goal.")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


async def laptop_snapshot(ctx: ToolContext) -> Any:
    snap = await collect_snapshot(ctx)
    return ok("laptop_snapshot", _snapshot_message(snap), device=ctx.device_id, **snap)


async def laptop_processes(
    ctx: ToolContext, sort_by: str = "cpu", limit: str = "15", name: str = ""
) -> Any:
    procs = await _collect_processes()
    if not procs:
        return fail("laptop_processes", "Could not read the process table (ps unavailable).", device=ctx.device_id)
    if name:
        needle = name.lower()
        procs = [p for p in procs if needle in p["name"].lower()]
    key = "mem" if sort_by.strip().lower() in ("mem", "memory") else "cpu"
    procs.sort(key=lambda p: p[key], reverse=True)
    try:
        cap = int(limit)
    except (TypeError, ValueError):
        cap = 15
    cap = max(1, min(cap, 100))
    top = procs[:cap]
    listing = "\n".join(
        f"  {p['pid']:<7}{p['cpu']:>6.1f}%  {p['mem']:>6.1f}%  {p['name']}" for p in top
    )
    scope = f" matching {name!r}" if name else ""
    return ok(
        "laptop_processes",
        f"{len(procs)} process(es){scope}; top {len(top)} by {key} shown.",
        device=ctx.device_id,
        processes=top,
        total=len(procs),
        sort_by=key,
        listing=listing,
    )


async def laptop_project_status(ctx: ToolContext, project: str = "") -> Any:
    from ..coding.workspace import get_workspace_registry

    registry = get_workspace_registry()
    if project:
        workspace = registry.resolve(project)
        if workspace is None:
            return fail(
                "laptop_project_status",
                f"No open project named {project!r}. Open it first with the Coding Agent.",
                device=ctx.device_id,
            )
        entries = [await _project_entry(workspace)]
    else:
        entries = [await _project_entry(ws) for ws in registry.all()]

    if not entries:
        return ok(
            "laptop_project_status",
            "No projects are currently open in JARVIS. Open one with the Coding Agent first.",
            device=ctx.device_id,
            projects=[],
        )
    listing = "\n".join(_project_line(e) for e in entries)
    return ok(
        "laptop_project_status",
        f"{len(entries)} project(s) open.\n{listing}",
        device=ctx.device_id,
        projects=entries,
    )


async def build_agent_prompt(ctx: ToolContext, goal: str = "", project: str = "") -> Any:
    goal = (goal or "").strip()
    if not goal:
        return fail(
            "build_agent_prompt",
            "Tell me the goal -- what should the agent do? -- and I'll build the prompt.",
            device=ctx.device_id,
        )

    snap = await collect_snapshot(ctx)

    from ..coding.workspace import get_workspace_registry

    registry = get_workspace_registry()
    if project:
        workspace = registry.resolve(project)
        entries = [await _project_entry(workspace)] if workspace else []
    else:
        entries = [await _project_entry(ws) for ws in registry.all()]

    prompt = compose_agent_prompt(goal, snap, entries)
    return ok(
        "build_agent_prompt",
        "Here is a ready-to-paste prompt. Copy it and hand it to the agent you want to run the task.",
        device=ctx.device_id,
        prompt=prompt,
        goal=goal,
        projects=[p["name"] for p in entries],
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_LCC = "laptop_center"

SPECS = [
    (
        ToolSpec(
            name="laptop_snapshot",
            description="Report the laptop's current condition: CPU, memory, disk, battery, uptime.",
            agent=_LCC,
            risk=RiskLevel.READ,
            parameters={},
            tags=["laptop", "system", "condition", "read"],
        ),
        laptop_snapshot,
    ),
    (
        ToolSpec(
            name="laptop_processes",
            description="List running processes, sorted by CPU or memory, optionally filtered by name.",
            agent=_LCC,
            risk=RiskLevel.READ,
            parameters={"sort_by": "cpu or memory", "limit": "int as string", "name": "name filter"},
            tags=["laptop", "processes", "tasks", "read"],
        ),
        laptop_processes,
    ),
    (
        ToolSpec(
            name="laptop_project_status",
            description="Report the status of projects open in JARVIS: files, languages, tests, git branch/state.",
            agent=_LCC,
            risk=RiskLevel.READ,
            parameters={"project": "project name or workspace id (empty for all)"},
            tags=["laptop", "project", "status", "read"],
        ),
        laptop_project_status,
    ),
    (
        ToolSpec(
            name="build_agent_prompt",
            description=(
                "Compose a ready-to-paste prompt embedding the machine condition and open "
                "project status plus a goal, for hand-off to a coding agent."
            ),
            agent=_LCC,
            risk=RiskLevel.LOW,
            parameters={"goal": "what the agent should do", "project": "project name or id (empty for all)"},
            required_parameters=["goal"],
            tags=["laptop", "agent", "prompt", "handoff"],
        ),
        build_agent_prompt,
    ),
]
