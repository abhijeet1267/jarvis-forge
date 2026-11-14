"""The Work page's automations section — the templates, run from the page.

`test_r117_workflow_templates_run.py` covers the API and the handlers. This is
the surface: a person opens `/work`, sees the eight pre-built automations, and
runs one, and the page reports the real outcome (status, step count) or the
real reason it did not run.

Runs against the isolated server, never the live backend.
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
    not _playwright_available(),
    reason="playwright is not installed here (see docs/RELEASE_AND_BUILD.md)",
)


@playwright_required
def test_the_work_page_lists_the_real_automations(isolated_server):
    from playwright.sync_api import sync_playwright

    base = isolated_server["base"]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(f"{base}/work", wait_until="networkidle")
        page.wait_for_timeout(1500)

        text = page.locator("#templates").inner_text()
        assert "Loading…" not in text, text
        # The names the API advertises, now actually present and runnable.
        for name in ("System status", "Run tests", "Check build"):
            assert name in text, (name, text)
        # One Run control per automation, not a dead list of names.
        assert page.locator("#templates button").count() >= 8
        browser.close()


@playwright_required
def test_running_an_automation_reports_the_real_outcome(isolated_server):
    from playwright.sync_api import sync_playwright

    base = isolated_server["base"]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(f"{base}/work", wait_until="networkidle")
        page.wait_for_timeout(1500)

        # Run the template the page lists by name, so the assertion does not
        # depend on which automation happens to sort first.
        run = page.locator("#templates div", has_text="System status").locator("button")
        run.first.click()
        page.wait_for_timeout(3000)

        result = page.locator("#template-run").inner_text()
        err = page.locator("#template-err").inner_text()
        assert not err, f"the automations section reported an error: {err}"
        assert "System status" in result, result
        # The real outcome: a status and a step count, not a bare "done".
        assert "status" in result, result
        assert "step" in result, result
        browser.close()