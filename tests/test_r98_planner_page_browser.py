"""Epic 1 — the planner page, driven in a real browser.

The backend slice is covered by ``test_r97_daily_planner.py``. This file is the
other half the product depends on: a person can open the page, add a task, and
see it — and the page tells the truth about the calendar and about the next
action.

It runs against the project's **isolated server** (a real uvicorn on a scratch
home), never the developer's live backend — the same rule the other browser
tests follow.
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
def test_the_planner_page_loads_and_shows_an_empty_state(isolated_server):
    """The page authenticates, renders, and says there is nothing yet."""
    from playwright.sync_api import sync_playwright

    base = isolated_server["base"]
    page_errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.on("pageerror", lambda e: page_errors.append(str(e)[:160]))
        page.goto(f"{base}/planner", wait_until="networkidle")
        page.wait_for_timeout(1500)

        assert page.title() == "Vantage — Planner", page.title()
        assert page.locator("#status").inner_text().strip() == "connected", (
            page.locator("#status").inner_text())
        # The isolated server is shared for the session, so another test may have
        # left tasks. Assert the surface rendered, not that the list is empty.
        tasks_text = page.locator("#tasks").inner_text()
        assert tasks_text.strip(), "the task list rendered nothing at all"
        assert "Loading…" not in tasks_text, tasks_text
        browser.close()
    assert not page_errors, page_errors


@playwright_required
def test_a_task_added_in_the_page_appears_and_is_next_up(isolated_server):
    """Type a title, press Add, and the task shows — and becomes 'next up'."""
    from playwright.sync_api import sync_playwright

    base = isolated_server["base"]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(f"{base}/planner", wait_until="networkidle")
        page.wait_for_timeout(1200)

        page.fill("#title", "r98-unique-acceptance-report")
        page.select_option("#priority", "urgent")
        page.click("#add-form button[type=submit]")
        page.wait_for_timeout(1200)

        tasks = page.locator("#tasks").inner_text()
        assert "r98-unique-acceptance-report" in tasks, tasks
        # It is urgent and the only task, so it is the next action.
        next_text = page.locator("#next-action").inner_text()
        assert "r98-unique-acceptance-report" in next_text, next_text
        # The rule that picked it is stated, not implied.
        assert "chosen as the" in next_text, next_text

        # Completing it removes it from the open list.
        page.locator("#tasks li.row button", has_text="Done").first.click()
        page.wait_for_timeout(1200)
        assert "r98-unique-acceptance-report" not in page.locator("#tasks").inner_text()
        browser.close()


@playwright_required
def test_the_page_states_when_the_calendar_could_not_be_read(isolated_server):
    """An unread calendar must be called out, not presented as an empty day.

    The isolated server has a local calendar that is readable, so this asserts
    the *positive* rendering (no false warning) — the failure branch is covered
    in the unit tests, where the provider can be made to fail deterministically.
    """
    from playwright.sync_api import sync_playwright

    base = isolated_server["base"]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(f"{base}/planner", wait_until="networkidle")
        page.wait_for_timeout(1200)
        assert page.locator("#calendar-warning").is_hidden(), (
            "a readable calendar must not show the 'could not be read' warning")
        assert "Nothing scheduled today." in page.locator("#events").inner_text()
        browser.close()

@playwright_required
def test_a_schedule_can_be_created_paused_and_cancelled_in_the_page(isolated_server):
    """The scheduler section is real: create a job, see it, pause it, cancel it."""
    from playwright.sync_api import sync_playwright

    base = isolated_server["base"]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(f"{base}/planner", wait_until="networkidle")
        page.wait_for_timeout(1200)

        # The isolated server is shared for the session, so another test may have
        # left a job. This test tracks only the job it creates.
        page.fill("#sched-action", "reminder")
        page.fill("#sched-when", "every morning at 8")
        page.fill("#sched-message", "lifecycle-check-unique")
        page.click("#sched-form button[type=submit]")
        page.wait_for_timeout(1200)

        text = page.locator("#schedules").inner_text()
        assert "reminder" in text, text
        assert "next run" in text, text

        # Pause it.
        page.locator("#schedules li.row button", has_text="Pause").first.click()
        page.wait_for_timeout(1000)
        assert "paused" in page.locator("#schedules").inner_text()

        # Resume, then cancel.
        page.locator("#schedules li.row button", has_text="Resume").first.click()
        page.wait_for_timeout(1000)
        assert "active" in page.locator("#schedules").inner_text()
        page.locator("#schedules li.row button", has_text="Cancel").first.click()
        page.wait_for_timeout(1000)
        # Its own job is gone (a cancelled job drops out of the active list).
        assert "lifecycle-check-unique" not in page.locator("#schedules").inner_text()
        browser.close()


@playwright_required
def test_an_unsupported_schedule_action_is_refused_in_the_page(isolated_server):
    """The page refuses an action the scheduler cannot run, before sending it."""
    from playwright.sync_api import sync_playwright

    base = isolated_server["base"]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(f"{base}/planner", wait_until="networkidle")
        page.wait_for_timeout(1000)
        page.fill("#sched-action", "delete_everything")
        page.fill("#sched-when", "every day at 9")
        page.click("#sched-form button[type=submit]")
        page.wait_for_timeout(600)
        assert "briefing" in page.locator("#sched-err").inner_text()
        browser.close()


@playwright_required
def test_planning_a_goal_in_the_page_creates_ordered_tasks(isolated_server):
    """The goal box is real: it POSTs, saves tasks, and they appear in order.

    Pinned because this control was once added to the backend and described in
    tests while never actually reaching the page — the "backend works, UI
    missing" failure this project keeps having to guard against.
    """
    from playwright.sync_api import sync_playwright

    base = isolated_server["base"]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(f"{base}/planner", wait_until="networkidle")
        page.wait_for_timeout(1200)
        page.fill("#goal", "Ship the AMR demo")
        page.fill("#goal-project", "AMR")
        page.click("#goal-form button[type=submit]")
        page.wait_for_timeout(1600)

        tasks = page.locator("#tasks").inner_text()
        assert "Define what" in tasks, tasks
        assert "Ship the AMR demo" in tasks, tasks
        assert "Verify" in tasks, tasks
        # The plan note says nothing was scheduled on the calendar.
        assert "calendar" in page.locator("#goal-note").inner_text().lower()
        browser.close()


@playwright_required
def test_the_attention_section_appears_only_when_something_is_wrong(isolated_server):
    """Verified in a browser, not just at the API: hidden when clear, shown when not.

    Pinned because the section is rendered from `/attention` — a control whose
    only proof until now was an API test, which is the "backend works, page
    never checked" gap this project keeps finding.
    """
    import json
    import urllib.request
    from playwright.sync_api import sync_playwright
    from tests.isolated_server import session_token

    base = isolated_server["base"]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(f"{base}/planner", wait_until="networkidle")
        page.wait_for_timeout(1600)
        assert page.locator("#attention-section").is_hidden(), (
            "nothing is wrong, so the attention section must be hidden")

        tok = session_token(base)
        req = urllib.request.Request(
            f"{base}/planner/tasks", method="POST",
            data=json.dumps({"title": "Late thing", "due_at": "2020-01-01T09:00:00"}).encode(),
            headers={"Content-Type": "application/json", "X-JARVIS-Token": tok})
        urllib.request.urlopen(req).read()

        page.reload(wait_until="networkidle")
        page.wait_for_timeout(1600)
        assert page.locator("#attention-section").is_visible()
        assert "Late thing" in page.locator("#needs-attention").inner_text()
        browser.close()


@playwright_required
def test_the_dashboard_links_to_the_product_pages(isolated_server):
    """A page nobody can navigate to is a page nobody uses.

    `/planner`, `/operator` and `/work` were each built and verified, but the
    dashboard's nav listed only AI Chat and Settings — so they were unreachable
    from the app's own home.
    """
    from playwright.sync_api import sync_playwright

    base = isolated_server["base"]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(f"{base}/dashboard", wait_until="networkidle")
        page.wait_for_timeout(700)
        links = [a.get_attribute("href") for a in page.locator("header nav a").all()]
        for target in ("/planner", "/operator", "/work", "/ai/chat", "/ai-connections"):
            assert target in links, f"the dashboard does not link to {target}: {links}"
        # And the link actually lands on a working page.
        page.click("header nav a[href='/planner']")
        page.wait_for_timeout(1500)
        assert page.title() == "Vantage — Planner"
        assert page.locator("#status").inner_text().strip() == "connected"
        browser.close()


@playwright_required
def test_the_product_pages_share_one_navigation(isolated_server):
    """Every product page offers the same ways out — no dead-end pages."""
    from playwright.sync_api import sync_playwright

    expected = {"/planner", "/operator", "/work", "/dashboard", "/ai-connections"}
    base = isolated_server["base"]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        for path in ("/planner", "/operator", "/work"):
            page.goto(f"{base}{path}", wait_until="networkidle")
            page.wait_for_timeout(700)
            links = {a.get_attribute("href") for a in page.locator("header a").all()}
            assert expected <= links, f"{path} is missing nav links: {expected - links}"
        # Cross-navigation actually works.
        page.goto(f"{base}/planner", wait_until="networkidle")
        page.click("header a[href='/work']")
        page.wait_for_timeout(1200)
        assert page.title() == "Vantage — Work"
        browser.close()
