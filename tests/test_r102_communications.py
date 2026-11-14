"""Feature 5 — contacts and call preparation.

Preparation is a *read*: it finds the contact, open tasks that name them, recent
mail from them, and returns a briefing. It never claims a call happened.
"""

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


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from jarvis.api.app import app
    from tests.conftest import AUTH_HEADERS
    c = TestClient(app, base_url="http://127.0.0.1:8765")
    c.headers.update(AUTH_HEADERS)
    return c


@playwright_required
def test_add_a_contact_and_prepare_a_call_in_the_page(isolated_server):
    from playwright.sync_api import sync_playwright

    base = isolated_server["base"]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(f"{base}/operator", wait_until="networkidle")
        page.wait_for_timeout(1200)
        page.fill("#contact-name", "Priya Raman")
        page.fill("#contact-address", "priya@example.com")
        page.click("#contact-form button[type=submit]")
        page.wait_for_timeout(1200)
        assert "Priya Raman" in page.locator("#contacts").inner_text()

        # The contact is now a row: the name is the row, the action sits beside
        # it, so the test finds the row by its text rather than a button label.
        row = page.locator("#contacts > div", has_text="Priya Raman")
        row.locator("button", has_text="Prepare call").click()
        page.wait_for_timeout(1500)
        brief = page.locator("#call-brief").inner_text()
        assert "Call brief: Priya Raman" in brief, brief
        assert "priya@example.com" in brief, brief

        # Calling is implemented now (FaceTime), so the old assertion that the
        # brief "does not place the call" is no longer true. What must remain
        # true is the honest limit: Vantage cannot see or hear the call, and the
        # call panel says so rather than claiming a conversation happened.
        assert page.locator("#call-actions").is_visible(), "the call panel is offered"
        # Availability is stated before anyone dials, so a machine without a
        # calling app cannot offer a button that cannot work.
        assert page.locator("#call-avail").inner_text().strip(), (
            "the panel must report whether a calling app is available")
        # Nothing has been dialled by merely preparing a brief.
        assert page.locator("#call-note").inner_text().strip() == ""
        browser.close()


def test_prepare_call_for_an_unknown_contact_is_404(client):
    assert client.post("/contacts/Nobody Here/prepare-call").status_code == 404


def test_prepare_call_reports_open_tasks_for_the_contact(client):
    client.post("/contacts", json={"name": "Sam Okoro", "address": "sam@example.com"})
    client.post("/planner/tasks", json={"title": "Follow up with Sam Okoro about the demo"})
    brief = client.post("/contacts/Sam Okoro/prepare-call").json()
    assert brief["contact"]["address"] == "sam@example.com"
    assert any("Sam Okoro" in t["title"] for t in brief["open_tasks"]), brief
    # `supported` used to be hard-coded False, from before the FaceTime path
    # existed. What must still hold is that the brief *says what it can and cannot
    # do*: either it can hand the contact to a calling app (and asks first), or it
    # says plainly why it cannot. A bare flag is not the promise; the sentence is.
    place = brief["place_call"]
    assert place["reason"], place
    if place["supported"]:
        assert "asks first" in place["reason"], place
    else:
        assert "not installed" in place["reason"] or "cannot" in place["reason"], place
