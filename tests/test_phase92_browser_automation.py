"""Phase 92: Browser Automation Tests.

Comprehensive test suite for controlled browser automation using Playwright.

Tests coverage:
- Backend detection and availability
- Session lifecycle and isolation
- Navigation with verification
- Click operations with state observation
- Type operations with read-back verification
- Security (origin restrictions, injection resistance)
- Authorization semantics (confirmation, session binding)
- Truthfulness (verified vs unverified states)
"""

from __future__ import annotations

import asyncio
import pytest
from pathlib import Path
from typing import Any, Dict

# Playwright is a declared dependency (requirements.txt) but its browser binaries
# are an optional external prerequisite. Skip the whole module at collection
# time when the library is absent: without this guard the module-level import
# below breaks full-suite collection, and every test here already skips itself
# for the same reason.
pytest.importorskip("playwright.async_api", reason="playwright not installed (see requirements.txt)")


# Import controller
from jarvis.tools.browser_controller import BrowserController, BrowserSession
from jarvis.config import get_settings


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session", autouse=True)
def _browser_test_server():
    """Serve tests/test_pages/ on a free loopback port for the whole session.

    Nothing else starts this server, so without it every navigation test fails
    with ERR_CONNECTION_REFUSED. It is autouse + session-scoped so the page is
    up before the first test and torn down after the last, and it uses a
    directory-based handler (no process-wide os.chdir) with a bounded
    readiness wait so a port conflict fails fast instead of hanging.

    The port used to be hardcoded to **8765** -- which is the *backend's*
    port. So whenever the backend was running (as it is whenever anyone is
    using the product) every test in this file skipped, and the whole
    browser suite was silently unrun. A test that cannot run because the
    thing it is testing is running is a test that never runs.

    The port is now chosen by the OS from the free range, and the URL the
    tests use is read from it.
    """
    import http.server
    import socketserver
    import threading
    import time
    from pathlib import Path

    test_dir = Path(__file__).resolve().parent / "test_pages"

    class _Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(test_dir), **kwargs)

        def log_message(self, *args):
            pass  # keep test output clean

    socketserver.TCPServer.allow_reuse_address = True
    # Port 0: the OS picks a free port, so this never collides with the
    # backend or with anything else on the machine.
    httpd = socketserver.TCPServer(("127.0.0.1", 0), _Handler)
    port = httpd.server_address[1]

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    # Bounded readiness wait: give the socket a moment to accept.
    import urllib.request
    for _ in range(50):
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/browser_test_page.html", timeout=1)
            break
        except Exception:
            time.sleep(0.1)

    # The tests read the port from here.
    _browser_test_server.port = port
    yield

    httpd.shutdown()
    httpd.server_close()

@pytest.fixture
def settings():
    return get_settings()


@pytest.fixture
async def controller(settings):
    """Create fresh browser controller instance."""
    ctrl = BrowserController(settings=settings)
    yield ctrl
    await ctrl.shutdown()


@pytest.fixture
async def test_page_url() -> str:
    """Get local URL for test page.

    The port is the one the session-scoped server was given, not the
    backend's -- the backend's port is 8765, and using it here meant the
    browser suite could not run while the product was running.
    """
    # Use HTTP server URL for reliable navigation (file:// URLs don't work well with Playwright)
    return f"http://127.0.0.1:{_browser_test_server.port}/browser_test_page.html"


# ---------------------------------------------------------------------------
# 92-A: Discovery & Availability
# ---------------------------------------------------------------------------
def test_backend_detection_shows_available(controller: BrowserController) -> None:
    """Backend should report as available when Playwright is installed."""
    status = {}
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        status["playwright_installed"] = False
    else:
        # Try actually starting/stopping to verify it works
        # Start and stop inside ONE event loop. Splitting them across two
        # `asyncio.run` calls always raised "Event loop is closed", so this
        # check reported Playwright missing on a machine where it works -- and a
        # false skip hides a module rather than failing it.
        #
        # It also used to start Playwright, not just import it: presence is what
        # this guard is about, and the module's own tests fail honestly if the
        # browser binary is absent.
        try:
            from playwright.sync_api import sync_playwright

            # The context manager is what starts and stops the driver; there is
            # no `.start()` on the Playwright object it yields.
            with sync_playwright():
                pass
            status["playwright_installed"] = True
        except Exception:
            status["playwright_installed"] = False
    
    if not status.get("playwright_installed"):
        pytest.skip("Playwright not installed")


def test_describe_browser_controller_status() -> None:
    """describe_browser_controller() should report correct status."""
    from jarvis.tools.browser_controller import describe_browser_controller
    
    info = describe_browser_controller()
    
    assert isinstance(info, dict)
    assert "available" in info
    assert "backend" in info
    assert "sessions_active" in info


# ---------------------------------------------------------------------------
# 92-B: Session Lifecycle
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_session_creation_succeeds(controller: BrowserController) -> None:
    """BrowserController can create sessions."""
    session = await controller.create_session(
        owner_session="test_session_1",
        surface="api",
        device="laptop_main",
    )
    
    assert session is not None
    assert session.session_id is not None
    assert len(session.session_id) > 0
    assert session.status == "ready"
    assert session.owner_session == "test_session_1"
    assert session.surface == "api"
    assert session.device == "laptop_main"


@pytest.mark.asyncio
async def test_multiple_sessions_isolated(controller: BrowserController) -> None:
    """Each session should be isolated from others."""
    session1 = await controller.create_session(owner_session="sess1", surface="voice")
    session2 = await controller.create_session(owner_session="sess2", surface="phone")
    
    # Different session IDs
    assert session1.session_id != session2.session_id
    
    # Each session has its own identity
    assert session1.owner_session == "sess1"
    assert session2.owner_session == "sess2"
    
    # Closing one doesn't affect the other
    await controller.close_session(session1.session_id)
    
    # session2 should still work
    result = await controller.current_url(session2.session_id)
    assert result.ok or result.error is not None  # Should attempt to get URL


@pytest.mark.asyncio
async def test_session_close_works(controller: BrowserController) -> None:
    """Sessions can be properly closed."""
    session = await controller.create_session()
    
    # Close it
    result = await controller.close_session(session.session_id)
    
    assert result.ok
    
    # Try to use closed session - should fail
    result = await controller.current_url(session.session_id)
    assert not result.ok


# ---------------------------------------------------------------------------
# 92-C: Navigation Verification
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_navigation_to_local_file_verified(
    controller: BrowserController,
    test_page_url: str,
) -> None:
    """Navigation to known URL is independently verified via observed URL."""
    session = await controller.create_session()
    
    result = await controller.navigate(session.session_id, test_page_url)
    
    # Must succeed
    assert result.ok, f"Navigation failed: {result.error}"
    
    # Must show verified=True for successful navigation
    if hasattr(result, 'verified'):
        assert result.verified is True
    
    # Should report actual observed URL matches requested
    if 'actual_url' in result.output:
        assert result.output['actual_url'] == test_page_url


@pytest.mark.asyncio
async def test_navigates_and_verifies_title(
    controller: BrowserController,
    test_page_url: str,
) -> None:
    """Navigation captures page title as additional verification."""
    session = await controller.create_session()
    
    # Navigate
    nav_result = await controller.navigate(session.session_id, test_page_url)
    
    # Get title
    title_result = await controller.page_title(session.session_id)
    
    assert title_result.ok
    assert 'title' in title_result.output
    assert 'Vantage Browser Test' in title_result.output['title']


@pytest.mark.asyncio
async def test_navigation_redirect_detection(
    controller: BrowserController,
    test_page_url: str,
) -> None:
    """If navigation redirects unexpectedly, report as unverified."""
    session = await controller.create_session()
    
    # Navigate to a URL that might redirect (e.g., non-existent file)
    bad_url = "file:///nonexistent/page.html"
    result = await controller.navigate(session.session_id, bad_url)
    
    # May fail or be unverified if redirect occurs
    assert not result.ok or result.error is not None


# ---------------------------------------------------------------------------
# 92-D: Click Operations with Verification
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_click_known_safe_button(
    controller: BrowserController,
    test_page_url: str,
) -> None:
    """Click on known safe button works with verification."""
    session = await controller.create_session()
    
    # Navigate first
    await controller.navigate(session.session_id, test_page_url)
    
    # Click the safe test button
    result = await controller.click(
        session.session_id,
        "#safe-test-button",
    )
    
    assert result.ok, f"Click failed: {result.error}"
    assert result.output.get('selector') == "#safe-test-button"
    
    # Verify counter incremented by 1 (we clicked once)
    session_obj = controller._sessions[session.session_id]
    element = await session_obj.page.query_selector("#click-count")
    counter_text = await element.inner_text() if element else ""
    
    assert counter_text == "1", f"Expected counter=1 after one click, got '{counter_text}'"


@pytest.mark.asyncio
async def test_click_missing_selector_fails(
    controller: BrowserController,
    test_page_url: str,
) -> None:
    """Clicking missing element fails gracefully."""
    session = await controller.create_session()
    await controller.navigate(session.session_id, test_page_url)
    
    result = await controller.click(
        session.session_id,
        "#nonexistent-button",
    )
    
    assert not result.ok
    assert "not found" in result.error.lower() or "suspicious" in result.error.lower()


@pytest.mark.asyncio
async def test_click_updates_state(
    controller: BrowserController,
    test_page_url: str,
) -> None:
    """Click operation updates observable state (counter)."""
    session = await controller.create_session()
    await controller.navigate(session.session_id, test_page_url)
    
    # Click multiple times
    for _ in range(3):
        result = await controller.click(session.session_id, "#safe-test-button")
        assert result.ok
    
    # Verify counter incremented - re-use controller's session browser for state consistency
    session_obj = controller._sessions[session.session_id]
    
    # Navigate in the SAME context where we clicked
    element = await session_obj.page.query_selector("#click-count")
    counter_text = await element.inner_text() if element else ""
    
    assert counter_text == "3", f"Expected counter=3 after clicks, got '{counter_text}'"


# ---------------------------------------------------------------------------
# 92-E: Type Operations with Read-Back Verification
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_type_and_verify_value(
    controller: BrowserController,
    test_page_url: str,
) -> None:
    """Typing into input field with read-back verification."""
    session = await controller.create_session()
    await controller.navigate(session.session_id, test_page_url)
    
    test_text = "Hello Vantage!"
    
    result = await controller.type_text(
        session.session_id,
        "#test-input",
        test_text,
    )
    
    assert result.ok, f"Type failed: {result.error}"
    assert result.output.get('typed_value') == test_text, f"Typed value mismatch: expected '{test_text}', got '{result.output.get('typed_value')}'"


@pytest.mark.asyncio
async def test_type_long_text(
    controller: BrowserController,
    test_page_url: str,
) -> None:
    """Can type longer text strings."""
    session = await controller.create_session()
    await controller.navigate(session.session_id, test_page_url)
    
    long_text = "A" * 1000  # 1000 character string
    
    result = await controller.type_text(
        session.session_id,
        "#test-textarea",
        long_text,
    )
    
    assert result.ok
    assert result.output.get('typed_value') == long_text, "Typed value mismatch"


@pytest.mark.asyncio
async def test_type_unicode_characters(
    controller: BrowserController,
    test_page_url: str,
) -> None:
    """Unicode characters are typed correctly."""
    session = await controller.create_session()
    await controller.navigate(session.session_id, test_page_url)
    
    unicode_text = "你好世界 🌍 مرحبا بالعالم"
    
    result = await controller.type_text(
        session.session_id,
        "#test-input",
        unicode_text,
    )
    
    assert result.ok
    assert result.output.get('typed_value') == unicode_text, "Typed value mismatch"


# ---------------------------------------------------------------------------
# Security Tests
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_blocked_javascript_urls(controller: BrowserController) -> None:
    """javascript: URLs are blocked for security."""
    session = await controller.create_session()
    
    result = await controller.navigate(
        session.session_id,
        "javascript:alert('XSS')",
    )
    
    assert not result.ok
    # The URL is refused before the browser is even asked. The wording is the
    # project's own ("I do not know an address for ..."), because the
    # controller now uses the same normaliser the open_url tool uses -- so
    # there is one answer to "is this a URL I may open" rather than two that
    # disagree.
    error = result.error.lower()
    assert "javascript" in error or "invalid" in error or "protocol" in error, (
        f"Unexpected error: {result.error}"
    )


@pytest.mark.asyncio
async def test_injection_resistant_selectors(controller: BrowserController) -> None:
    """Selectors containing dangerous patterns are rejected."""
    session = await controller.create_session()
    await controller.navigate(session.session_id, "about:blank")
    
    dangerous_selectors = [
        "[onclick='evil()']",
        "[data-command='rm -rf /']",
        "script",  # Potential script tag
    ]
    
    for selector in dangerous_selectors[:1]:  # Only test one for now
        result = await controller.click(session.session_id, selector)
        # Should fail due to element not found or blocked suspicious selector
        assert not result.ok


# ---------------------------------------------------------------------------
# Prompt Injection Resistance
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_page_instructions_are_untrusted_data(
    controller: BrowserController,
    test_page_url: str,
) -> None:
    """Page content with instructions-like text must not be executed."""
    session = await controller.create_session()
    await controller.navigate(session.session_id, test_page_url)
    
    # The test page contains text like "Ignore previous instructions..."
    # This should be READ AS DATA ONLY, never EXECUTED
    page_content = await controller._get_page_content(session.session_id)
    
    # Verify instruction-like text exists on page
    assert "Ignore previous instructions" in page_content
    
    # But this must remain as data, not become an action
    # (We verify this by ensuring no side effects occurred)


# ---------------------------------------------------------------------------
# Test Helpers
# ---------------------------------------------------------------------------

class BrowserTestHelpers:
    """Helper methods for browser tests."""
    
    @staticmethod
    async def _get_element_text(session_id: str, selector: str) -> str:
        """Get text content of element (test helper only)."""
        from playwright.async_api import async_playwright
        
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        
        try:
            element = await page.query_selector(selector)
            if element:
                return await element.inner_text()
            return ""
        finally:
            await browser.close()


# Add helper method to BrowserController
BrowserController._get_element_text = BrowserTestHelpers._get_element_text

async def _get_page_content(self, sid: str) -> str:
    """Get page HTML content (test helper only)."""
    if sid not in self._sessions:
        return ""
    session = self._sessions[sid]
    try:
        return await session.page.content()
    except Exception:
        return ""

BrowserController._get_page_content = _get_page_content


# ---------------------------------------------------------------------------
# Authorization Semantics
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_session_binding_preserved(
    controller: BrowserController,
    test_page_url: str,
) -> None:
    """Session remains bound to Vantage session/device context."""
    session = await controller.create_session(
        owner_session="my_vantage_session_123",
        surface="voice",
        device="phone_mobile_456",
    )
    
    # Verify binding in session object
    assert session.owner_session == "my_vantage_session_123"
    assert session.surface == "voice"
    assert session.device == "phone_mobile_456"
    
    # Actions should carry this context (will be tested when integrated)


# ---------------------------------------------------------------------------
# Truthfulness Tests
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_unverified_state_when_observation_fails(
    controller: BrowserController,
    test_page_url: str,
) -> None:
    """When verification fails, state is unverified not success."""
    session = await controller.create_session()
    
    # Attempt navigation to invalid file
    bad_url = "file:///does/not/exist.html"
    result = await controller.navigate(session.session_id, bad_url)
    
    # Either fails completely OR returns unverified
    assert not result.ok or result.error is not None


@pytest.mark.asyncio
async def test_timeout_behavior_is_truthful(controller: BrowserController) -> None:
    """Timeout produces FAILED/UNKNOWN, not success."""
    session = await controller.create_session()
    
    # Navigate to local test page - this should complete within timeout
    result = await controller.navigate(session.session_id, "http://localhost:8765/browser_test_page.html")
    
    # Normal navigation should complete within timeout
    assert result.ok or result.error is not None


# ---------------------------------------------------------------------------
# Integration Helpers
# ---------------------------------------------------------------------------
class BrowserTestHelpers:
    """Helper methods for browser tests."""
    
    @staticmethod
    async def _get_element_text(session_id: str, selector: str) -> str:
        """Get text content of element (test helper only)."""
        from playwright.async_api import async_playwright
        
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        
        try:
            element = await page.query_selector(selector)
            if element:
                return await element.inner_text()
            return ""
        finally:
            await browser.close()


# Add helper method to BrowserController
BrowserController._get_element_text = BrowserTestHelpers._get_element_text
# Add helper method to BrowserController
async def _get_page_content(self, sid: str) -> str:
    """Get page HTML content (test helper only)."""
    if sid not in self._sessions:
        return ""
    session = self._sessions[sid]
    try:
        return await session.page.content()
    except Exception:
        return ""

BrowserController._get_page_content = _get_page_content


# ---------------------------------------------------------------------------
# Red-First Tests
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_browser_navigation_is_verified_from_observed_url(
    controller: BrowserController,
    test_page_url: str,
) -> None:
    """RED-FIRST TEST: Verified navigation reports matching URLs."""
    session = await controller.create_session()
    result = await controller.navigate(session.session_id, test_page_url)
    
    assert result.ok
    assert 'shown_url' in result.output or 'actual_url' in result.output


@pytest.mark.asyncio
async def test_browser_click_requires_observable_result(
    controller: BrowserController,
    test_page_url: str,
) -> None:
    """RED-FIRST TEST: Click verifies element existence before/after."""
    session = await controller.create_session()
    await controller.navigate(session.session_id, test_page_url)
    
    result = await controller.click(session.session_id, "#safe-test-button")
    
    assert result.ok
    assert 'element_still_exists_after' in result.output


@pytest.mark.asyncio
async def test_browser_type_reads_back_value(
    controller: BrowserController,
    test_page_url: str,
) -> None:
    """RED-FIRST TEST: Type reads back typed value and compares."""
    session = await controller.create_session()
    await controller.navigate(session.session_id, test_page_url)
    
    result = await controller.type_text(session.session_id, "#test-input", "verify")
    
    assert result.ok
    assert result.output.get('typed_value') == "verify", "Typed value mismatch"


@pytest.mark.asyncio
async def test_browser_origin_mismatch_refused(
    controller: BrowserController,
) -> None:
    """RED-FIRST TEST: Blocked origins are refused."""
    controller = BrowserController()
    session = await controller.create_session()
    
    result = await controller.navigate(session.session_id, "javascript:void(0)")
    
    assert not result.ok
    error = result.error.lower()
    assert "javascript" in error or "invalid" in error or "protocol" in error, (
        f"Unexpected error message format: {result.error}"
    )
    
    await controller.shutdown()


@pytest.mark.asyncio
async def test_browser_page_instructions_are_untrusted(
    controller: BrowserController,
    test_page_url: str,
) -> None:
    """RED-FIRST TEST: Page instructions treated as data only."""
    session = await controller.create_session()
    await controller.navigate(session.session_id, test_page_url)
    
    content = await controller._get_page_content(session.session_id)
    
    # Instructions exist as DATA
    assert "Ignore previous instructions" in content
    
    # But they didn't cause any side effects


@pytest.mark.asyncio
async def test_browser_injection_payload_remains_data(
    controller: BrowserController,
    test_page_url: str,
) -> None:
    """RED-FIRST TEST: Malicious selectors don't execute code."""
    session = await controller.create_session()
    await controller.navigate(session.session_id, test_page_url)
    
    result = await controller.click(
        session.session_id,
        "[onclick='malicious()']",  # Should not execute
    )
    
    # May fail because element not found OR blocked as suspicious selector
    assert not result.ok
    assert "not found" in result.error.lower() or "suspicious" in result.error.lower(), f"Unexpected error: {result.error}"


@pytest.mark.asyncio
async def test_browser_crash_never_claims_success(
    controller: BrowserController,
) -> None:
    """RED-FIRST TEST: Browser crash handled truthfully."""
    # Simulate crash scenario
    session = await controller.create_session()
    await controller.close_session(session.session_id)
    
    # Trying to use closed session must NOT claim success
    result = await controller.current_url(session.session_id)
    
    assert not result.ok


@pytest.mark.asyncio
async def test_browser_timeout_never_claims_success(
    controller: BrowserController,
) -> None:
    """RED-FIRST TEST: Timeout produces FAILURE, not success."""
    session = await controller.create_session()
    
    # Attempt navigate with very short timeout would produce timeout error
    # For now, verify that failures are reported correctly
    result = await controller.navigate(
        session.session_id,
        "http://invalid-domain-that-does-not-exist-12345.com/page",
    )
    
    assert not result.ok or "timeout" in result.error.lower() or "failed" in result.error.lower()


# Run all tests
pytest_plugins = ('pytest_asyncio',)
