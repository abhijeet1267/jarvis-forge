"""Feature 2 — the operator page, driven in a real browser (isolated server).

The page is a thin surface over `/command`, so these checks confirm the wiring
(authenticate, run a command, render the outcome) and — importantly — that a
result is shown as **verified / not verified / failed / needs approval** rather
than as a bare "done".
"""

from __future__ import annotations

import pytest


def _playwright_available() -> bool:
    try:
        import playwright  # noqa: F401
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        return False
    return True


playwright_required = pytest.mark.skipif(
    not _playwright_available(), reason="playwright not installed")


@playwright_required
def test_the_operator_page_connects_and_shows_quick_actions(isolated_server):
    from playwright.sync_api import sync_playwright

    base = isolated_server["base"]
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)[:160]))
        page.goto(f"{base}/operator", wait_until="networkidle")
        page.wait_for_timeout(1200)
        assert page.title() == "Vantage — Operator"
        assert page.locator("#status").inner_text().strip() == "connected"
        assert page.locator("#chips button").count() >= 5
        browser.close()
    assert not errors, errors


@playwright_required
def test_running_a_command_shows_an_outcome_with_a_verdict(isolated_server):
    """A read command's result carries a verdict badge, not a bare 'done'."""
    from playwright.sync_api import sync_playwright

    base = isolated_server["base"]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(f"{base}/operator", wait_until="networkidle")
        page.wait_for_timeout(1000)
        page.fill("#cmd", "what is my battery")
        page.click("#run-form button[type=submit]")
        page.wait_for_timeout(2500)
        text = page.locator("#results").inner_text().lower()
        assert "battery" in text, text
        # A verdict is present — verified, not verified, failed, or needs approval.
        assert any(b in text for b in ("verified", "failed", "needs approval")), text
        browser.close()
