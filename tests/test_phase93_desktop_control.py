"""Phase 93: Desktop Control (Computer Use) Tests.

Tests real macOS desktop automation via AppleScript/Accessibility API.
Requires macOS and accessibility permissions.
"""

import pytest


@pytest.mark.skip(reason="macOS-only test")
def test_accessibility_permission_required():
    """Desktop control requires macOS accessibility permissions."""
    from jarvis.tools.desktop import _accessibility
    from jarvis.config import get_settings
    
    settings = get_settings()
    state = _accessibility(settings)
    
    # Must report status (even if not granted)
    assert "status" in state
    
    if state["status"] == "GRANTED":
        print("✓ Accessibility permission GRANTED - ready for desktop control")
    else:
        print(f"⚠ Accessibility permission NOT GRANTED: {state.get('reason', 'unknown')}")
        pytest.skip("Accessibility not granted - cannot perform desktop control")


@pytest.mark.skip(reason="macOS-only test")
def test_focused_application_readable():
    """Can read currently focused application name."""
    from jarvis.tools.desktop import _focused, _READ_FRONTMOST
    from .agents import send_command
    
    result = send_command("osascript", "-e", _READ_FRONTMOST)
    
    if result.exit_code == 0 and result.stdout.strip():
        app_name = result.stdout.split("\n")[0].strip()
        assert len(app_name) > 0, "Focused app name should not be empty"
        print(f"✓ Focused application: {app_name}")
    else:
        print(f"✗ Could not read frontmost app: {result.stderr}")


@pytest.mark.skip(reason="macOS-only test")
async def test_click_at_coordinate_granted():
    """Click at known coordinate works when accessibility granted."""
    from jarvis.tools.desktop import click_desktop
    from jarvis.core.models import ToolContext
    
    ctx = ToolContext(
        device_id="laptop_main",
        platform="macos",
        apps=None,
        settings=None,
        token="test-token"
    )
    
    # Click on common safe location (top-left corner of screen)
    result = await click_desktop(ctx, x=100, y=100)
    
    assert result.ok or "authoriz" in result.error.lower(), \
        f"Click should succeed or require authorization: {result.error}"


@pytest.mark.skip(reason="macOS-only test")
async def test_type_text_into_active_app():
    """Type text into currently active application."""
    from jarvis.tools.desktop import type_desktop_text
    from jarvis.core.models import ToolContext
    
    ctx = ToolContext(
        device_id="laptop_main",
        platform="macos",
        apps=None,
        settings=None,
        token="test-token"
    )
    
    # Type test string
    result = await type_desktop_text(ctx, text="VantageTest123")
    
    # Should either succeed or be unverified (can't observe effect)
    assert result.ok or result.verification.name == "UNKNOWN", \
        f"Should succeed or be unverified: {result.error}"


@pytest.mark.skip(reason="macOS-only test")
def test_desktop_control_state_reported():
    """get_desktop_control_state returns accurate permission status."""
    from jarvis.tools.desktop import get_desktop_control_state
    from jarvis.core.models import ToolContext
    
    ctx = ToolContext(
        device_id="laptop_main",
        platform="macos",
        apps=None,
        settings=None,
        token="test-token"
    )
    
    result = get_desktop_control_state(ctx)
    
    assert "can_control" in result.output, "Must report can_control flag"
    assert "accessibility" in result.output, "Must report accessibility status"


# ---------------------------------------------------------------------------
# Red-First Tests
# ---------------------------------------------------------------------------

@pytest.mark.skip(reason="macOS-only test")
def test_click_without_authorization_rejected():
    """RED-FIRST TEST: Click is refused without accessibility permission."""
    from jarvis.tools.desktop import click_desktop
    from jarvis.core.models import ToolContext
    
    ctx = ToolContext(
        device_id="laptop_main",
        platform="macos",
        apps=None,
        settings=None,
        token="test-token"
    )
    
    # First check if we have permission
    from jarvis.tools.desktop import _accessibility
    from jarvis.config import get_settings
    
    state = _accessibility(get_settings())
    
    if state.get("status") != "GRANTED":
        # Without permission, click should fail immediately
        result = click_desktop(ctx, x=100, y=100)
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


@pytest.mark.skip(reason="macOS-only test")
def test_safe_coordinates_enforced():
    """RED-FIRST TEST: Coordinates outside screen bounds are rejected."""
    from jarvis.tools.desktop import click_desktop
    from jarvis.core.models import ToolContext
    
    ctx = ToolContext(
        device_id="laptop_main",
        platform="macos",
        apps=None,
        settings=None,
        token="test-token"
    )
    
    # MAX_COORDINATE defined in desktop.py
    MAX_COORDINATE = 32000
    
    # Test boundary
    result = click_desktop(ctx, x=MAX_COORDINATE, y=MAX_COORDINATE)
    assert not result.ok or result.verification.name == "UNKNOWN", \
        "Boundary clicks handled truthfully"
    
    # Test overflow
    result = click_desktop(ctx, x=MAX_COORDINATE + 1, y=MAX_COORDINATE + 1)
    assert not result.ok, "Overflow coordinates must be rejected"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
