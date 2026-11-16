"""The bridge to the native desktop executor — real Core Graphics, not AppleScript.

Why this exists, in one measurement. On this machine, from a Python process:

* ``osascript ... click at {x, y}`` exits **1** with
  ``osascript is not allowed assistive access (-25211)``. The existing
  ``click_desktop`` tool correctly reports that as a failure.
* ``CGPreflightPostEventAccess()`` is ``false`` for any freshly built binary, and
  ``CGEvent.post`` then **returns no error and does nothing** — the silent no-op
  that makes "the call returned" worthless as evidence.
* ``CGWarpMouseCursorPosition`` moves the real pointer with only screen-recording
  access: measured (701,718) → (731,748), confirmed by readback.

So the capability is split by what macOS actually authorizes, and this module
reports which half is available rather than implying both are.

The executor is a compiled Swift binary (``native/desktop_executor/main.swift``).
It is a separate executable on purpose: macOS grants input permission per
*binary identity*, so the process that posts the event must be the process the
person authorized. A grant to Vantage.app does not extend to a Python script,
and this module never pretends otherwise.

Every call returns the executor's own structured result, including the readback
that decided the status. Nothing here upgrades an ``performed_unverified`` to a
success.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..logging_setup import get_logger

log = get_logger("desktop.executor")

#: One action cannot hold the executor open forever; a hung event loop would
#: block the mission runner's whole step.
DEFAULT_TIMEOUT_S = 20.0

#: A move is allowed a longer bound because a smooth path sleeps between steps.
MOVE_TIMEOUT_S = 30.0

_EXECUTABLE_NAME = "vantage-desktop"


def executor_path() -> Optional[Path]:
    """Where the native executor is, or None.

    Search order is deliberate: an explicit override first (tests and a packaged
    build), then next to the app bundle, then the in-repo build output. A
    missing binary is reported as a missing capability, never worked around with
    a slower or less honest mechanism.
    """
    override = os.environ.get("VANTAGE_DESKTOP_EXECUTOR")
    if override and Path(override).is_file():
        return Path(override)

    here = Path(__file__).resolve()
    candidates = [
        # A packaged app puts the helper beside the backend.
        here.parent.parent.parent / "native" / "desktop_executor" / _EXECUTABLE_NAME,
        here.parent.parent.parent / "build" / _EXECUTABLE_NAME,
        Path.home() / ".jarvis-forge" / "bin" / _EXECUTABLE_NAME,
    ]
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate

    found = shutil.which(_EXECUTABLE_NAME)
    return Path(found) if found else None


def _run(operation: str, args: Optional[List[str]] = None,
         timeout: float = DEFAULT_TIMEOUT_S) -> Dict[str, Any]:
    """Invoke the executor and return its JSON result verbatim.

    The executor's exit code is coarse; the structured ``status`` field is the
    meaning. Anything unparseable becomes ``unknown`` with the raw output, which
    is the honest answer when a helper misbehaves.
    """
    args = list(args or [])
    binary = executor_path()
    if binary is None:
        return {
            "status": "permission_required",
            "reason": (
                "the native desktop executor is not built. Desktop control needs a "
                "compiled helper macOS can authorize; build it with "
                "`native/desktop_executor/build.sh`. No AppleScript fallback is used "
                "because it cannot post events either without the same permission."
            ),
            "operation": operation,
            "executor": None,
        }

    try:
        completed = subprocess.run(
            [str(binary), operation, *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "timed_out",
            "reason": f"the desktop executor did not answer within {timeout:.0f}s",
            "operation": operation,
        }
    except OSError as exc:
        return {
            "status": "failed",
            "reason": f"the desktop executor could not be started: {exc}",
            "operation": operation,
        }

    raw = (completed.stdout or "").strip()
    result: Dict[str, Any]
    try:
        result = json.loads(raw) if raw else {}
    except ValueError:
        result = {
            "status": "unknown",
            "reason": "the desktop executor returned something that is not JSON",
            "raw": raw[:400],
        }
    if not isinstance(result, dict) or "status" not in result:
        result = {
            "status": "unknown",
            "reason": "the desktop executor returned no status",
            "raw": raw[:400],
        }
    result.setdefault("operation", operation)
    if completed.stderr and completed.stderr.strip():
        # Kept because a Swift crash writes there and would otherwise vanish.
        result.setdefault("stderr", completed.stderr.strip()[:400])
    return result


# -- readings -----------------------------------------------------------------

def permissions() -> Dict[str, Any]:
    """What this executable may actually do, straight from macOS."""
    return _run("permissions")


def request_access() -> Dict[str, Any]:
    """Ask macOS to show its own Accessibility prompt. Grants nothing itself."""
    return _run("request_access")


# -- pointer ------------------------------------------------------------------

def move(x: float, y: float, *, smooth: bool = False,
         duration_ms: int = 0) -> Dict[str, Any]:
    args = [f"x={x}", f"y={y}"]
    if smooth:
        args.append("smooth=true")
    if duration_ms > 0:
        args.append(f"duration_ms={int(duration_ms)}")
    return _run("move", args, timeout=MOVE_TIMEOUT_S)


def click(x: float, y: float, *, button: str = "left", count: int = 1) -> Dict[str, Any]:
    return _run("click", [f"x={x}", f"y={y}", f"button={button}", f"count={int(count)}"])


def drag(from_x: float, from_y: float, to_x: float, to_y: float,
         *, duration_ms: int = 400,
         cancel_file: Optional[str] = None) -> Dict[str, Any]:
    args = [f"from_x={from_x}", f"from_y={from_y}",
            f"to_x={to_x}", f"to_y={to_y}",
            f"duration_ms={int(duration_ms)}"]
    if cancel_file:
        args += ["--cancel-file", cancel_file]
    return _run("drag", args)


def scroll(x: float, y: float, *, lines: int = 3,
           horizontal: bool = False) -> Dict[str, Any]:
    args = [f"x={x}", f"y={y}", f"lines={int(lines)}"]
    if horizontal:
        args.append("horizontal=true")
    return _run("scroll", args)


# -- keyboard -----------------------------------------------------------------

def type_text(text: str, *, cancel_file: Optional[str] = None) -> Dict[str, Any]:
    args = [f"text={text}"]
    if cancel_file:
        args += ["--cancel-file", cancel_file]
    return _run("type", args)


def release_all() -> Dict[str, Any]:
    """Release any synthetic button or modifier the executor left held.

    Recovery, not a normal action: a drag that is cancelled or dies part-way can
    leave the physical button logically down, and every later pointer action then
    behaves as if a drag is in progress. Calling this costs nothing when nothing
    is held.
    """
    return _run("release_all")


def press_key(key: str, modifiers: Optional[List[str]] = None) -> Dict[str, Any]:
    args = [f"key={key}"]
    if modifiers:
        args.append("modifiers=" + ",".join(modifiers))
    return _run("key", args)


# -- accessibility ------------------------------------------------------------

def focus_application(application: str = "", window: str = "") -> Dict[str, Any]:
    args = [f"application={application}"]
    if window:
        args.append(f"window={window}")
    return _run("focus", args)


def inspect(application: str = "", *, depth: int = 6) -> Dict[str, Any]:
    return _run("inspect", [f"application={application}", f"depth={int(depth)}"],
                timeout=DEFAULT_TIMEOUT_S * 2)


def read_element(application: str, keyword: str, *, role: str = "") -> Dict[str, Any]:
    return _run("read_element", [f"application={application}",
                                 f"keyword={keyword}", f"role={role}"])


def press_element(application: str, keyword: str, *, role: str = "") -> Dict[str, Any]:
    return _run("press_element", [f"application={application}",
                                  f"keyword={keyword}", f"role={role}"])


def verify_change(application: str, keyword: str, *, key: str, value: str,
                  role: str = "") -> Dict[str, Any]:
    """Re-read one element and compare it with what the action claimed.

    This is the only function whose ``verified`` is allowed to mean "the UI
    changed". Everything else reports ``performed_unverified``.
    """
    return _run("verify_change", [f"application={application}",
                                  f"keyword={keyword}", f"role={role}",
                                  f"key={key}", f"value={value}"])


# -- mission integration ------------------------------------------------------

#: Results that mean the executor could not act at all, as opposed to acting and
#: failing to observe. The runner treats them differently.
NOT_ACTED = frozenset({"permission_required", "blocked", "target_not_found",
                       "target_ambiguous", "cancelled", "timed_out"})


def is_verified(result: Dict[str, Any]) -> bool:
    return str(result.get("status") or "") == "verified"


def is_performed(result: Dict[str, Any]) -> bool:
    """True when an event was accepted, whether or not its effect was observed."""
    return str(result.get("status") or "") in {"verified", "performed_unverified"}