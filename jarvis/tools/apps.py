"""Application control tools (section 11).

macOS is driven through ``open`` and AppleScript -- the official, scriptable
path -- rather than GUI clicking. Other platforms degrade gracefully.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
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


def resolve_folder(value: str, ctx: ToolContext):
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


#: Editors this tool knows how to aim at a folder, in preference order. A name here is
#: a claim that `EDITOR_STORAGE` or a CLI can prove the folder opened; anything else the
#: machine has installed is deliberately not included, because an unverifiable launch
#: would be reported as success anyway.
EDITOR_KEYS = ("vscode", "cursor")


def _app_installed(spec, platform: str) -> bool:
    """Whether the app is actually on this machine, from the same places a person would look."""
    import shutil

    target = spec.launch.get(platform, "")
    if platform == "macos":
        from .agents import BUNDLE_ROOTS

        return any(os.path.isdir(os.path.expanduser(root) + f"/{target}.app")
                   for root in BUNDLE_ROOTS)
    return shutil.which(target) is not None


def installed_editors(apps, platform: str = "macos") -> List[Any]:
    """The registered editors present on this machine, best first."""
    found = []
    for key in EDITOR_KEYS:
        spec = apps.get(key)
        if spec is not None and _app_installed(spec, platform):
            found.append(spec)
    return found


async def open_project_folder(ctx: ToolContext, project: str = "") -> Any:
    """Open a project's folder in an editor, choosing the editor from what is installed.

    "Open my project" used to be answered with a note claiming the build had no file
    agent. It does, and the folder-open it needed was built a phase earlier, so this is
    that path with the project named -- and with the honest answers kept: no open project
    says so, two open projects asks which, and nothing is invented in either case.
    """
    from ..coding.workspace import get_workspace_registry

    needle = str(project or "").strip()
    if needle:
        folder, problem = resolve_folder(needle, ctx)
        if folder is None:
            return fail("open_project_folder", problem, device=ctx.device_id)
    else:
        workspaces = get_workspace_registry().all()
        if not workspaces:
            return fail(
                "open_project_folder",
                "No project is open in me yet. Give me its folder and I will "
                "open_workspace it first, then I can open it in the editor.",
                device=ctx.device_id,
            )
        if len(workspaces) > 1:
            names = ", ".join(w.display for w in workspaces[:4])
            return fail(
                "open_project_folder",
                f"More than one project is open: {names}. Which one?",
                device=ctx.device_id,
                candidates=[str(w.root) for w in workspaces[:4]],
            )
        folder = Path(workspaces[0].root)

    editors = installed_editors(ctx.apps, platform=ctx.platform)
    if not editors:
        return fail(
            "open_project_folder",
            f"{folder.name} is a real folder, but no editor I can aim at a folder is "
            f"installed ({', '.join(EDITOR_KEYS)}). Open it yourself, or install one.",
            device=ctx.device_id,
            folder=str(folder),
        )

    editor = editors[0]
    result = await open_app(ctx, app=editor.display_name, path=str(folder))
    if not result.ok:
        return fail("open_project_folder", result.error, device=ctx.device_id,
                    **{**result.output, "folder": str(folder)})
    #: The inner result already reports the folder and the app; `editor` is the name a
    #: person recognises, so it is added rather than duplicated.
    details = {**result.output, "folder": str(folder), "editor": editor.display_name}
    return ok("open_project_folder", result.message, device=ctx.device_id,
              verification=result.verification, **details)


def bundle_installed(target: str) -> bool:
    """Is an application by this name actually on this machine?

    Needed because a launch is detached on purpose (a GUI app holds its pipe open for as long
    as it runs), so ``open -a``'s exit code is never read -- and ``open -a Firefox`` for an
    absent Firefox exits 1. Without this check the only answer available was "told, could not
    confirm", when the truth is that there is nothing to open.
    """
    from .agents import BUNDLE_ROOTS

    name = str(target or "").strip()
    if not name:
        return False
    return any(
        Path(os.path.expanduser(os.path.join(root, f"{name}.app"))).is_dir()
        for root in BUNDLE_ROOTS
    )


async def _process_observed(target: str, platform: str) -> bool:
    """Has the application actually appeared in the process table?

    ``open -a`` exits 0 when the launch was *requested*. That is the difference this project
    keeps having to relearn: an exit code is a handler succeeding, not a window on screen.
    """
    code, out, _err = await run_command(["ps", "-axo", "pid=,comm="])
    if code != 0 or not out:
        return False
    if platform == "macos":
        #: The main executable, not anything under the bundle. With Calendar.app closed, the
        #: process table still held
        #: ``/System/Applications/Calendar.app/Contents/PlugIns/CalendarWidgetExtension.appex/...``,
        #: so a probe for "/Calendar.app/" reported the application as open from a widget.
        return f"/{target}.app/Contents/MacOS/".lower() in (out or "").lower()
    needle = target.lower()
    return any(line.strip().rsplit("/", 1)[-1] == needle for line in (out or "").splitlines())


#: Short and bounded on purpose. A cold app shows up inside a few hundred milliseconds; if it
#: has not appeared by now the honest answer is "could not confirm", not a longer wait that
#: turns one sentence into a stall.
LAUNCH_OBSERVATION_TRIES = 6
LAUNCH_OBSERVATION_INTERVAL_S = 0.2


async def open_app(ctx: ToolContext, app: str = "", path: str = "") -> "object":
    """Launch an application, optionally aimed at a folder the user named."""
    spec = ctx.apps.resolve(app, platform=ctx.platform)
    if spec is None:
        return fail("open_application",
            f"I could not find an application matching {app!r}.",
            device=ctx.device_id,
            query=app,
        )

    target = ctx.apps.launch_target(spec, platform=ctx.platform)
    if not target:
        return fail("open_application",
            f"{spec.display_name} is not available on {ctx.platform}.",
            device=ctx.device_id,
        )

    folder = None
    if str(path).strip():
        folder, problem = resolve_folder(path, ctx)
        if folder is None:
            return fail("open_application", problem, device=ctx.device_id)

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
            return fail("open_application",
                f"I can start {spec.display_name} on {ctx.platform}, but not with a folder: "
                "it has no command line here that accepts one.",
                device=ctx.device_id,
            )

    # The editor stays open, so this must not wait on it; whether the folder actually
    # opened is decided below, by reading the editor's own record.
    code, _out, err = await run_command(argv, detach=True)

    if code != 0:
        return fail("open_application",
            f"Could not open {spec.display_name}: {err or 'launch failed'}",
            device=ctx.device_id,
        )

    if folder is None:
        #: The launch was requested; now look for the process. "is open" is only said when
        #: something observed it, and the absence of an observation is reported as such.
        observed = False
        for _ in range(LAUNCH_OBSERVATION_TRIES):
            if await _process_observed(target, ctx.platform):
                observed = True
                break
            await asyncio.sleep(LAUNCH_OBSERVATION_INTERVAL_S)

        if observed:
            return ok("open_application",
                f"{spec.display_name} is open on your {ctx.platform} machine.",
                device=ctx.device_id,
                app_key=spec.key,
                display_name=spec.display_name,
                verified=True,
                risk=int(RiskLevel.LOW),
            )
        if not bundle_installed(target):
            return fail("open_application",
                f"{spec.display_name} is not installed on this machine, so there is nothing "
                "for me to open. The name came from my own application list, which means that "
                "list is out of date.",
                device=ctx.device_id, app_key=spec.key, display_name=spec.display_name)
        return unverified("open_application",
            f"{spec.display_name} was told to open, but I could not confirm it from the "
            "process table -- look at the screen before believing me.",
            device=ctx.device_id,
            app_key=spec.key,
            display_name=spec.display_name,
            verified=False,
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
        return ok("open_application", said, device=ctx.device_id, app_key=spec.key,
            display_name=spec.display_name, folder=str(folder), verified=True,
            risk=int(RiskLevel.LOW),
        )
    return unverified("open_application", said, device=ctx.device_id, app_key=spec.key,
        display_name=spec.display_name, folder=str(folder), verified=False,
    )


async def open_document(ctx: ToolContext, path: str = "", app: str = "") -> "object":
    """Open a document in a chosen app (or in its default handler).

    The verification is the same rule ``open_application`` follows: ``open``
    exiting 0 means the launch was *requested*, not that a window appeared. When
    an app is named, success is claimed only if that app is observed in the
    process table; with no app, the default handler is an unverifiable launch and
    is reported as such.
    """
    target_path = Path(os.path.expanduser(str(path or "").strip()))
    if not str(path).strip():
        return fail("open_document", "Which document should I open? Give me its path.",
                    device=ctx.device_id)
    if not target_path.exists():
        return fail("open_document", f"I could not find {target_path}.", device=ctx.device_id,
                    path=str(target_path))
    if target_path.is_dir():
        return fail("open_document",
                    f"{target_path.name} is a folder, not a document -- say open the project "
                    "folder instead.", device=ctx.device_id, path=str(target_path))

    named = str(app or "").strip()
    if named:
        spec = ctx.apps.resolve(named, platform=ctx.platform)
        if spec is None:
            return fail("open_document", f"I could not find an application matching {named!r}.",
                        device=ctx.device_id, app=named)
        target = ctx.apps.launch_target(spec, platform=ctx.platform)
        if ctx.platform == "macos":
            argv = ["open", "-a", target, str(target_path)]
        elif ctx.platform == "linux":
            argv = [target, str(target_path)]
        else:
            argv = ["cmd", "/c", "start", "", target, str(target_path)]
        code, _out, err = await run_command(argv, detach=True)
        if code != 0:
            return fail("open_document", f"Could not open {target_path.name} in "
                        f"{spec.display_name}: {err or 'launch failed'}", device=ctx.device_id)
        observed = False
        for _ in range(LAUNCH_OBSERVATION_TRIES):
            if await _process_observed(target, ctx.platform):
                observed = True
                break
            await asyncio.sleep(LAUNCH_OBSERVATION_INTERVAL_S)
        if observed:
            return ok("open_document", f"Opened {target_path.name} in {spec.display_name}.",
                      device=ctx.device_id, path=str(target_path), app_key=spec.key,
                      display_name=spec.display_name, verified=True, risk=int(RiskLevel.LOW))
        return unverified("open_document",
                          f"{spec.display_name} was told to open {target_path.name}, but I "
                          "could not confirm it -- check the window.", device=ctx.device_id,
                          path=str(target_path), app_key=spec.key, verified=False)

    # No app named: hand it to the default handler. That launch cannot be observed
    # from here, so it is reported unverified rather than claimed.
    argv = (["open", str(target_path)] if ctx.platform == "macos"
            else [str(target_path)] if ctx.platform == "linux"
            else ["cmd", "/c", "start", "", str(target_path)])
    code, _out, err = await run_command(argv, detach=True)
    if code != 0:
        return fail("open_document", f"Could not open {target_path.name}: "
                    f"{err or 'launch failed'}", device=ctx.device_id)
    return unverified("open_document",
                      f"Asked your default application to open {target_path.name}; I cannot "
                      "confirm which app took it.", device=ctx.device_id, path=str(target_path),
                      verified=False)


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


#: Ask an application to come forward. Verified afterwards by reading the
#: frontmost application back, because "the AppleScript exited 0" is not the same
#: fact as "that window is now in front".
_ACTIVATE = '''
tell application "{target}" to activate
'''

async def focus_app(ctx: ToolContext, app: str = "") -> "object":
    """Bring an already-running application to the front.

    Distinct from opening it: a person asking to *switch to* Mail does not want a
    second instance, and wants to know whether it actually came forward. The
    result is checked against the frontmost application rather than assumed from
    the AppleScript's exit code, and an app that is not running is reported as
    such with the next useful action, not silently launched.
    """
    spec = ctx.apps.resolve(app, platform=ctx.platform)
    if spec is None:
        return fail(
            "focus_application",
            f"I could not find an application matching {app!r}.",
            device=ctx.device_id,
        )
    if ctx.platform != "macos":
        return fail(
            "focus_application",
            "Bringing an application forward is only implemented on macOS for now.",
            device=ctx.device_id,
        )

    target = ctx.apps.launch_target(spec, platform=ctx.platform)
    code, _out, err = await run_command(
        ["osascript", "-e", _ACTIVATE.format(target=target)], timeout=ACTIVATE_TIMEOUT_S)
    if code != 0:
        detail = (err or "").strip()
        if "-1728" in detail or "can't get application" in detail.lower():
            return fail(
                "focus_application",
                f"{spec.display_name} is not running, so there is nothing to bring forward. "
                f"Open it first.",
                device=ctx.device_id, running=False,
            )
        if "-1743" in detail or "not authorized" in detail.lower():
            return fail(
                "focus_application",
                f"macOS would not let me bring {spec.display_name} forward: Vantage needs "
                "Automation permission for it (System Settings -> Privacy & Security -> "
                "Automation).",
                device=ctx.device_id,
            )
        return fail("focus_application",
                    f"Could not bring {spec.display_name} forward. {detail[:160]}".strip(),
                    device=ctx.device_id)

    # Read the answer back rather than trusting the exit code.
    check_code, out, _err = await run_command(
        ["osascript", "-e", _FRONTMOST_PROCESS], timeout=FINDER_QUERY_TIMEOUT_S)
    front = (out or "").strip().splitlines()[0].strip() if check_code == 0 and out else ""
    if front and front.lower() == spec.display_name.lower():
        return ok("focus_application",
                  f"{spec.display_name} is in front.",
                  device=ctx.device_id, application=front, verified=True)
    return unverified(
        "focus_application",
        f"I asked {spec.display_name} to come forward and the request was accepted, but "
        + (f"{front} is still the application in front." if front
           else "I could not confirm which application is in front."),
        device=ctx.device_id,
    )

async def review_downloads(ctx: ToolContext) -> "object":
    """List what is in Downloads, newest first. It never deletes anything.

    Reporting only the count would answer a question nobody asked; the point of
    reviewing a folder is seeing what is in it, so the largest/most recent entries
    are named.
    """
    folder = Path.home() / "Downloads"
    if not folder.is_dir():
        return fail("review_downloads",
                    f"There is no Downloads folder at {folder}.",
                    device=ctx.device_id)
    entries = []
    try:
        for item in folder.iterdir():
            if item.name.startswith("."):
                continue
            try:
                stat = item.stat()
            except OSError:
                continue
            entries.append({"name": item.name, "is_dir": item.is_dir(),
                            "size_bytes": stat.st_size, "modified": stat.st_mtime})
    except OSError as exc:
        return fail("review_downloads", f"Could not read Downloads: {exc}",
                    device=ctx.device_id)

    entries.sort(key=lambda e: e["modified"], reverse=True)
    shown = entries[:10]
    if entries:
        names = ", ".join(e["name"] for e in shown[:5])
        message = (f"Your Downloads folder has {len(entries)} item(s). "
                   f"Most recent: {names}.")
    else:
        message = "Your Downloads folder is empty."
    return ok("review_downloads", message, device=ctx.device_id,
              count=len(entries), items=shown, verified=True)

def _running_locally(name: str) -> bool:
    """Whether a process with this executable name is running, by exact name.

    Substring matching on the full path is wrong here: macOS ships
    ``CursorUIViewService``, so "cursor" appears in a path on a machine with no
    Cursor installed. Only the executable's own name counts.
    """
    try:
        out = subprocess.run(["ps", "-wwo", "comm=", "-A"],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return False
    wanted = name.strip().lower()
    return any(Path(line.strip()).name.lower() in (wanted, wanted + ".app")
               for line in out.splitlines())

#: Where macOS puts application bundles. Utilities and CoreServices matter:
#: Terminal lives in ``/System/Applications/Utilities`` and Finder in
#: ``/System/Library/CoreServices``, so a check that only looks at
#: ``/Applications`` reports two of the most-used apps as not installed.
_BUNDLE_ROOTS = (
    "/Applications",
    "/Applications/Utilities",
    "/System/Applications",
    "/System/Applications/Utilities",
    "/System/Library/CoreServices",
)

def _bundle_installed(target: str) -> bool:
    """Whether macOS has an application bundle with this name."""
    roots = list(_BUNDLE_ROOTS) + [str(Path.home() / "Applications")]
    return any(Path(root, f"{target}.app").exists() for root in roots)

async def get_app_status(ctx: ToolContext) -> "object":
    """Which known applications are actually installed, and which are running.

    "Vantage knows 15 applications" was true and useless: it listed apps the
    machine does not have (Cursor, on this one), so a person could not tell what
    an open request would actually do. Availability is checked on disk and
    running state is asked of the system, not assumed.
    """
    rows = []
    for app in ctx.apps.all():
        if ctx.platform not in app.platform:
            continue
        target = ctx.apps.launch_target(app, platform=ctx.platform) or app.display_name
        installed = _bundle_installed(target) if ctx.platform == "macos" else True
        rows.append({
            "key": app.key,
            "name": app.display_name,
            "category": app.category,
            "installed": installed,
            "running": _running_locally(app.display_name) if installed else False,
        })
    # The question asked is usually "what is running", so name those. Reporting only
    # the install count answered a question nobody asked.
    running = [r["name"] for r in rows if r["running"]]
    installed = sum(1 for r in rows if r["installed"])
    if running:
        message = (f"Running now: {', '.join(running)}. "
                   f"{installed} of {len(rows)} known applications are installed here.")
    else:
        message = (f"Nothing from my list is running right now. "
                   f"{installed} of {len(rows)} known applications are installed here.")
    return ok("get_app_status", message, device=ctx.device_id, apps=rows, verified=True)

async def list_apps(ctx: ToolContext) -> "object":
    """List applications JARVIS knows how to launch."""
    apps = [
        {"key": a.key, "name": a.display_name, "category": a.category}
        for a in ctx.apps.all()
        if ctx.platform in a.platform
    ]
    return ok("get_running_applications",
        f"I know {len(apps)} applications on this {ctx.platform} machine.",
        device=ctx.device_id,
        apps=apps,
    )


#: §6's standard folders. The dictionary is the whole authority: the parameter names one of
#: these, and the path is built from the home directory, never from the words in the sentence.
#: That is what makes "open ../../.ssh" a refusal instead of a request with a different shape.
USER_FOLDERS = {
    "desktop": "Desktop",
    "downloads": "Downloads",
    "documents": "Documents",
    "pictures": "Pictures",
    "home": "",
}

#: What Finder is showing. An AppleScript query, so it needs Automation permission; without
#: it the folder may well have opened and nothing here can prove it, which is UNKNOWN, not a
#: claim and not a failure.
_FINDER_WINDOWS = 'tell application "Finder" to get name of every window'

#: Which application macOS considers frontmost. Needs Accessibility on the same grounds.
_FRONTMOST_PROCESS = ("tell application \"System Events\" to get name of first "
                      "application process whose frontmost is true")


def _standard_folder(name: str) -> Tuple[Optional[Path], str]:
    """(path, problem) -- exactly one of the two is set. Never a path the caller wrote."""
    key = str(name or "").strip().lower()
    for suffix in (" folder", " directory"):
        if key.endswith(suffix):
            key = key[: -len(suffix)].strip()
    if key not in USER_FOLDERS:
        return None, (
            f"I only open the standard folders by name: {', '.join(USER_FOLDERS)}. "
            "Anything else needs its own path, and I do not take a path from a folder name."
        )
    path = Path.home() / USER_FOLDERS[key]
    if not path.is_dir():
        return None, f"{path} does not exist on this machine, so there is nothing to open."
    return path, ""


#: An unanswered AppleScript is the common case here, not the rare one: the query below took
#: 20 s on a live run because the Automation prompt had no one in front of it. One sentence
#: gets one deadline (Phase 34), so the question is asked with a short budget and a timeout is
#: treated as an answer -- "I cannot ask Finder" -- rather than as a reason to wait again.
FINDER_QUERY_TIMEOUT_S = 3.0

#: Activating an application is not the same budget as asking Finder a question:
#: the target app has to receive and handle an AppleScript event, and one that is
#: starting up (or showing an Automation prompt) takes longer than 3 s. Mail timed
#: out at the Finder budget and the user was told "could not bring Mail forward"
#: about an app that simply had not answered yet.
ACTIVATE_TIMEOUT_S = 15.0


async def _finder_reports(name: str) -> Tuple[bool, str]:
    """(seen, refusal_reason) -- did Finder list a window for this folder?"""
    for _ in range(4):
        code, out, err = await run_command(
            ["osascript", "-e", _FINDER_WINDOWS], timeout=FINDER_QUERY_TIMEOUT_S)
        if code != 0:
            text = (err or out or "").strip()
            if code == 124 or "-1712" in text:
                return False, ("Finder did not answer in time, which usually means the "
                               "Automation permission prompt is still waiting for you")
            if "-1743" in text or "not authorized" in text.lower() or "not permitted" in text.lower():
                return False, ("macOS will not let me ask Finder what it is showing "
                               "(Automation permission)")
            return False, f"Finder did not answer ({text[:120]})" if text else "Finder did not answer"
        if name.lower() in (out or "").lower():
            return True, ""
        await asyncio.sleep(0.25)
    return False, ""


async def open_user_folder(ctx: ToolContext, folder: str = "") -> "object":
    """Reveal one of the standard folders in Finder."""
    path, problem = _standard_folder(folder)
    if path is None:
        return fail("open_user_folder", problem, device=ctx.device_id, folder=folder)

    argv = (["open", str(path)] if ctx.platform == "macos" else ["xdg-open", str(path)])
    code, _out, err = await run_command(argv)
    if code != 0:
        return fail("open_user_folder",
                    f"Could not open {path.name or 'the home folder'}: {(err or '').strip()[:160]}",
                    device=ctx.device_id, folder=str(path))

    name = path.name or "Home"
    seen, refusal = await _finder_reports(name)
    if seen:
        return ok("open_user_folder", f"{name} is open in Finder.", device=ctx.device_id,
                  folder=str(path), verified=True)
    if refusal:
        return unverified("open_user_folder",
            f"{name} was told to open, but {refusal} -- look at the screen before believing me.",
            device=ctx.device_id, folder=str(path), verified=False)
    return unverified("open_user_folder",
        f"{name} was told to open, but Finder has not shown a window for it yet.",
        device=ctx.device_id, folder=str(path), verified=False)


async def get_frontmost_application(ctx: ToolContext) -> "object":
    """Name the application macOS says is in front, or say which permission stopped it."""
    code, out, err = await run_command(
        ["osascript", "-e", _FRONTMOST_PROCESS], timeout=FINDER_QUERY_TIMEOUT_S)
    text = (out or "").strip()
    name = text.splitlines()[0].strip() if text else ""
    if code != 0 or not name:
        detail = (err or out or "").strip()
        if code == 124 or "-1712" in detail:
            return fail("get_frontmost_application",
                "System Events did not answer in time, so I will not guess which application "
                "is in front. This usually means an Accessibility prompt is waiting for you "
                "(System Settings -> Privacy & Security -> Accessibility).",
                device=ctx.device_id)
        if "-1743" in detail or "not authorized" in detail.lower():
            return fail("get_frontmost_application",
                "macOS will not tell me which application is in front: Vantage needs "
                "Accessibility permission (System Settings -> Privacy & Security -> "
                "Accessibility). I am not going to guess the name.",
                device=ctx.device_id)
        return fail("get_frontmost_application",
            f"I could not read the frontmost application. {detail[:160]}".strip(),
            device=ctx.device_id)
    return ok("get_frontmost_application",
              f"{name} is the application in front right now.",
              device=ctx.device_id, application=name, verified=True)


SPECS = [
    (
        ToolSpec(
            name="open_application",
            #: The name this tool shipped under for its first 48 phases. Kept as an
            #: alias because permissions, confirmations, audit rows and stored job
            #: steps all reference it -- see ToolSpec.aliases.
            aliases=("open_app",),
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
            name="open_project_folder",
            description="Open a project's folder in an installed editor, by name or the one open project.",
            risk=RiskLevel.LOW,
            requires_confirmation=False,
            reversible=True,
            parameters={"project": "string"},
            required_parameters=[],
            tags=["computer", "app", "project"],
        ),
        open_project_folder,
    ),
    (
        ToolSpec(
            name="open_document",
            description="Open a document in a chosen app, or in its default handler.",
            risk=RiskLevel.LOW,
            requires_confirmation=False,
            reversible=True,
            parameters={"path": "string", "app": "string"},
            required_parameters=["path"],
            tags=["computer", "app", "document"],
        ),
        open_document,
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
            name="get_running_applications",
            #: The name this tool shipped under for its first 48 phases. Kept as an
            #: alias because permissions, confirmations, audit rows and stored job
            #: steps all reference it -- see ToolSpec.aliases.
            aliases=("list_apps",),
            description="List applications available to launch on this machine.",
            risk=RiskLevel.READ,
            parameters={},
            required_parameters=[],
            tags=["computer", "app"],
        ),
        list_apps,
    ),
    (
        ToolSpec(
            name="open_user_folder",
            description=("Reveal a standard folder (Desktop, Downloads, Documents, Pictures, "
                         "Home) in Finder. The folder is named, never typed as a path."),
            agent="laptop",
            risk=RiskLevel.LOW,
            requires_confirmation=False,
            reversible=True,
            allowed_devices=["laptop_main"],
            parameters={"folder": "string"},
            required_parameters=["folder"],
            tags=["computer", "app", "folder"],
        ),
        open_user_folder,
    ),
    (
        ToolSpec(
            name="review_downloads",
            description="List what is in the Downloads folder. It never deletes.",
            risk=RiskLevel.READ,
            requires_confirmation=False,
            allowed_devices=["laptop_main"],
            parameters={},
            tags=["computer", "files", "read"],
        ),
        review_downloads,
    ),
    (
        ToolSpec(
            name="get_app_status",
            description="Which known applications are installed and running on this machine.",
            risk=RiskLevel.READ,
            requires_confirmation=False,
            parameters={},
            required_parameters=[],
            tags=["computer", "app", "read"],
        ),
        get_app_status,
    ),
    (
        ToolSpec(
            name="focus_application",
            aliases=("focus_app", "bring_to_front", "switch_to_app"),
            description="Bring an already-running application to the front.",
            risk=RiskLevel.LOW,
            requires_confirmation=False,
            reversible=True,
            parameters={"app": "string"},
            required_parameters=["app"],
            tags=["computer", "app", "window"],
        ),
        focus_app,
    ),
    (
        ToolSpec(
            name="get_frontmost_application",
            description="Name the application macOS reports as being in front right now.",
            agent="laptop",
            risk=RiskLevel.READ,
            requires_confirmation=False,
            reversible=True,
            allowed_devices=["laptop_main"],
            parameters={},
            required_parameters=[],
            tags=["computer", "app", "read"],
        ),
        get_frontmost_application,
    ),
]
