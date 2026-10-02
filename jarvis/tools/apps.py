"""Application control tools (section 11).

macOS is driven through ``open`` and AppleScript -- the official, scriptable
path -- rather than GUI clicking. Other platforms degrade gracefully.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, List, Optional, Tuple

from ..core.models import RiskLevel, ToolSpec
from ..logging_setup import get_logger
from .base import ToolContext, fail, ok, run_command, unverified

log = get_logger("tools.apps")

#: Where an editor records the folders it has opened. VS Code writes
#: ``workspaceStorage/<hash>/workspace.json`` the moment a window takes a directory,
#: which is the difference between "a command exited 0" and "the folder opened".
EDITOR_STORAGE = {
    "vscode": "~/Library/Application Support/Code/User/workspaceStorage",
    "cursor": "~/Library/Application Support/Cursor/User/workspaceStorage",
}


def resolve_editor_command(spec, platform: str = "macos"):
    """Find the command line that can carry an argument, or None.

    A declared value with a "/" is relative to the installed bundle, because the
    installer that puts `code` on PATH is a separate action many users never take.
    """
    import shutil

    command = (getattr(spec, "cli", None) or {}).get(platform)
    if not command:
        return None
    if "/" not in command:
        return shutil.which(command)
    target = spec.launch.get(platform) or spec.launch.get("macos")
    for root in ("/Applications", str(Path.home() / "Applications")):
        candidate = Path(root) / f"{target}.app" / command
        if candidate.exists():
            return str(candidate)
    return shutil.which(command.split("/")[-1])


def editor_folder_opened(folder: Path, app_key: str = "vscode", storage_root=None) -> bool:
    """Whether the editor itself recorded this folder as open."""
    root = (Path(storage_root) if storage_root
            else Path(str(EDITOR_STORAGE.get(app_key, "")).replace("~", str(Path.home()))))
    if not root.is_dir():
        return False
    wanted = folder.resolve().as_uri()
    for record in root.glob("*/workspace.json"):
        try:
            recorded = json.loads(record.read_text(encoding="utf-8")).get("folder", "")
        except (OSError, ValueError):
            continue
        if recorded == wanted or Path(recorded.replace("file://", "")).expanduser() == folder:
            return True
    return False


def _resolve_folder(value: str, ctx: ToolContext):
    """Turn what the user said into a real directory, or say what went wrong.

    Absolute paths are used as given; anything else is looked up among the workspaces
    Vantage already has open, so "open vscode with agent-test" works without the user
    typing a path they have to remember.
    """
    from ..coding.workspace import get_workspace_registry

    candidate = Path(str(value)).expanduser()
    if candidate.is_dir():
        return candidate, None
    if candidate.exists():
        return None, f"{value!r} is a file, not a directory. Open its folder instead."

    needle = str(value).strip().lower()
    try:
        workspaces = get_workspace_registry().all()
    except Exception:  # a registry that cannot be read must not invent a folder either
        workspaces = []
    matches = [w for w in workspaces
               if needle and (needle == w.display.lower()
                              or needle == Path(w.root).name.lower()
                              or needle in str(w.root).lower())]
    if len(matches) == 1:
        return Path(matches[0].root), None
    if matches:
        names = ", ".join(Path(m.root).name for m in matches[:4])
        return None, (f"More than one open workspace matches {value!r}: {names}. "
                      "Say which one, or give the full path.")
    open_names = ", ".join(Path(w.root).name for w in workspaces[:4]) or "none are open"
    return None, (f"No open workspace matches {value!r} and it is not a directory on disk. "
                  f"Open workspaces: {open_names}. If it is somewhere else, give the path "
                  "and I will open_workspace for it first.")


async def open_app(ctx: ToolContext, app: str = "", path: str = "") -> "object":
    """Launch an application, optionally aimed at a folder the user named."""
    spec = ctx.apps.resolve(app, platform=ctx.platform)
    if spec is None:
        return fail(
            "open_app",
            f"I could not find an application matching {app!r}.",
            device=ctx.device_id,
            query=app,
        )

    target = ctx.apps.launch_target(spec, platform=ctx.platform)
    if not target:
        return fail(
            "open_app",
            f"{spec.display_name} is not available on {ctx.platform}.",
            device=ctx.device_id,
        )

    folder = None
    if str(path).strip():
        folder, problem = _resolve_folder(path, ctx)
        if folder is None:
            return fail("open_app", problem, device=ctx.device_id)

    if folder is None:
        if ctx.platform == "macos":
            argv = ["open", "-a", target]
        elif ctx.platform == "linux":
            argv = [target]
        else:
            argv = ["cmd", "/c", "start", "", target]
    else:
        command = resolve_editor_command(spec, platform=ctx.platform)
        if command:
            argv = [command, str(folder)]
        elif ctx.platform == "macos":
            # `--args` is the only way `open` forwards anything to the app.
            argv = ["open", "-a", target, "--args", str(folder)]
        else:
            return fail(
                "open_app",
                f"I can start {spec.display_name} on {ctx.platform}, but not with a folder: "
                "it has no command line here that accepts one.",
                device=ctx.device_id,
            )

    # The editor stays open, so this must not wait on it; whether the folder actually
    # opened is decided below, by reading the editor's own record.
    code, _out, err = await run_command(argv, detach=True)

    if code != 0:
        return fail(
            "open_app",
            f"Could not open {spec.display_name}: {err or 'launch failed'}",
            device=ctx.device_id,
        )

    if folder is None:
        return ok(
            "open_app",
            f"Opening {spec.display_name} on your {ctx.platform} machine.",
            device=ctx.device_id,
            app_key=spec.key,
            display_name=spec.display_name,
            risk=int(RiskLevel.LOW),
        )

    # The editor writes its own record; give it a moment rather than trusting the exit
    # code, and say plainly when the record never appears.
    observed = False
    for _ in range(20):
        if editor_folder_opened(folder, app_key=spec.key):
            observed = True
            break
        await asyncio.sleep(0.25)

    said = f"Opened {folder.name} in {spec.display_name}." if observed else (
        f"{spec.display_name} was told to open {folder}, but I could not confirm it from the "
        "editor's own record -- look at the window before believing me.")
    if observed:
        return ok(
            "open_app", said, device=ctx.device_id, app_key=spec.key,
            display_name=spec.display_name, folder=str(folder), verified=True,
            risk=int(RiskLevel.LOW),
        )
    return unverified(
        "open_app", said, device=ctx.device_id, app_key=spec.key,
        display_name=spec.display_name, folder=str(folder), verified=False,
    )


async def close_app(ctx: ToolContext, app: str = "") -> "object":
    """Quit an application gracefully."""
    spec = ctx.apps.resolve(app, platform=ctx.platform)
    if spec is None:
        return fail(
            "close_app",
            f"I could not find an application matching {app!r}.",
            device=ctx.device_id,
        )

    target = ctx.apps.launch_target(spec, platform=ctx.platform)
    if ctx.platform == "macos":
        script = f'tell application "{target}" to quit'
        code, _out, err = await run_command(["osascript", "-e", script])
    else:
        return fail(
            "close_app",
            f"Quitting applications is only implemented on macOS for now.",
            device=ctx.device_id,
        )

    if code != 0:
        return fail("close_app", f"Could not quit {spec.display_name}: {err}", device=ctx.device_id)

    return ok(
        "close_app",
        f"Quit {spec.display_name}.",
        device=ctx.device_id,
        app_key=spec.key,
    )


async def list_apps(ctx: ToolContext) -> "object":
    """List applications JARVIS knows how to launch."""
    apps = [
        {"key": a.key, "name": a.display_name, "category": a.category}
        for a in ctx.apps.all()
        if ctx.platform in a.platform
    ]
    return ok(
        "list_apps",
        f"I know {len(apps)} applications on this {ctx.platform} machine.",
        device=ctx.device_id,
        apps=apps,
    )


SPECS = [
    (
        ToolSpec(
            name="open_app",
            description=("Launch an application (e.g. VS Code, Chrome, Terminal), optionally "
                         "opening a folder in it."),
            risk=RiskLevel.LOW,
            requires_confirmation=False,
            reversible=True,
            parameters={"app": "string", "path": "string"},
            required_parameters=["app"],
            provides_identifiers={},
            tags=["computer", "app"],
        ),
        open_app,
    ),
    (
        ToolSpec(
            name="close_app",
            description="Quit a running application.",
            risk=RiskLevel.LOW,
            requires_confirmation=False,
            reversible=True,
            parameters={"app": "string"},
            required_parameters=["app"],
            tags=["computer", "app"],
        ),
        close_app,
    ),
    (
        ToolSpec(
            name="list_apps",
            description="List applications available to launch on this machine.",
            risk=RiskLevel.READ,
            parameters={},
            required_parameters=[],
            tags=["computer", "app"],
        ),
        list_apps,
    ),
]
