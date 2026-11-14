"""Phase 93 Pre-Check: macOS Accessibility Permissions Status.

A pre-check, not a capability test: it reports whether the macOS Accessibility
permission that Phase 93 (desktop control) depends on is granted to *this*
process, and prints the exact steps to grant it if not. Granting the permission
is a System Settings action only the user can perform, so this file never
asserts the permission is present -- it only asserts the pre-check runs and
reports a coherent state.
"""

from jarvis.config import get_settings
from jarvis.tools.base import ToolContext
from jarvis.tools.desktop import get_desktop_control_state


def _context() -> ToolContext:
    """A minimal tool context the desktop tools need (settings + identity)."""
    return ToolContext(
        device_id="laptop_main",
        session_id="phase93_precheck",
        request_id="REQ-precheck",
        settings=get_settings(),
        apps=None,
        devices=None,
        core=None,
        memory=None,
        graph=None,
    )


async def _state() -> dict:
    """The desktop-control state the pre-check reports, as a plain dict."""
    result = await get_desktop_control_state(_context())
    return result.output or {}


async def check_preconditions() -> bool:
    """Run the accessibility pre-check. Returns True iff desktop control is ready.

    Uses a tool context (not bare settings): the desktop tools read the permission
    state through the permission service, which needs a context to scope the read.
    """
    output = await _state()
    status = str(output.get("accessibility", "UNKNOWN"))
    can_control = bool(output.get("can_control", False))

    print("=" * 60)
    print("PHASE 93: DESKTOP CONTROL PRE-CHECK")
    print("=" * 60)
    print(f"Accessibility Status: {status}")
    print(f"Subject: {output.get('about_bundled_host', 'unknown')}")
    print(f"Can Control Desktop: {can_control}")
    print()

    if can_control and status == "GRANTED":
        print("ACCESSIBILITY GRANTED - Ready for Phase 93 live verification")
        return True

    print("ACCESSIBILITY NOT GRANTED (or not readable from this process)")
    print()
    print("To enable desktop control:")
    print("1. Open System Settings")
    print("2. Privacy & Security -> Accessibility (and Automation)")
    print("3. Find Vantage.app and enable it")
    print("4. Restart the application")
    print()
    print("Phase 93 is BLOCKED on this permission until it is granted.")
    return False


async def test_precheck_runs_and_reports_a_coherent_state():
    """The pre-check is the only thing Phase 93 can do without the permission, so
    the suite checks that it runs and reports a self-consistent answer -- not that
    the permission is present (granting it is the user's action, and asserting it
    would turn an environment-dependent pre-check into a flaky test)."""
    ready = await check_preconditions()
    assert isinstance(ready, bool), "the pre-check must answer ready-or-not, not raise"

    output = await _state()
    assert output, "the pre-check produced no state to report"
    status = str(output.get("accessibility", ""))
    assert status, "the pre-check reported no accessibility status"


async def test_can_control_matches_granted_state():
    """`can_control` is a boolean function of the granted state, not an independent
    claim: if the tool reports it can control the desktop, the state must say the
    permission is granted, and vice versa. This pins the pre-check to the one
    source of truth (the permission service) so the two cannot drift apart."""
    output = await _state()
    can_control = bool(output.get("can_control", False))
    granted = str(output.get("accessibility", "")) == "GRANTED"
    assert can_control == granted, (
        f"can_control={can_control} disagrees with accessibility state "
        f"{output.get('accessibility')!r} -- the pre-check must derive one from the other")
