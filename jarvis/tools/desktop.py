"""Desktop control as a governed capability -- and the honest half of "Computer Use".

Spec §26.2 is one line and it is the whole design: **untrusted model output is data, never
authority.** A Computer Use feature that lets a model decide where the cursor goes has handed the
machine to whatever text happened to be on the screen. So this module splits the work the way the
rest of Vantage splits it:

* **Reading the desktop's reachability is a READ.** It answers "could you click for me right now",
  and the answer is whatever macOS says about this process at the moment it is asked.
* **Clicking and typing are DESTRUCTIVE and confirmation-gated.** They change the frontmost
  application of a live desktop, they are not reversible, and the thing they act on is whoever
  happens to be focused -- which is exactly what "accidental destructive actions" on the §26.1
  threat list means. In this registry every DESTRUCTIVE tool is confirmation-gated, and nothing
  here loosens that to fit a feature.
* **There is no planner route to either write.** A sentence does not raise a click. The confirmation
  card is the door and a person presses it; a mis-parse must not be able to move a cursor.
* **Nothing is claimed that was not observed.** After an event, the answer is built from a re-read.
  If the re-read is impossible, the answer is UNVERIFIED with the reason -- the oldest rule in this
  project (a handler returning is not the world changing) applied to the most irreversible action
  the product can take.

**What has not been done, and is not claimed.** No click and no keystroke has ever been sent by
this code. That is not because the OS would have refused: measured on 2026-10-05, Accessibility is
`GRANTED` to the python process launched from a terminal (the live proof's own reader answered
"the focused application is Qoder"), while the delivered `.app` reports
`REQUIRES_MANUAL_SYSTEM_SETTINGS` for itself -- the same process-dependent subject that Phase 50
made every permission answer name. So the gate that would actually let a click through is the
confirmation card, not the permission, and the proof run replaced the sender with a recorder that
returns nothing.

The reason it was not demonstrated for real is on the §26.1 threat list under "accidental
destructive actions": a click lands on whatever is focused in *the user's* session, and there is no
undo. Proving it would mean moving their cursor inside their applications, which is their decision
to ask for, not a verification step an agent takes by default.
"""

from __future__ import annotations

import subprocess
from typing import Any, Dict, List, Optional, Tuple

from ..core.models import RiskLevel, ToolSpec
from ..logging_setup import get_logger
from .base import ToolContext, fail, unverified, ok

log = get_logger("tools.desktop")

#: Coordinates are validated before anything is sent. A screen is a finite place, and a value
#: outside it is a bug or an injection attempt rather than a click.
MAX_COORDINATE = 32000

#: One keystroke call is bounded because the text is passed to another process as an argument, and
#: an unbounded payload is a way to make a governed action behave like a pipe.
MAX_TEXT_CHARS = 500

#: What each accessibility state means when a person asks why nothing was clicked.
_REFUSAL = {
    "GRANTED": "",
    "DENIED": "macOS has Vantage listed in Accessibility with the switch off, so no event can be "
              "sent. Turn it on for Vantage and try again.",
    "REQUIRES_MANUAL_SYSTEM_SETTINGS":
        "macOS will not answer this from a helper process, and nothing has been granted to Vantage "
        "yet. Add it in System Settings → Privacy & Security → Accessibility, then quit and reopen "
        "Vantage -- the grant is read at launch.",
    "NOT_DETERMINED": "The Accessibility state could not be read, so nothing was sent. A guess is "
                      "not a permission check.",
    "UNKNOWN": "The Accessibility state is unknown, so nothing was sent.",
}

# The three scripts are `on run argv` programs: every value arrives as an argument and is never
# interpolated into the source. `keystroke (item 1 of argv)` cannot execute a quoted suffix the
# way an f-string would, which is the difference between a governed action and a shell.
_READ_FRONTMOST = """on run argv
    tell application "System Events" to get name of first process whose frontmost is true
end run"""

_CLICK = """on run argv
    set theX to (item 1 of argv) as integer
    set theY to (item 2 of argv) as integer
    tell application "System Events" to click at {theX, theY}
end run"""

_TYPE = """on run argv
    tell application "System Events" to keystroke (item 1 of argv)
end run"""


def _osascript(script: str, args: List[str], timeout: float = 8.0) -> Tuple[int, str]:
    """Run one AppleScript with its values as arguments. The seam every test replaces.

    Kept as a module function rather than inlined so the refusal paths can be proven without an
    event ever reaching a desktop.
    """
    completed = subprocess.run(["osascript", "-e", script, *args],
                               capture_output=True, text=True, timeout=timeout)
    return completed.returncode, (completed.stdout or completed.stderr).strip()


def _focused(sent: Tuple[int, str]) -> str:
    """The frontmost application's name, or nothing.

    A one-line bound is not fussiness: `osascript` writes some failures to stdout with a zero
    exit, and a whole error block would otherwise be reported to the user as the name of the
    application their click landed on. Anything that is not a single plausible line reads as
    "could not see", which the callers answer as unverified.
    """
    code, out = sent
    if code != 0:
        return ""
    first = (out or "").strip().splitlines()[0].strip() if (out or "").strip() else ""
    return first[:120]


def _accessibility(ctx: ToolContext) -> Dict[str, Any]:
    """What macOS says about *this* process right now, from the service that already reads it."""
    from ..permissions.service import PermissionStatusService

    try:
        return PermissionStatusService(ctx.settings).by_capability("accessibility")
    except Exception as exc:  # an unreadable permission is not a granted one
        log.info("accessibility state unreadable", extra_fields={"error": str(exc)[:160]})
        return {"status": "UNKNOWN", "error": str(exc)[:160]}


def _refusal(status: str) -> str:
    return _REFUSAL.get(status, _REFUSAL["UNKNOWN"])


async def get_desktop_control_state(ctx: ToolContext, **_: Any) -> Any:
    """Whether Vantage could click or type, and what it would act on if it did."""
    state = _accessibility(ctx)
    status = str(state.get("status") or "UNKNOWN")
    can_control = status == "GRANTED"
    # Phase 50's rule, applied to a capability that arrived after it: macOS answers a permission
    # question about the process that asks, and Phase 61 measured the same question answering
    # GRANTED from a terminal and refused for the app. A state without its subject is a claim
    # about the product drawn from a reading of something else.
    about_app = state.get("about_bundled_host")
    subject = ("— read from Vantage.app itself" if about_app is True
               else "— this is the answer for the process that asked, not the app in your Dock"
               if about_app is False else "— the reader could not say which process it describes")
    note = str(state.get("status_note") or "")
    target = ""
    if can_control:
        target = _focused(_osascript(_READ_FRONTMOST, []))
    if can_control and not target:
        # Granted, and yet the read that uses it returned nothing. "Ready" would be a claim about
        # a path that just failed to work.
        return unverified("get_desktop_control_state",
                          f"Accessibility is granted {subject}, but the focused application could "
                          "not be read, so desktop control is not confirmed working either.",
                          device=ctx.device_id, accessibility=status, focused="",
                          about_bundled_host=about_app, status_note=note)
    return ok("get_desktop_control_state",
              (f"Desktop control is available {subject}; the focused application is {target}."
               if target else
               f"Desktop control is not available {subject}. {_refusal(status)}"),
              device=ctx.device_id, accessibility=status, focused=target,
              can_control=can_control, about_bundled_host=about_app, status_note=note)


async def click_desktop(ctx: ToolContext, x: Optional[int] = None, y: Optional[int] = None,
                        **_: Any) -> Any:
    """One click at a coordinate, after the person has approved it, with the OS asked first."""
    if x is None or y is None:
        return fail("click_desktop", "A click needs both an x and a y coordinate.",
                    device=ctx.device_id)
    try:
        point = (int(x), int(y))
    except (TypeError, ValueError):
        return fail("click_desktop", "Click coordinates have to be whole numbers.",
                    device=ctx.device_id)
    if not all(0 <= value <= MAX_COORDINATE for value in point):
        return fail("click_desktop",
                    f"({point[0]}, {point[1]}) is outside any screen this build will write to.",
                    device=ctx.device_id)

    state = _accessibility(ctx)
    status = str(state.get("status") or "UNKNOWN")
    if status != "GRANTED":
        # Refused before anything was sent: no event, no partial effect, and the reason is the
        # permission's own state rather than a generic failure.
        return fail("click_desktop", f"I did not click. {_refusal(status)}",
                    device=ctx.device_id, accessibility=status)

    before = _focused(_osascript(_READ_FRONTMOST, []))
    code, out = _osascript(_CLICK, [str(point[0]), str(point[1])])
    if code != 0:
        return fail("click_desktop",
                    f"macOS refused the click: {out[:200] or 'no reason given'}",
                    device=ctx.device_id, accessibility=status)

    after = _focused(_osascript(_READ_FRONTMOST, []))
    if not after:
        return unverified("click_desktop",
                          f"The click was sent to ({point[0]}, {point[1]}), and nothing came back "
                          "to confirm what it landed on, so this is reported unverified rather "
                          "than done.",
                          device=ctx.device_id, accessibility=status)
    return ok("click_desktop",
              f"Clicked at ({point[0]}, {point[1]}); {after} is the application in front now.",
              device=ctx.device_id, accessibility=status, focused=after,
              focused_before=before)


async def type_desktop_text(ctx: ToolContext, text: str = "", **_: Any) -> Any:
    """Type into whatever is focused -- only after approval, and only where the OS allows it."""
    if not text:
        return fail("type_desktop_text", "There was no text to type.", device=ctx.device_id)
    if len(text) > MAX_TEXT_CHARS:
        return fail("type_desktop_text",
                    f"That is {len(text)} characters; one keystroke call carries at most "
                    f"{MAX_TEXT_CHARS}.", device=ctx.device_id)

    state = _accessibility(ctx)
    status = str(state.get("status") or "UNKNOWN")
    if status != "GRANTED":
        return fail("type_desktop_text", f"I did not type. {_refusal(status)}",
                    device=ctx.device_id, accessibility=status)

    code, out = _osascript(_TYPE, [text])
    if code != 0:
        return fail("type_desktop_text", f"macOS refused the keystroke: {out[:200]}",
                    device=ctx.device_id, accessibility=status)

    focused = _focused(_osascript(_READ_FRONTMOST, []))
    if not focused:
        # Typing has no observable effect a re-read can reach from here: the characters went to a
        # field Vantage cannot see into. Saying "typed" would be the fake-success message §26.1
        # names as a threat to defend against.
        return unverified("type_desktop_text",
                          f"The keystroke was sent to the application in front, and nothing here "
                          "can read what appeared in its text field, so this is reported "
                          "unverified rather than done.",
                          device=ctx.device_id, accessibility=status)
    return unverified("type_desktop_text",
                      f"The keystroke was sent while {focused} was in front. Vantage cannot read "
                      f"the field it went into, so whether the text appeared is not something it "
                      "can confirm.",
                      device=ctx.device_id, accessibility=status, focused=focused)


SPECS = [
    (
        ToolSpec(
            name="get_desktop_control_state",
            description=(
                "Report whether macOS will let Vantage click or type on this desktop right now, "
                "and which application would receive it."
            ),
            risk=RiskLevel.READ,
            parameters={},
            tags=["system", "read", "desktop"],
        ),
        get_desktop_control_state,
    ),
    (
        ToolSpec(
            name="click_desktop",
            description=(
                "Send one mouse click to a screen coordinate through macOS. Always asks first: "
                "it acts on whatever is focused, and nothing undoes it."
            ),
            risk=RiskLevel.DESTRUCTIVE,
            requires_confirmation=True,
            reversible=False,
            parameters={"x": "integer", "y": "integer"},
            required_parameters=("x", "y"),
            tags=["system", "desktop", "actuation"],
        ),
        click_desktop,
    ),
    (
        ToolSpec(
            name="type_desktop_text",
            description=(
                "Type text into the application macOS has in front, through System Events. "
                "Always asks first, and reports unverified unless something observable changed."
            ),
            risk=RiskLevel.DESTRUCTIVE,
            requires_confirmation=True,
            reversible=False,
            parameters={"text": "string"},
            required_parameters=("text",),
            tags=["system", "desktop", "actuation"],
        ),
        type_desktop_text,
    ),
]
