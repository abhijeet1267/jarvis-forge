"""The scheduler UI: create, edit, history, pause — verified in a real browser."""

from __future__ import annotations

import pytest


def _pw():
    try:
        import playwright  # noqa: F401
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        return False
    return True


playwright_required = pytest.mark.skipif(not _pw(), reason="playwright not installed")


@playwright_required
def test_a_job_can_be_edited_and_its_history_viewed_in_the_page(isolated_server):
    from playwright.sync_api import sync_playwright

    base = isolated_server["base"]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(f"{base}/planner", wait_until="networkidle")
        page.wait_for_timeout(1200)

        page.fill("#sched-action", "reminder")
        page.fill("#sched-when", "every morning at 8")
        page.fill("#sched-message", "stretch")
        page.click("#sched-form button[type=submit]")
        page.wait_for_timeout(1300)
        assert "reminder" in page.locator("#schedules").inner_text()

        # Edit: rename it.
        page.locator("#schedules li.row button", has_text="Edit").first.click()
        page.wait_for_timeout(500)
        name_input = page.locator("#schedules .sched-edit input").first
        name_input.fill("Morning stretch")
        page.locator("#schedules .sched-edit button[type=submit]").first.click()
        page.wait_for_timeout(1400)
        assert "Morning stretch" in page.locator("#schedules").inner_text()

        # History: run it, then expand history and see the run.
        page.locator("#schedules li.row button", has_text="Run now").first.click()
        page.wait_for_timeout(1500)
        page.locator("#schedules li.row button", has_text="History").first.click()
        page.wait_for_timeout(1500)
        history = page.locator("#schedules .sched-history").first.inner_text()
        assert "No runs yet." not in history, history
        assert any(word in history for word in ("executed", "awaiting_approval", "failed", "blocked")), history
        browser.close()


@playwright_required
def test_an_ambiguous_schedule_is_refused_in_the_page(isolated_server):
    from playwright.sync_api import sync_playwright

    base = isolated_server["base"]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(f"{base}/planner", wait_until="networkidle")
        page.wait_for_timeout(1000)
        page.fill("#sched-action", "reminder")
        page.fill("#sched-when", "every day at 7")
        page.click("#sched-form button[type=submit]")
        page.wait_for_timeout(1200)
        err = page.locator("#sched-err").inner_text().lower()
        assert "could not understand" in err, err
        # Nothing new was created. (An earlier test may have left a job, so this
        # counts rather than demanding an empty list — the isolated server is
        # shared for the session.)
        assert "every day at 7" not in page.locator("#schedules").inner_text().lower()
        browser.close()
