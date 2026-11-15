"""Mac Control: what is running, what can be launched, and what actually happened.

This is a thin, honest layer over things macOS already does. Two rules shape it:

* **A launch is verified by observing the process, not by the command's exit
  code.** ``open -a`` returning 0 means ``open`` did the asking; it does not mean
  the application is running. Every action here reports what it *observed*
  afterwards.
* **Only apps the registry knows about can be launched.** The registry is a
  closed list of applications with a known launch target per platform. A phrase
  that resolves to nothing is refused, so a typo cannot become a blind launch of
  whatever happened to match.

Nothing here kills a process, injects input, or reads another app's windows.
Those need accessibility permission and a different, explicitly-consented
capability; saying "not supported" is better than pretending.
"""

from __future__ import annotations

import subprocess
from typing import Any, Dict, List, Optional

from ..logging_setup import get_logger

log = get_logger("core.maccontrol")

#: How long a launch may take before the verification gives up waiting. A cold
#: start of a large editor is the slow case; past this the answer is "not
#: observed yet", which is what the caller should report.
LAUNCH_TIMEOUT = 15.0


def running_applications() -> List[Dict[str, str]]:
    """The GUI applications currently running, as macOS reports them.

    Read with ``osascript``'s System Events list. If that is unavailable (no
    automation permission, or a headless session) this returns an empty list --
    never a fabricated one -- and ``available`` on the caller's payload says
    whether the answer is real.
    """
    script = ('tell application "System Events" to get name of every '
              'application process')
    try:
        result = subprocess.run(["osascript", "-e", script],
                                capture_output=True, text=True, timeout=10)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []
    if result.returncode != 0:
        return []
    names = [name.strip() for name in (result.stdout or "").split(",") if name.strip()]
    return [{"name": name} for name in sorted(names)]


def _is_running(process_name: str) -> bool:
    """Whether a process whose name matches is alive right now."""
    try:
        result = subprocess.run(["pgrep", "-x", process_name],
                                capture_output=True, text=True, timeout=5)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False
    return bool((result.stdout or "").strip())


def supported_applications() -> List[Dict[str, Any]]:
    """Every application this system knows how to launch, with its target."""
    from ..registry.apps import AppRegistry

    registry = AppRegistry()
    out: List[Dict[str, Any]] = []
    for spec in registry.all():
        out.append({
            "key": spec.key,
            "name": spec.display_name,
            "aliases": list(spec.aliases),
            "launch_target": registry.launch_target(spec) or "",
            "running": _is_running((registry.launch_target(spec) or "").split("/")[-1]),
        })
    return sorted(out, key=lambda a: a["name"].lower())


def status() -> Dict[str, Any]:
    """What Mac Control can and cannot do on this machine right now."""
    running = running_applications()
    return {
        "supported": supported_applications(),
        "running": running,
        "can_list_running": bool(running),
        "note": (
            "Listing running applications needs Automation permission for System "
            "Events. An empty list with can_list_running false means it was not "
            "granted, not that nothing is running."
            if not running else
            "Launching and verifying applications is supported. Window control and "
            "input injection need Accessibility permission and are not offered."),
    }


def open_application(app: str, path: str = "") -> Dict[str, Any]:
    """Launch an application, then observe whether it actually started.

    The application must resolve through the registry: an unknown phrase is
    refused rather than passed to ``open``, so a typo cannot launch something
    unintended.
    """
    from ..registry.apps import AppRegistry

    registry = AppRegistry()
    spec = registry.resolve(app)
    if spec is None:
        return {"ok": False, "launched": False,
                "detail": f"{app!r} does not match an application this system knows",
                "observed_running": False}

    target = registry.launch_target(spec)
    if not target:
        return {"ok": False, "launched": False,
                "detail": f"{spec.display_name} has no launch target on this platform",
                "observed_running": False}

    argv = ["open", "-a", target]
    if str(path).strip():
        argv.append(str(path))

    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=20)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        return {"ok": False, "launched": False,
                "detail": f"launch failed: {exc}", "observed_running": False}

    if result.returncode != 0:
        return {"ok": False, "launched": False,
                "detail": (result.stderr or "open reported a failure").strip()[:300],
                "observed_running": False}

    # The command succeeded. Whether the app is running is a separate question,
    # and the honest answer may be "not yet".
    import time

    process_name = target.split("/")[-1]
    deadline = time.time() + LAUNCH_TIMEOUT
    while time.time() < deadline:
        if _is_running(process_name):
            log.info("application launched and observed",
                     extra_fields={"app": spec.key})
            return {"ok": True, "launched": True,
                    "detail": f"{spec.display_name} is running",
                    "observed_running": True, "app": spec.display_name}
        time.sleep(0.5)

    return {
        "ok": False, "launched": True,
        "detail": (f"the launch command was accepted, but {spec.display_name} was not "
                   f"observed running within {int(LAUNCH_TIMEOUT)}s"),
        "observed_running": False, "app": spec.display_name,
    }