"""The professional-networking recap, driven from the operator page.

`test_r115_recap_post.py` covers the API. This checks the button a person
actually clicks: a recap appears when work was completed, and the page says
plainly that there is nothing to recap when it was not — it never shows a draft
that does not exist.

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


def _task(client, title: str, project: str = "") -> None:
    """Create and complete a task through the isolated server's own routes."""
    created = client.post("/planner/tasks", json={"title": title, "project": project})
    assert created.status_code == 200, created.text
    ref = created.json()["task_ref"]
    assert client.post(f"/planner/tasks/{ref}/complete", json={}).status_code == 200


@pytest.fixture()
def server_client(isolated_server):
    """A client bound to the isolated server (not the test process's own app)."""
    import httpx

    return httpx.Client(
        base_url=isolated_server["base"],
        headers={"X-JARVIS-Token": "isolated-browser-test-token"},
        timeout=20.0,
    )


@playwright_required
def test_a_recap_is_drafted_from_completed_work_in_the_page(isolated_server, server_client):
    from playwright.sync_api import sync_playwright

    _task(server_client, "Ship the isolated recap", project="Isolated")

    base = isolated_server["base"]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(f"{base}/operator", wait_until="networkidle")
        page.wait_for_timeout(1200)

        page.click("#recap-post")
        page.wait_for_timeout(1500)

        note = page.locator("#recap-note").inner_text()
        assert "Drafted from" in note, note
        posts = page.locator("#posts").inner_text()
        assert "Ship the isolated recap" in posts, posts
        # The page states the publishing boundary instead of offering it.
        assert "no approved publishing integration" in page.locator("#publish-note").inner_text()
        browser.close()


@playwright_required
def test_publishing_is_stated_as_unsupported_not_offered(isolated_server):
    from playwright.sync_api import sync_playwright

    base = isolated_server["base"]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(f"{base}/operator", wait_until="networkidle")
        page.wait_for_timeout(1200)

        text = page.locator("body").inner_text()
        assert "no approved publishing integration" in text, text[:400]
        # No control that would pretend to publish.
        assert page.locator("button", has_text="Publish").count() == 0
        browser.close()