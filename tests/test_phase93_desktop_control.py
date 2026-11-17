"""Phase 93: Desktop Control (Computer Use) Tests.

Tests real macOS desktop automation via AppleScript/Accessibility API.
Requires macOS and accessibility permissions.
"""

import sys

import pytest

# macOS-only means "skip where macOS is absent", not "never run". These were
# unconditionally skipped, so seven desktop-control tests never executed on the
# one platform they were written for -- and two of them had drifted onto an API
# that no longer exists, which is exactly what running them would have caught.
requires_macos = pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only test")

def _ctx():
    """A ToolContext built from the fields that actually exist.

    These tests used to pass `platform=` and `token=`, which are not fields -- so
    they could not have run even if the blanket skip had been lifted. Kept in one
    place so the next signature change is one edit, not five.
    """
    from jarvis.config import get_settings
    from jarvis.tools.base import ToolContext

    return ToolContext(device_id="laptop_main", settings=get_settings())



@requires_macos
def test_accessibility_permission_required():
    """Desktop control requires macOS accessibility permissions."""
    from jarvis.tools.desktop import _accessibility
    from jarvis.config import get_settings
    from jarvis.tools.base import ToolContext

    state = _accessibility(ToolContext(settings=get_settings(), device_id="laptop"))
    
    # Must report status (even if not granted)
    assert "status" in state
    
    if state["status"] == "GRANTED":
        print("✓ Accessibility permission GRANTED - ready for desktop control")
    else:
        print(f"⚠ Accessibility permission NOT GRANTED: {state.get('reason', 'unknown')}")
        pytest.skip("Accessibility not granted - cannot perform desktop control")


@requires_macos
def test_focused_application_readable():
    """Can read currently focused application name."""
    from jarvis.tools.desktop import _focused, _READ_FRONTMOST, _osascript

    # `from .agents import send_command` referenced a helper that no longer exists,
    # which is why this could never have run. `_osascript` is the seam the module
    # actually provides.
    code, out = _osascript(_READ_FRONTMOST, [])
    if code == 0 and out.strip():
        app_name = _focused((code, out))
        assert app_name, "the frontmost application name should not be empty"


@requires_macos
async def test_click_at_coordinate_granted():
    """Click at known coordinate works when accessibility granted."""
    from jarvis.tools.desktop import click_desktop

    result = await click_desktop(_ctx(), x=100, y=100)
    
    # A click is permitted, or it is refused for want of authority. The refusal
    # wording varies: AppleScript reports `-25200` (errAEEventNotPermitted) when
    # macOS Automation access is missing, and the tool's own text says "refused".
    # Matching only the word "authoriz" rejected a real, correctly-reported
    # permission denial -- the test was wrong, not the code.
    text = str(result.error or "").lower()
    refused_for_authority = any(marker in text for marker in
                                ("authoriz", "refused", "-25200", "permission"))
    assert result.ok or refused_for_authority, (
        f"a click must either succeed or be refused for want of authority, "
        f"not fail for another reason: {result.error}")


@requires_macos
async def test_type_text_into_active_app():
    """Type text into currently active application."""
    from jarvis.tools.desktop import type_desktop_text

    result = await type_desktop_text(_ctx(), text="VantageTest123")
    
    # Should either succeed or be unverified (can't observe effect)
    assert result.ok or result.verification.name == "UNKNOWN", \
        f"Should succeed or be unverified: {result.error}"


@requires_macos
def test_desktop_control_state_reported():
    """get_desktop_control_state returns accurate permission status."""
    import asyncio

    from jarvis.tools.desktop import get_desktop_control_state

    result = asyncio.run(get_desktop_control_state(_ctx()))
    
    assert "can_control" in result.output, "Must report can_control flag"
    assert "accessibility" in result.output, "Must report accessibility status"


# ---------------------------------------------------------------------------
# Red-First Tests
# ---------------------------------------------------------------------------

@requires_macos
def test_click_without_authorization_rejected():
    """RED-FIRST TEST: Click is refused without accessibility permission."""
    from jarvis.tools.desktop import click_desktop

    # First check if we have permission
    from jarvis.tools.desktop import _accessibility
    from jarvis.config import get_settings
    
    state = _accessibility(_ctx())

    if state.get("status") != "GRANTED":
        import asyncio
        # Without permission, click should fail immediately.
        result = asyncio.run(click_desktop(_ctx(), x=100, y=100))
        assert not result.ok, "Click should fail without accessibility permission"
        assert "refusal" in str(result.error).lower() or "authoriz" in str(result.error).lower(), \
            f"Error should indicate refusal: {result.error}"


def test_unverified_when_cannot_observe_effect():
    """A keystroke whose effect cannot be read back is UNVERIFIED, never SUCCESS.

    §26.1 names this as the threat: typing into a field Vantage cannot see into, then
    saying "typed," is the fake-success message to defend against. The previous test
    was a bare ``pass`` ("implementation verified in desktop.py") and was
    ``@pytest.mark.skip``'d as "macOS-only" — so it never actually ran. It also could
    not run as written: it passed ``platform=``/``token=`` to ``ToolContext``, neither
    of which is a field.

    The invariant here is OS-independent — it is about the *verification contract*
    (an un-observable keystroke must not be reported as success), not about any macOS
    syscall. So it is now hermetic: the three OS seams (``_accessibility``,
    ``_osascript``, ``_focused``) are patched, no real typing / AppleScript /
    accessibility call happens, and it runs on any platform. (The skip decorator that
    was on the vacuous stub is gone for that reason.)
    """
    import asyncio
    from unittest.mock import patch

    from jarvis.tools import desktop
    from jarvis.tools.desktop import type_desktop_text
    from jarvis.tools.base import ToolContext
    from jarvis.core.models import Verification

    ctx = ToolContext(device_id="laptop_main")

    # The un-observable case: the keystroke is "sent" (exit 0) but nothing here can read
    # what appeared in the field (frontmost-app read returns no usable line).
    with patch.object(desktop, "_accessibility", return_value={"status": "GRANTED"}), \
         patch.object(desktop, "_osascript", return_value=(0, "")):
        result = asyncio.run(type_desktop_text(ctx, text="hello"))

    assert result.ok, f"the keystroke was sent, so this is not a failure: {result.error}"
    assert result.verification is Verification.UNKNOWN, \
        f"an un-observable keystroke must be reported unverified, got {result.verification}"
    assert result.verification is not Verification.SUCCESS, \
        "a keystroke Vantage cannot read back must never be reported as verified/success"


@requires_macos
def test_safe_coordinates_enforced():
    """RED-FIRST TEST: Coordinates outside screen bounds are rejected."""
    import asyncio

    from jarvis.tools.desktop import click_desktop

    ctx = _ctx()

    # MAX_COORDINATE defined in desktop.py
    MAX_COORDINATE = 32000
    
    # Test boundary
    result = asyncio.run(click_desktop(ctx, x=MAX_COORDINATE, y=MAX_COORDINATE))
    assert not result.ok or result.verification.name == "UNKNOWN", \
        "Boundary clicks handled truthfully"
    
    # Test overflow -- `click_desktop` is a coroutine, so it must be awaited.
    # Calling it bare returned a coroutine object, and `.ok` on that raised
    # AttributeError: the assertion could never have been evaluating a result.
    overflow = asyncio.run(click_desktop(ctx, x=MAX_COORDINATE + 1, y=MAX_COORDINATE + 1))
    assert not overflow.ok, "Overflow coordinates must be rejected"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
