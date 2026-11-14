"""Feature 3 — the workspace / workflow supervisor page, in a real browser."""

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
def test_the_work_page_connects_and_opens_a_workspace(isolated_server):
    """Open a real folder as a workspace, see it listed, and inspect it."""
    import tempfile
    from pathlib import Path
    from playwright.sync_api import sync_playwright

    base = isolated_server["base"]
    workdir = Path(tempfile.mkdtemp(prefix="ws-"))
    (workdir / "main.py").write_text("print('hi')\n")
    (workdir / "test_main.py").write_text("def test_a(): assert True\n")
    (workdir / "pyproject.toml").write_text("[project]\nname='x'\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(f"{base}/work", wait_until="networkidle")
        page.wait_for_timeout(1200)
        assert page.title() == "Vantage — Work"
        assert page.locator("#status").inner_text().strip() == "connected"

        page.fill("#ws-path", str(workdir))
        page.click("#ws-form button[type=submit]")
        page.wait_for_timeout(1500)
        text = page.locator("#workspaces").inner_text()
        assert "No workspace open." not in text, text

        page.locator("#workspaces li.row button", has_text="Inspect").first.click()
        page.wait_for_timeout(1500)
        detail = page.locator("#ws-detail").inner_text()
        assert "source files" in detail.lower(), detail
        assert "test files" in detail.lower(), detail
        browser.close()


@playwright_required
def test_the_work_page_refuses_a_missing_folder(isolated_server):
    from playwright.sync_api import sync_playwright

    base = isolated_server["base"]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(f"{base}/work", wait_until="networkidle")
        page.wait_for_timeout(1000)
        page.fill("#ws-path", "/no/such/folder/at/all")
        page.click("#ws-form button[type=submit]")
        page.wait_for_timeout(1200)
        assert page.locator("#ws-err").inner_text().strip(), "an error must be shown"
        browser.close()


@playwright_required
def test_inspect_shows_real_workspace_facts(isolated_server):
    """The inspect route returns the inspection FLATTENED; the page must read it.

    Measured: reading `data.inspection` rendered every field as "?" because the
    route spreads the inspection at the top level (`**inspect(workspace)`).
    """
    import tempfile
    from pathlib import Path
    from playwright.sync_api import sync_playwright

    base = isolated_server["base"]
    workdir = Path(tempfile.mkdtemp(prefix="ws2-"))
    (workdir / "app.py").write_text("x = 1\n")
    (workdir / "test_app.py").write_text("def test_x(): assert True\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(f"{base}/work", wait_until="networkidle")
        page.wait_for_timeout(1000)
        page.fill("#ws-path", str(workdir))
        page.click("#ws-form button[type=submit]")
        page.wait_for_timeout(1300)
        page.locator("#workspaces li.row button", has_text="Inspect").first.click()
        page.wait_for_timeout(1300)
        detail = page.locator("#ws-detail").inner_text()
        assert "?" not in detail.split("source files")[1][:8], detail
        assert "python" in detail.lower(), detail


@playwright_required
def test_a_workflow_job_shows_its_steps_and_outcomes(isolated_server):
    """A multi-action request creates a real job whose steps show completed work.

    A job exists only for a plan with more than one action; its per-step status
    is what distinguishes "completed" from "remaining".
    """
    import json
    import urllib.request
    from playwright.sync_api import sync_playwright
    from tests.isolated_server import session_token

    base = isolated_server["base"]
    tok = session_token(base)
    req = urllib.request.Request(
        f"{base}/command", method="POST",
        data=json.dumps({"text": "check battery, then check storage",
                         "source": "t", "session_id": "wf-steps"}).encode(),
        headers={"Content-Type": "application/json", "X-JARVIS-Token": tok})
    urllib.request.urlopen(req).read()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(f"{base}/work", wait_until="networkidle")
        page.wait_for_timeout(1300)
        page.locator("#jobs li.row button", has_text="Steps").first.click()
        page.wait_for_timeout(1500)
        text = page.locator("#jobs").inner_text()
        assert "get_battery_status" in text, text
        assert "succeeded" in text, text
        browser.close()
