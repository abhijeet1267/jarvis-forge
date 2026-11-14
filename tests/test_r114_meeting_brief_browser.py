"""Workflow C in a real browser — preparing for a meeting on the planner page.

`test_r113_meeting_prep.py` covers the API. This is the other half: a person can
open the planner, name a meeting, and read the briefing — including the plain
statement that Vantage prepares it and does not attend.

It runs against the project's isolated server (a real uvicorn on a scratch
home), never the developer's live backend. The event is seeded into *that*
server's database, not the test process's, so the page and the seed cannot
diverge.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

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


def _seed_event(server_home: Path, uid: str, title: str, *, location: str = "",
                description: str = "", days: int = 2) -> None:
    """Insert one event into the isolated server's own database."""
    start = (datetime.now().astimezone() + timedelta(days=days)).replace(
        hour=15, minute=0, second=0, microsecond=0)
    end = start + timedelta(minutes=45)
    with sqlite3.connect(server_home / "jarvis.db") as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO calendar_events
                (uid, title, start_at, end_at, location, description, calendar, all_day)
            VALUES (?, ?, ?, ?, ?, ?, 'default', 0)
            """,
            (uid, title, start.isoformat(), end.isoformat(), location, description),
        )
        conn.commit()


@playwright_required
def test_prepare_a_meeting_from_the_planner_page(isolated_server):
    from playwright.sync_api import sync_playwright

    base = isolated_server["base"]
    _seed_event(isolated_server["home"], "EVT-BROWSER-MEETING", "Browser brief review",
                location="Room 9", description="Walk through the new brief")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(f"{base}/planner", wait_until="networkidle")
        page.wait_for_timeout(1200)

        page.fill("#meeting-name", "Browser brief review")
        page.click("#meeting-form button[type=submit]")
        page.wait_for_timeout(1500)

        brief = page.locator("#meeting-brief").inner_text()
        assert "Browser brief review" in brief, brief
        assert "Room 9" in brief, brief
        assert "does not attend" in brief, brief
        assert "Steps:" in brief, brief
        browser.close()


@playwright_required
def test_an_unknown_meeting_is_reported_in_the_page(isolated_server):
    from playwright.sync_api import sync_playwright

    base = isolated_server["base"]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(f"{base}/planner", wait_until="networkidle")
        page.wait_for_timeout(1200)

        page.fill("#meeting-name", "No Such Gathering Anywhere")
        page.click("#meeting-form button[type=submit]")
        page.wait_for_timeout(1500)

        err = page.locator("#meeting-err").inner_text()
        assert "no meeting matching" in err.lower(), err
        browser.close()