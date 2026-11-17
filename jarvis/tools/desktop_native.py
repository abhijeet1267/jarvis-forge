"""Real desktop actuation as governed tools — pointer, keyboard and Accessibility.

This module replaces the AppleScript path in :mod:`jarvis.tools.desktop` for the
operations AppleScript cannot perform. The measurement that forced it, taken on
this machine from a Python process: ``osascript ... click at {x,y}`` exits 1 with
``osascript is not allowed assistive access (-25211)``, so every real click went
through a mechanism macOS had already refused. The native executor in
``native/desktop_executor`` posts through Core Graphics instead, and reports what
macOS actually allows it to do.

Three rules this module keeps, and why each one matters:

* **Risk follows consequence, and confirmation follows risk.** A pointer move is
  visible, reversible and harmless, so it is EXTERNAL. A click or a keystroke
  lands on whatever is focused in the person's live session and nothing undoes
  it, so both are DESTRUCTIVE and need confirmation. Making the move LO
  read-tier would be wrong: it still changes what the user sees.
* **"Performed" is not "verified".** The executor distinguishes an event that was
  accepted from an outcome that was observed, and so does this module. A click
  returns ``unverified`` unless a separate observation says the target changed —
  see :func:`desktop_verify_change`, which is the only tool allowed to say
  ``verified`` about an effect.
* **A missing permission is a result, not an error to swallow.** Every tool
  surfaces ``permission_required`` with the executable that needs the grant and
  what to do, because the alternative — pretending to have acted — is the
  fake-success failure these tools exist to avoid.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from ..core.models import RiskLevel, ToolSpec
from ..desktop import executor
from ..desktop.coordinates import (
    CoordinateError, Screen, parse_drag, parse_point,
)
from ..logging_setup import get_logger
from .base import ToolContext, fail, ok, unverified

log = get_logger("tools.desktop_native")

#: Coordinates are validated before anything is sent. A screen is a finite
#: place, and a value outside it is a bug or an injection attempt.
MAX_COORDINATE = 32000


def _screen() -> Optional[Screen]:
    """The display geometry the executor reports, or None when it cannot say.

    Asked on demand rather than cached: a monitor can be plugged in or removed
    between two actions, and a stale display list would refuse a point that is
    now perfectly valid.
    """
    try:
        posture = executor.permissions()
    except Exception:
        return None
    raw = posture.get("displays")
    if not isinstance(raw, list):
        return None
    return Screen.from_bounds(raw)


def _validated_point(x: Any, y: Any) -> Tuple[float, float]:
    """Validate a screen point against real display geometry.

    Raises :class:`CoordinateError` carrying the sentence to show. Every
    coordinate-taking tool calls this, so a malformed value is refused in the
    same words no matter which door it came through.
    """
    return parse_point(x, y, screen=_screen())


def _validated_drag(from_x: Any, from_y: Any, to_x: Any, to_y: Any
                    ) -> Tuple[float, float, float, float]:
    return parse_drag(from_x, from_y, to_x, to_y, screen=_screen())


#: One keystroke call carries at most this much text: an unbounded payload
#: makes a governed action behave like a pipe into the focused application.
MAX_TEXT_CHARS = 2000

#: The executor results that mean "nothing was attempted", as opposed to
#: "something was attempted and did not work". The distinction decides how the
#: mission runner treats the step.
_NOT_ACTED = executor.NOT_ACTED


def _result_message(result: Dict[str, Any], acted_phrase: str) -> str:
    """Turn an executor result into the sentence the user reads.

    Specialised only where the status changes what the person should do, which
    is almost always the permission cases.
    """
    status = str(result.get("status") or "unknown")
    reason = str(result.get("reason") or "")

    if status == "permission_required":
        return f"I did not act. {reason}"
    if status == "blocked":
        return f"I did not act. {reason}"
    if status == "target_not_found":
        return f"I could not find that. {reason}"
    if status == "target_ambiguous":
        return f"More than one target matched. {reason}"
    if status == "timed_out":
        return f"It did not finish in time. {reason}"
    if status == "verified":
        return reason or f"{acted_phrase}, and the result was observed."
    if status == "performed_unverified":
        return ((reason or f"{acted_phrase}.") +
                " Whether it worked is not something this step could observe.")
    if status == "failed":
        return f"It did not work. {reason}"
    return reason or f"{acted_phrase}."


def _finish(tool: str, result: Dict[str, Any], ctx: ToolContext,
            acted_phrase: str, **extra: Any) -> Any:
    """Map an executor result onto the tool result type the registry expects."""
    status = str(result.get("status") or "unknown")
    message = _result_message(result, acted_phrase)
    payload = {
        "device": ctx.device_id,
        "desktop_status": status,
        "desktop_result": result,
        **extra,
    }
    if status == "verified":
        return ok(tool, message, verified=True, **payload)
    if status == "performed_unverified":
        return unverified(tool, message, **payload)
    # Everything else — permission, blocked, not found, timeout, failed, unknown.
    # `fail` carries its reason in `error`, which is the field the UI and the
    # audit log read for a non-success; passing it as `message` would leave the
    # person looking at an empty row.
    return fail(tool, message, **payload)


# -- pointer ------------------------------------------------------------------

async def desktop_move_pointer(ctx: ToolContext, x: Optional[float] = None,
                               y: Optional[float] = None, smooth: bool = False,
                               duration_ms: int = 0, **_: Any) -> Any:
    """Move the real on-screen pointer to a global display point."""
    if x is None or y is None:
        return fail("desktop_move_pointer",
                    "A pointer move needs both an x and a y coordinate.",
                    device=ctx.device_id)
    try:
        px, py = _validated_point(x, y)
    except CoordinateError as refusal:
        return fail("desktop_move_pointer", str(refusal), device=ctx.device_id,
                    detail=refusal.detail)
    result = executor.move(px, py, smooth=bool(smooth),
                           duration_ms=max(0, min(5000, int(duration_ms or 0))))
    return _finish("desktop_move_pointer", result, ctx,
                   f"Moved the pointer to ({x}, {y})")


async def desktop_click(ctx: ToolContext, x: Optional[float] = None,
                        y: Optional[float] = None, button: str = "left",
                        expect_window_id: Optional[int] = None,
                        **_: Any) -> Any:
    """Post one real mouse click. DESTRUCTIVE: needs confirmation."""
    if x is None or y is None:
        return fail("desktop_click", "A click needs both an x and a y coordinate.",
                    device=ctx.device_id)
    try:
        px, py = _validated_point(x, y)
    except CoordinateError as refusal:
        return fail("desktop_click", str(refusal), device=ctx.device_id,
                    detail=refusal.detail)
    result = executor.click(px, py, button=button, count=1,
                            expect_window_id=expect_window_id)
    return _finish("desktop_click", result, ctx,
                   f"Clicked at ({x}, {y})")


async def desktop_double_click(ctx: ToolContext, x: Optional[float] = None,
                               y: Optional[float] = None, **_: Any) -> Any:
    """Post one double-click (a click with clickState 2, not two clicks)."""
    if x is None or y is None:
        return fail("desktop_double_click",
                    "A double-click needs both an x and a y coordinate.",
                    device=ctx.device_id)
    try:
        px, py = _validated_point(x, y)
    except CoordinateError as refusal:
        return fail("desktop_double_click", str(refusal), device=ctx.device_id,
                    detail=refusal.detail)
    result = executor.click(px, py, button="left", count=2)
    return _finish("desktop_double_click", result, ctx,
                   f"Double-clicked at ({x}, {y})")


async def desktop_right_click(ctx: ToolContext, x: Optional[float] = None,
                              y: Optional[float] = None, **_: Any) -> Any:
    """Post one right-click, which normally opens a context menu."""
    if x is None or y is None:
        return fail("desktop_right_click",
                    "A right-click needs both an x and a y coordinate.",
                    device=ctx.device_id)
    try:
        px, py = _validated_point(x, y)
    except CoordinateError as refusal:
        return fail("desktop_right_click", str(refusal), device=ctx.device_id,
                    detail=refusal.detail)
    result = executor.click(px, py, button="right", count=1)
    return _finish("desktop_right_click", result, ctx,
                   f"Right-clicked at ({x}, {y})")


async def desktop_drag(ctx: ToolContext, from_x: Optional[float] = None,
                       from_y: Optional[float] = None, to_x: Optional[float] = None,
                       to_y: Optional[float] = None, duration_ms: int = 400,
                       expect_window_id: Optional[int] = None,
                       expect_pid: Optional[int] = None,
                       **_: Any) -> Any:
    """Drag from one point to another, releasing the button at the end."""
    missing = [name for name, value in
               (("from_x", from_x), ("from_y", from_y), ("to_x", to_x), ("to_y", to_y))
               if value is None]
    if missing:
        return fail("desktop_drag",
                    "A drag needs from_x, from_y, to_x and to_y. Missing: "
                    + ", ".join(missing), device=ctx.device_id)
    try:
        fx, fy, tx, ty = _validated_drag(from_x, from_y, to_x, to_y)
    except CoordinateError as refusal:
        return fail("desktop_drag", str(refusal), device=ctx.device_id,
                    detail=refusal.detail)
    result = executor.drag(fx, fy, tx, ty,
                           duration_ms=max(50, min(8000, int(duration_ms or 400))),
                           expect_window_id=expect_window_id,
                           expect_pid=expect_pid)
    return _finish("desktop_drag", result, ctx,
                   f"Dragged from ({from_x}, {from_y}) to ({to_x}, {to_y})")


async def desktop_scroll(ctx: ToolContext, x: Optional[float] = None,
                         y: Optional[float] = None, lines: int = 3,
                         horizontal: bool = False,
                         expect_window_id: Optional[int] = None,
                         expect_pid: Optional[int] = None, **_: Any) -> Any:
    """Post a bounded wheel scroll at a point."""
    if x is None or y is None:
        return fail("desktop_scroll", "A scroll needs both an x and a y coordinate.",
                    device=ctx.device_id)
    bounded = max(-50, min(50, int(lines or 0)))
    if bounded == 0:
        return fail("desktop_scroll", "A scroll of zero lines would do nothing.",
                    device=ctx.device_id)
    try:
        px, py = _validated_point(x, y)
    except CoordinateError as refusal:
        return fail("desktop_scroll", str(refusal), device=ctx.device_id,
                    detail=refusal.detail)
    result = executor.scroll(px, py, lines=bounded,
                             horizontal=bool(horizontal),
                             expect_window_id=expect_window_id,
                             expect_pid=expect_pid)
    return _finish("desktop_scroll", result, ctx,
                   f"Scrolled {bounded} line(s) at ({x}, {y})")


# -- keyboard -----------------------------------------------------------------

async def desktop_type_text(ctx: ToolContext, text: str = "",
                            expect_app: str = "", **_: Any) -> Any:
    """Type into the focused field through real keyboard events."""
    if not text:
        return fail("desktop_type_text", "There was no text to type.",
                    device=ctx.device_id)
    if len(text) > MAX_TEXT_CHARS:
        return fail("desktop_type_text",
                    f"That is {len(text)} characters; one call carries at most "
                    f"{MAX_TEXT_CHARS}.", device=ctx.device_id)
    result = executor.type_text(text, expect_app=expect_app)
    return _finish("desktop_type_text", result, ctx,
                   f"Typed {len(text)} character(s) into the focused field")


async def desktop_press_key(ctx: ToolContext, key: str = "",
                            modifiers: Optional[list] = None,
                            expect_app: str = "", **_: Any) -> Any:
    """Press one key, optionally with modifiers (cmd, shift, opt, ctrl)."""
    if not key:
        return fail("desktop_press_key", "No key was named.", device=ctx.device_id)
    if isinstance(modifiers, str):
        modifiers = [part.strip() for part in modifiers.split(",") if part.strip()]
    result = executor.press_key(key, list(modifiers or []), expect_app=expect_app)
    label = "+".join([*(modifiers or []), key])
    return _finish("desktop_press_key", result, ctx, f"Pressed {label}")


async def desktop_shortcut(ctx: ToolContext, modifiers: Optional[list] = None,
                           key: str = "", expect_app: str = "", **_: Any) -> Any:
    """Press a keyboard shortcut. Refuses a shortcut with no modifier.

    A "shortcut" with no modifier is just a keystroke, and sending one through
    this tool would let a caller type into a field while believing it took a
    safe, well-understood action.
    """
    if isinstance(modifiers, str):
        modifiers = [part.strip() for part in modifiers.split(",") if part.strip()]
    modifiers = list(modifiers or [])
    if not modifiers:
        return fail("desktop_shortcut",
                    "A shortcut needs at least one modifier (cmd, shift, opt, "
                    "ctrl). Use desktop_press_key for a bare keystroke.",
                    device=ctx.device_id)
    if not key:
        return fail("desktop_shortcut", "No key was named.", device=ctx.device_id)
    result = executor.press_key(key, modifiers, expect_app=expect_app)
    label = "+".join([*modifiers, key])
    return _finish("desktop_shortcut", result, ctx, f"Pressed {label}")


# -- accessibility ------------------------------------------------------------

async def desktop_focus_window(ctx: ToolContext, application: str = "",
                               window: str = "", **_: Any) -> Any:
    """Bring an application (and optionally a named window) to the front."""
    result = executor.focus_application(application, window)
    return _finish("desktop_focus_window", result, ctx,
                   f"Brought {application or 'the application'} to the front")


async def desktop_inspect_ui(ctx: ToolContext, application: str = "",
                              depth: int = 6, **_: Any) -> Any:
    """Read the Accessibility tree of an application."""
    result = executor.inspect(application, depth=depth)
    return _finish("desktop_inspect_ui", result, ctx,
                   f"Read the Accessibility tree of {application or 'the front application'}")


async def desktop_press_element(ctx: ToolContext, application: str = "",
                                keyword: str = "", role: str = "", **_: Any) -> Any:
    """Press a UI element through its Accessibility action (AXPress).

    Preferred over a coordinate click: it is the interface the application
    itself publishes, so it cannot miss by a pixel and works without knowing
    where the element is on screen.
    """
    if not keyword:
        return fail("desktop_press_element",
                    "A semantic press needs the element's text or label.", device=ctx.device_id)
    result = executor.press_element(application, keyword, role=role)
    return _finish("desktop_press_element", result, ctx,
                   f"Pressed {keyword!r}")


async def desktop_observe(ctx: ToolContext, application: str = "",
                          keyword: str = "", role: str = "", **_: Any) -> Any:
    """Read one element's current state — the observation half of act-then-verify."""
    if not keyword:
        return fail("desktop_observe",
                    "An observation needs the element's text or label.", device=ctx.device_id)
    result = executor.read_element(application, keyword, role=role)
    return _finish("desktop_observe", result, ctx,
                   f"Read {keyword!r}")


async def desktop_verify_change(ctx: ToolContext, application: str = "",
                                keyword: str = "", key: str = "", value: str = "",
                                role: str = "", **_: Any) -> Any:
    """Re-read an element and compare it with what an action claimed.

    This is the only desktop tool whose ``verified`` means the UI changed. It
    performs no action of its own, so it is safe to call as often as a mission
    needs.
    """
    if not (keyword and key):
        return fail("desktop_verify_change",
                    "A change check needs the element's text and the attribute to compare.",
                    device=ctx.device_id)
    result = executor.verify_change(application, keyword, key=key, value=value,
                                    role=role)
    return _finish("desktop_verify_change", result, ctx,
                   f"Checked {keyword!r} for {key} = {value}")


async def desktop_control_probe(ctx: ToolContext, **_: Any) -> Any:
    """Move the pointer in a small, bounded square and report what moved back.

    The "is this real?" test a person can run from the UI. It never clicks, types
    or reads anything, so it is safe on any machine; what it answers is whether
    the executor genuinely moves the system pointer, which is the one capability
    this project has measured working. The readback per leg is the evidence — a
    permission problem or a frozen pointer shows up as a mismatch, not as a
    success sentence.
    """
    posture = executor.permissions()
    if not posture.get("can_move_cursor"):
        return fail("desktop_control_probe",
                    f"Cursor movement is not available to {posture.get('process_name')}: "
                    f"{posture.get('reason')}", device=ctx.device_id)
    origin = posture.get("cursor") or {}
    try:
        ox, oy = float(origin.get("x", 0)), float(origin.get("y", 0))
    except (TypeError, ValueError):
        return fail("desktop_control_probe",
                    "The executor did not report where the pointer is.",
                    device=ctx.device_id)

    # 40 points is visible on screen and small enough not to disturb anything.
    legs = [(ox + 40, oy), (ox + 40, oy + 40), (ox, oy + 40), (ox, oy)]
    checked = []
    for tx, ty in legs:
        result = executor.move(tx, ty)
        if result.get("status") != "verified":
            return unverified(
                "desktop_control_probe",
                f"The probe stopped at ({tx:.0f}, {ty:.0f}): {result.get('reason')}",
                device=ctx.device_id, desktop_result=result, legs_checked=checked)
        after = result.get("cursor_after") or {}
        checked.append({"target": [tx, ty], "observed": [after.get("x"), after.get("y")]})
    return ok("desktop_control_probe",
              f"The pointer moved through {len(checked)} points and each move was "
              f"confirmed by reading its position back. Real cursor control is working "
              f"for {posture.get('process_name')}.",
              device=ctx.device_id, verified=True, legs=checked,
              desktop_result={"process_name": posture.get("process_name"),
                              "executable": posture.get("executable")})


async def desktop_release_input(ctx: ToolContext, **_: Any) -> Any:
    """Release any synthetic button or modifier left held. Recovery only."""
    result = executor.release_all()
    return _finish("desktop_release_input", result, ctx,
                   "Released any held mouse button and modifier")


async def desktop_permissions(ctx: ToolContext, **_: Any) -> Any:
    """Report what macOS actually allows this executable to do, right now."""
    result = executor.permissions()
    status = str(result.get("status") or "unknown")
    payload = {"device": ctx.device_id, "desktop_result": result}
    if status != "verified":
        return unverified("desktop_permissions",
                          f"The permission posture could not be read: {result.get('reason')}",
                          **payload)
    return ok("desktop_permissions",
              (f"Cursor movement: {'yes' if result.get('can_move_cursor') else 'no'}; "
               f"clicking and typing: {'yes' if result.get('can_click') else 'no'}; "
               f"reading the UI: {'yes' if result.get('can_read_ui') else 'no'}; "
               f"screen capture: {'yes' if result.get('screen_capture') else 'no'}. "
               f"These describe {result.get('process_name')} only."),
              **payload)


async def desktop_windows(ctx: ToolContext, application: str = "", **_: Any) -> Any:
    """List on-screen windows and their real global bounds.

    The window-relative half of the coordinate system, and the cheapest one:
    the window server answers with only screen-recording access, so a caller can
    learn where a window is before asking the machine for anything it has not
    granted. Bounds are global points — the same space a click accepts.
    """
    result = executor.windows(application)
    return _finish("desktop_windows", result, ctx,
                   f"Listed the windows of {application or 'every application'}",
                   # Surfaced top-level as well as under `desktop_result`: these
                   # bounds are what a caller aims a click at, and making it dig
                   # through a wrapper to find them invites a re-derivation.
                   windows=result.get("windows") or [])

async def desktop_identity(ctx: ToolContext, **_: Any) -> Any:
    """Report which binary macOS is being asked about, and how it is signed."""
    result = executor.identity()
    # `_finish` maps a failed read onto `fail`. An `unverified()` here would
    # report ok=True for a failed read, because that helper means "ran, outcome
    # unknown" — which is not the same as "the read did not work".
    return _finish("desktop_identity", result, ctx,
                   "Read the executor's identity",
                   note=result.get("grant_caveat") or "")

SPECS = [
    (
        ToolSpec(
            name="desktop_permissions",
            description=(
                "Report what macOS actually allows Vantage to do on this desktop right "
                "now — move the cursor, post clicks and keys, read the UI, capture the "
                "screen — and which executable those answers describe."
            ),
            risk=RiskLevel.READ,
            parameters={},
            tags=["system", "read", "desktop"],
        ),
        desktop_permissions,
    ),
    (
        ToolSpec(
            name="desktop_identity",
            description=(
                "Report which executable macOS is being asked about: its path, bundle "
                "identifier, signing mode and hash, plus the live permission snapshot. "
                "A grant is attached to a binary, so this is what makes a refusal "
                "actionable. Read-only."
            ),
            risk=RiskLevel.READ,
            parameters={},
            tags=["system", "read", "desktop", "diagnostic"],
        ),
        desktop_identity,
    ),
    (
        ToolSpec(
            name="desktop_windows",
            description=(
                "List on-screen windows and their real global bounds in points: which "
                "application owns each, its title, position, size and window id. Needs "
                "only screen-recording access. Read-only — it is how a caller finds out "
                "where a window is before aiming at it."
            ),
            risk=RiskLevel.READ,
            parameters={"application": "string"},
            tags=["system", "read", "desktop", "windows"],
        ),
        desktop_windows,
    ),
    (
        ToolSpec(
            name="desktop_move_pointer",
            description=(
                "Move the real on-screen pointer to a global display point. The move is "
                "verified by reading the pointer position back."
            ),
            # LOW, not EXTERNAL: moving the pointer changes nothing but where the
            # pointer is, and it is reversible by moving it back. The project's
            # invariant is that every EXTERNAL and DESTRUCTIVE tool is gated, and
            # a pointer move is less consequential than `close_app` or
            # `browser_open`, both of which are LOW and ungated. Calling it
            # EXTERNAL while ungated was the incoherence; a click is where the
            # consequence actually starts, and that one is DESTRUCTIVE.
            risk=RiskLevel.LOW,
            requires_confirmation=False,
            reversible=True,
            parameters={"x": "number", "y": "number", "smooth": "boolean",
                        "duration_ms": "integer"},
            required_parameters=("x", "y"),
            tags=["system", "desktop", "pointer"],
        ),
        desktop_move_pointer,
    ),
    (
        ToolSpec(
            name="desktop_click",
            description=(
                "Post one real mouse click at a screen point. Acts on whatever is under "
                "the pointer and cannot be undone, so it always asks first."
            ),
            risk=RiskLevel.DESTRUCTIVE,
            requires_confirmation=True,
            reversible=False,
            parameters={"x": "number", "y": "number", "button": "string",
                        "expect_window_id": "integer", "expect_pid": "integer"},
            required_parameters=("x", "y"),
            tags=["system", "desktop", "pointer", "actuation"],
        ),
        desktop_click,
    ),
    (
        ToolSpec(
            name="desktop_double_click",
            description="Post one real double-click at a screen point. Always asks first.",
            risk=RiskLevel.DESTRUCTIVE,
            requires_confirmation=True,
            reversible=False,
            parameters={"x": "number", "y": "number"},
            required_parameters=("x", "y"),
            tags=["system", "desktop", "pointer", "actuation"],
        ),
        desktop_double_click,
    ),
    (
        ToolSpec(
            name="desktop_right_click",
            description=(
                "Post one real right-click at a screen point, which normally opens a "
                "context menu. Always asks first."
            ),
            risk=RiskLevel.DESTRUCTIVE,
            requires_confirmation=True,
            reversible=False,
            parameters={"x": "number", "y": "number"},
            required_parameters=("x", "y"),
            tags=["system", "desktop", "pointer", "actuation"],
        ),
        desktop_right_click,
    ),
    (
        ToolSpec(
            name="desktop_drag",
            description=(
                "Drag from one screen point to another through real mouse events, with "
                "the button released at the end. Always asks first."
            ),
            risk=RiskLevel.DESTRUCTIVE,
            requires_confirmation=True,
            reversible=False,
            parameters={"from_x": "number", "from_y": "number", "to_x": "number",
                        "to_y": "number", "duration_ms": "integer",
                        "expect_window_id": "integer", "expect_pid": "integer"},
            required_parameters=("from_x", "from_y", "to_x", "to_y"),
            tags=["system", "desktop", "pointer", "actuation"],
        ),
        desktop_drag,
    ),
    (
        ToolSpec(
            name="desktop_scroll",
            description="Post a bounded wheel scroll at a screen point. Always asks first.",
            risk=RiskLevel.DESTRUCTIVE,
            requires_confirmation=True,
            reversible=True,
            parameters={"x": "number", "y": "number", "lines": "integer",
                        "horizontal": "boolean",
                        "expect_window_id": "integer", "expect_pid": "integer"},
            required_parameters=("x", "y"),
            tags=["system", "desktop", "pointer", "actuation"],
        ),
        desktop_scroll,
    ),
    (
        ToolSpec(
            name="desktop_type_text",
            description=(
                "Type text into whatever field macOS has focused, through real keyboard "
                "events. Always asks first. Never used for passwords or security prompts."
            ),
            risk=RiskLevel.DESTRUCTIVE,
            requires_confirmation=True,
            reversible=False,
            parameters={"text": "string"},
            required_parameters=("text",),
            tags=["system", "desktop", "keyboard", "actuation"],
        ),
        desktop_type_text,
    ),
    (
        ToolSpec(
            name="desktop_press_key",
            description="Press one key, with optional modifiers. Always asks first.",
            risk=RiskLevel.DESTRUCTIVE,
            requires_confirmation=True,
            reversible=False,
            parameters={"key": "string", "modifiers": "list", "expect_app": "string"},
            required_parameters=("key",),
            tags=["system", "desktop", "keyboard", "actuation"],
        ),
        desktop_press_key,
    ),
    (
        ToolSpec(
            name="desktop_shortcut",
            description=(
                "Press a keyboard shortcut such as cmd+s. Refuses a 'shortcut' with no "
                "modifier. Always asks first."
            ),
            risk=RiskLevel.DESTRUCTIVE,
            requires_confirmation=True,
            reversible=False,
            parameters={"key": "string", "modifiers": "list", "expect_app": "string"},
            required_parameters=("key", "modifiers"),
            tags=["system", "desktop", "keyboard", "actuation"],
        ),
        desktop_shortcut,
    ),
    (
        ToolSpec(
            name="desktop_focus_window",
            description="Bring an application, and optionally one named window, to the front.",
            risk=RiskLevel.LOW,
            parameters={"application": "string", "window": "string"},
            tags=["system", "desktop", "windows"],
        ),
        desktop_focus_window,
    ),
    (
        ToolSpec(
            name="desktop_inspect_ui",
            description=(
                "Read the Accessibility tree of an application: roles, titles, labels, "
                "values, positions and available actions. Read-only."
            ),
            risk=RiskLevel.READ,
            parameters={"application": "string", "depth": "integer"},
            tags=["system", "read", "desktop", "accessibility"],
        ),
        desktop_inspect_ui,
    ),
    (
        ToolSpec(
            name="desktop_press_element",
            description=(
                "Press a UI element by its label or title through its Accessibility "
                "action (AXPress), which cannot miss by a pixel. Always asks first."
            ),
            risk=RiskLevel.DESTRUCTIVE,
            requires_confirmation=True,
            reversible=False,
            parameters={"application": "string", "keyword": "string", "role": "string"},
            required_parameters=("keyword",),
            tags=["system", "desktop", "accessibility", "actuation"],
        ),
        desktop_press_element,
    ),
    (
        ToolSpec(
            name="desktop_observe",
            description=(
                "Read one UI element's current state by label or title. Read-only; this "
                "is the observation half of act-then-verify."
            ),
            risk=RiskLevel.READ,
            parameters={"application": "string", "keyword": "string", "role": "string"},
            required_parameters=("keyword",),
            tags=["system", "read", "desktop", "accessibility"],
        ),
        desktop_observe,
    ),
    (
        ToolSpec(
            name="desktop_verify_change",
            description=(
                "Re-read one UI element and compare an attribute with an expected value. "
                "The only desktop tool whose 'verified' means the UI actually changed."
            ),
            risk=RiskLevel.READ,
            parameters={"application": "string", "keyword": "string", "key": "string",
                        "value": "string", "role": "string"},
            required_parameters=("keyword", "key", "value"),
            tags=["system", "read", "desktop", "verification"],
        ),
        desktop_verify_change,
    ),
    (
        ToolSpec(
            name="desktop_control_probe",
            description=(
                "Prove real cursor control: move the pointer a short bounded square and "
                "report the position read back after each move. Moves only — never clicks, "
                "types or reads — so it is safe to run on any machine."
            ),
            # LOW and ungated, matching desktop_move_pointer: it is the same act,
            # four times, and refusing it behind a confirmation would make the one
            # capability that works the hardest one to check.
            risk=RiskLevel.LOW,
            requires_confirmation=False,
            reversible=True,
            parameters={},
            tags=["system", "desktop", "pointer", "diagnostic"],
        ),
        desktop_control_probe,
    ),
    (
        ToolSpec(
            name="desktop_release_input",
            description=(
                "Release any mouse button or modifier the executor left held after a "
                "cancelled or failed drag. Recovery only; does nothing when nothing is held."
            ),
            # DESTRUCTIVE because it posts input events, and confirmation-gated for
            # the same reason every other posting tool is -- even though the only
            # thing it can do is undo a stuck state.
            risk=RiskLevel.DESTRUCTIVE,
            requires_confirmation=True,
            reversible=True,
            parameters={},
            tags=["system", "desktop", "recovery"],
        ),
        desktop_release_input,
    ),
]