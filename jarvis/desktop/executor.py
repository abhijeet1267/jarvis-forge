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

#: The development bundle a person is told to grant in System Settings.
_DEV_BUNDLE = "VantageDesktopDev.app"


def executor_path() -> Optional[Path]:
    """Where the native executor is, or None.

    Search order is deliberate: an explicit override first (tests and a packaged
    build), then the app bundle, then the in-repo build output. A missing binary
    is reported as a missing capability, never worked around with a slower or
    less honest mechanism.

    The bundle comes *before* the bare binary because they are different code
    identities and only one of them is offered to the person granting access.
    Measured: with the bare binary first, the backend invoked
    ``native/desktop_executor/vantage-desktop`` while the Accessibility grant sat
    on ``VantageDesktopDev.app`` -- so every action was refused as unauthorised
    while System Settings showed the app toggled on. Preferring the bundle makes
    the thing that runs and the thing that is granted the same thing.
    """
    override = os.environ.get("VANTAGE_DESKTOP_EXECUTOR")
    if override and Path(override).is_file():
        return Path(override)

    here = Path(__file__).resolve()
    root = here.parent.parent.parent
    candidates = [
        # The granted identity first: the bundle's own executable.
        root / "native" / "desktop_executor" / _DEV_BUNDLE / "Contents" / "MacOS"
        / _EXECUTABLE_NAME,
        root / "native" / "desktop_executor" / _EXECUTABLE_NAME,
        root / "build" / _EXECUTABLE_NAME,
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


def identity() -> Dict[str, Any]:
    """Which code identity macOS is being asked about.

    A permission grant is attached to a binary, and the binary on disk is not
    always the one a person thinks they approved: an ad-hoc signature does not
    survive a rebuild. Reading the executable path, bundle id and signature state
    from the process that would be doing the acting is what makes a "not granted"
    message actionable rather than vague.
    """
    return _run("identity")


def windows(application: str = "") -> Dict[str, Any]:
    """Every on-screen window and its real global bounds, with no Accessibility.

    Needs only screen-recording, which is cheaper than the post-event access a
    click needs. That makes it the honest way to answer "where is this window?"
    before asking for anything the machine has not yet granted.
    """
    args = [f"application={application}"] if application else []
    return _run("windows", args)


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


def click(x: float, y: float, *, button: str = "left", count: int = 1,
          expect_window_id: Optional[int] = None,
          expect_pid: Optional[int] = None) -> Dict[str, Any]:
    """Post a click, optionally refusing if the intended window has moved.

    The expectation is what makes a resolved target safe to act on: between
    resolving a window and clicking it, the window can move or close, and a
    coordinate click would then land on whatever is beneath. The executor checks
    it before posting; passing nothing keeps the old behaviour.
    """
    args = [f"x={x}", f"y={y}", f"button={button}", f"count={int(count)}"]
    if expect_window_id is not None:
        args.append(f"--expect-window-id={int(expect_window_id)}")
    if expect_pid is not None:
        args.append(f"--expect-pid={int(expect_pid)}")
    return _run("click", args)


def drag(from_x: float, from_y: float, to_x: float, to_y: float,
         *, duration_ms: int = 400,
         cancel_file: Optional[str] = None,
         expect_window_id: Optional[int] = None,
         expect_pid: Optional[int] = None) -> Dict[str, Any]:
    """Drag between two points, optionally refusing if the source window moved.

    The expectation describes where the drag *starts*: the executor checks the
    window under the from-point, so a drag whose source button has gone lands
    nowhere rather than somewhere.
    """
    args = [f"from_x={from_x}", f"from_y={from_y}",
            f"to_x={to_x}", f"to_y={to_y}",
            f"duration_ms={int(duration_ms)}"]
    if cancel_file:
        args += ["--cancel-file", cancel_file]
    if expect_window_id is not None:
        args.append(f"--expect-window-id={int(expect_window_id)}")
    if expect_pid is not None:
        args.append(f"--expect-pid={int(expect_pid)}")
    return _run("drag", args)


def scroll(x: float, y: float, *, lines: int = 3,
           horizontal: bool = False,
           expect_window_id: Optional[int] = None,
           expect_pid: Optional[int] = None) -> Dict[str, Any]:
    """Scroll at a point, optionally refusing if the intended window has moved.

    A wheel event carries no target of its own: it scrolls whatever is under the
    pointer. Without the expectation a recorded scroll replays into whichever
    window happens to sit at those coordinates now.
    """
    args = [f"x={x}", f"y={y}", f"lines={int(lines)}"]
    if horizontal:
        args.append("horizontal=true")
    if expect_window_id is not None:
        args.append(f"--expect-window-id={int(expect_window_id)}")
    if expect_pid is not None:
        args.append(f"--expect-pid={int(expect_pid)}")
    return _run("scroll", args)


# -- keyboard -----------------------------------------------------------------

def type_text(text: str, *, cancel_file: Optional[str] = None,
              expect_app: str = "") -> Dict[str, Any]:
    """Type at the focused field, optionally refusing if the wrong app has focus.

    `expect_app` is what makes typing safe to aim: the keystrokes go wherever
    macOS has focus, and between the click that aimed at a window and the typing,
    focus can move. The executor checks it before asking for permission, so a
    wrong focus is reported as a wrong focus rather than as a permission problem.
    """
    args = [f"text={text}"]
    if expect_app:
        args.append(f"expect_app={expect_app}")
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