"""The AI chat dashboard, driven in a real browser.

The dashboard's approval and attachment clients were written and contract-
tested, but nobody had *run* them: the page's own script sent the literal
string ``__JARVIS_BOOTSTRAP__`` as its API token, so every request it made
came back 401 and the page could not do anything at all. A contract test
cannot catch that -- the contract was never exercised, only described.

These tests drive the real page in a real browser against a real backend, and
they found three defects that only appear when the page is actually used:

1. **The page had no working authentication.** ``authHeaders()`` read
   ``window.sessionToken`` -- a value nothing ever set -- while the page's
   bootstrap was read into ``state.bootstrapToken`` and never used. Every
   request went out with an empty token and came back 401.
2. **Every POST was 422.** The page sent ``JSON.stringify(...)`` without a
   ``Content-Type``, so the body arrived as ``text/plain`` and the API read a
   string where it wanted a dictionary.
3. **The page was served without a bootstrap.** The route was ``Protected``
   *and* left the placeholder in the HTML, so even a correctly-written page
   would have had nothing to spend.

The tests here are the ones that would have caught all three. They are slow
(a browser, a real backend) and they are skipped where Playwright is not
installed -- but they are the difference between a page that is described and
a page that works.
"""

from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

REPO = Path(__file__).resolve().parent.parent

#: The browser tests drive a **real server on a scratch home**, never the
#: developer's live backend.
#:
#: They used to point at ``http://127.0.0.1:8765`` -- the developer's own
#: running backend, against the real ``~/.jarvis-forge`` data directory. The
#: approval test raises "schedule a meeting called Browser Check" and clicks
#: **Approve**, so it was creating real calendar events in the user's data on
#: every run. Measured on this machine after a few runs: ten "called Browser
#: Check" events on 2026-12-05, all with the same title.
#:
#: :mod:`tests.isolated_server` starts a real uvicorn on a real port with a
#: real empty data directory, so the page, the session bootstrap and the
#: approval round trip are all exercised for real -- against a calendar that
#: starts out empty and is thrown away with the test home.
BASE = ""  # set per-test from the isolated server fixture


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


# ---------------------------------------------------------------------------
# The page's own source, checked without a browser
# ---------------------------------------------------------------------------
def _page_source() -> str:
    return (REPO / "dashboard" / "ai_chat.html").read_text(encoding="utf-8")


def test_the_page_reads_a_token_it_actually_sets():
    """The header must name a value the page sets.

    It read ``window.sessionToken`` -- a global nothing ever assigned -- while
    the bootstrap it *had* was read into a state field and never used. Every
    request therefore went out with an empty token.
    """
    source = _page_source()
    # Checked in the CODE: the string also appears in the comment that
    # explains why the global was wrong.
    code = "\n".join(
        line for line in source.splitlines()
        if not line.strip().startswith("//")
    )
    assert "window.sessionToken" not in code, (
        "the page still reads a global it never sets; the session must be held "
        "in the page's own state")
    assert "state.sessionToken" in code, (
        "the page does not use the session it buys")


def test_the_page_buys_a_session_with_its_bootstrap():
    """The bootstrap is single-use; it is *spent* to buy a session."""
    source = _page_source()
    assert "async function authenticate()" in source, (
        "the page never exchanges its bootstrap for a session")
    assert "/auth/session" in source, "the bootstrap is never spent"


def test_every_json_post_declares_its_content_type():
    """A JSON body without ``Content-Type`` arrives as text.

    The API then reads a string where it wanted a dictionary and answers 422
    ("Input should be a valid dictionary"). Every POST in the page had this
    bug, so the page could not create a chat, save a message, or process an
    attachment.
    """
    source = _page_source()
    headers = re.search(r"function authHeaders\(\).*?\n      \}", source, re.S)
    assert headers, "the page has no authHeaders()"
    assert "Content-Type" in headers.group(0), (
        "the page's JSON calls do not declare a content type")


def test_the_multipart_upload_keeps_its_own_content_type():
    """A multipart body carries its own boundary.

    Overriding its Content-Type with ``application/json`` makes the server
    read a multipart body as JSON and refuse it.
    """
    source = _page_source()
    upload = re.search(
        r'fetch\("/ai/attachments/upload".*?\}\);', source, re.S)
    assert upload, "the upload call is gone"
    body = "\n".join(
        line for line in upload.group(0).splitlines()
        if not line.strip().startswith("//")
    )
    assert "Content-Type" not in body, (
        "the multipart upload sets a JSON content type, which breaks the "
        "boundary the server needs")


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------
@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from jarvis.api.app import app
    return TestClient(app, base_url="http://127.0.0.1:8765")


def test_the_ai_chat_page_carries_a_usable_bootstrap(client):
    """The page is served with a bootstrap code, not the literal placeholder.

    A fresh tab navigation has no session token yet -- that is exactly what
    the bootstrap is for, and the page is linked from the dashboard, so it is
    a page a person reaches.
    """
    page = client.get("/ai/chat")
    assert page.status_code == 200, page.text[:200]
    match = re.search(r'name="jarvis-bootstrap" content="([^"]*)"', page.text)
    assert match, "the page carries no bootstrap field"
    code = match.group(1)
    assert code and code != "__JARVIS_BOOTSTRAP__", (
        "the page was served the literal placeholder instead of a bootstrap")
    # And it is a bootstrap, not a session: the API only accepts a session
    # token or the master token, never a bootstrap code.
    refused = client.get("/confirmations", headers={"X-JARVIS-Token": code})
    assert refused.status_code == 401, (
        "a bootstrap code was accepted as an API token -- the page's own "
        "script would then work by accident")


# ---------------------------------------------------------------------------
# The page, in a browser
# ---------------------------------------------------------------------------
@playwright_required
def test_the_page_loads_establishes_a_session_and_shows_no_errors(isolated_server):
    """The page works: a session, a chat, the providers, and no console noise."""
    from playwright.sync_api import sync_playwright

    base = isolated_server["base"]
    console_errors: List[str] = []
    page_errors: List[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.on("console",
                lambda m: console_errors.append(m.text[:120]) if m.type == "error" else None)
        page.on("pageerror", lambda e: page_errors.append(str(e)[:120]))
        page.goto(f"{base}/ai/chat", wait_until="networkidle")
        page.wait_for_timeout(2000)

        assert page.title(), "the page has no title"
        assert page.evaluate("() => !!state.sessionToken"), (
            "the page did not establish a session")
        assert page.evaluate("() => !!(state.currentChat && state.currentChat.id)"), (
            "the page did not create a conversation")
        assert page.evaluate("() => state.providers.length") > 0, (
            "the page loaded no providers")
        assert page.locator("#attach-button").count() == 1, (
            "the page has no attach button")
        browser.close()

    assert not page_errors, f"the page raised: {page_errors}"
    assert not console_errors, f"the page logged errors: {console_errors}"


@playwright_required
def test_a_reload_resumes_the_conversation_instead_of_stranding_it(isolated_server):
    """Continuity across a reload.

    The page used to create a *new* conversation on every load, so a chat was
    unreachable from the page that wrote it the moment the tab was closed or
    another device opened the page. This drives the real page: one turn is
    sent, the tab is reloaded, and the same conversation -- with its turn --
    must come back.
    """
    from playwright.sync_api import sync_playwright

    base = isolated_server["base"]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(f"{base}/ai/chat", wait_until="networkidle")
        page.wait_for_timeout(1500)

        first_id = page.evaluate("() => state.currentChat && state.currentChat.id")
        assert first_id, "the first load created no conversation"

        # A turn, saved through the API the page itself calls, so the resume
        # has something to prove it loaded.
        page.evaluate(
            """async () => {
                const chat = state.currentChat;
                const r = await fetch(`/ai/chats/${chat.id}/messages`, {
                  method: "POST", headers: authHeaders(),
                  body: JSON.stringify({ role: "user", content: "resume-marker-42" }),
                });
                if (!r.ok) throw new Error("save failed " + r.status);
            }"""
        )

        # Reload: the page must adopt the SAME conversation, not mint a new one.
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(2000)
        resumed_id = page.evaluate("() => state.currentChat && state.currentChat.id")
        assert resumed_id == first_id, (
            f"the reload opened {resumed_id!r} instead of resuming {first_id!r}")

        # And the earlier turn is on screen -- rendered as text, not markup.
        body = page.locator("#messages-area").inner_text()
        assert "resume-marker-42" in body, (
            "the resumed conversation did not show its earlier turn")

        # History lists it, and the panel opens with it as the current entry.
        page.click("#history-btn")
        page.wait_for_timeout(500)
        items = page.locator(".history-item")
        assert items.count() >= 1, "the history panel listed no conversations"
        assert page.locator(".history-item.current").count() == 1, (
            "the history panel did not mark the open conversation")
        browser.close()


@playwright_required
def test_a_message_is_rendered_as_text_not_markup(isolated_server):
    """Model/stored text is shown, never interpreted as HTML.

    A reply containing markup must appear as the words it is. The page used to
    build every message with innerHTML, so a resumed turn -- stored text --
    could inject structure into the page.
    """
    from playwright.sync_api import sync_playwright

    base = isolated_server["base"]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(f"{base}/ai/chat", wait_until="networkidle")
        page.wait_for_timeout(1500)
        page.evaluate(
            """async () => {
                const chat = state.currentChat;
                await fetch(`/ai/chats/${chat.id}/messages`, {
                  method: "POST", headers: authHeaders(),
                  body: JSON.stringify({ role: "user",
                    content: "<img src=x onerror=window.__pwned=1>" }),
                });
            }"""
        )
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(2000)
        assert page.evaluate("() => window.__pwned") is None, (
            "stored markup executed in the page")
        assert page.locator("#messages-area img").count() == 0, (
            "stored markup was rendered as an element")
        browser.close()


@playwright_required
def test_a_pending_approval_is_shown_and_can_be_answered(isolated_server):
    """The approval card appears, names the action, and answers.

    Driven end to end: a consequential request is raised, the page is loaded,
    the card is visible with the server's own description of what will
    happen, and clicking Approve runs it and reports the result.

    The server is the isolated one, so the event this creates lands in a
    scratch calendar that is thrown away -- not in the user's.
    """
    from playwright.sync_api import sync_playwright

    from tests.isolated_server import post_command, session_token

    base = isolated_server["base"]

    # A session, the way the dashboard gets one.
    token = session_token(base)

    # A request that needs approving.
    raised = post_command(
        base, token, "schedule a meeting called Browser Check on 2026-12-05 09:00")
    assert raised.get("confirmation_id"), raised
    confirmation_id = raised["confirmation_id"]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(f"{base}/ai/chat", wait_until="networkidle")
        page.wait_for_timeout(2000)

        # The page may show other pending approvals -- anything raised
        # earlier, by another test or by the person using the machine. The
        # check is that *this* request's card is there, not that the page
        # holds exactly one card: asserting an exact count made the test
        # depend on the machine's pending state, which is not its business.
        cards = page.locator(".approval-card")
        count = cards.count()
        assert count >= 1, "the pending approval was not shown"

        # Find the card for this request. The card does not render the
        # confirmation id, so it is matched by the server's own preview --
        # which is the text the person is being asked to approve, and is
        # unique to this request.
        target = None
        for index in range(count):
            card = cards.nth(index)
            if "Browser Check" in card.inner_text():
                target = card
                break
        assert target is not None, (
            f"the approval card for {confirmation_id} is not on the page; "
            f"the page has {count} card(s)")

        title = target.locator(".approval-title").inner_text()
        assert "create event" in title.lower(), title
        detail = target.locator(".approval-detail").inner_text()
        assert "create_event" in detail, (
            "the card does not show the server's own description of the action")

        target.locator("button.approve").click()
        page.wait_for_timeout(3000)

        # The card that was answered is gone.
        assert page.locator(
            ".approval-card:has-text('Browser Check')").count() == 0, (
            "the approval card survived being answered")
        texts = page.locator("#messages-area .message").all_inner_texts()
        assert texts, "nothing was reported after the approval"
        assert any("create_event" in t for t in texts), texts
        browser.close()


@playwright_required
def test_an_attachment_is_uploaded_processed_and_its_text_shown(tmp_path, isolated_server):
    """The attachment flow, end to end, in a browser.

    A PNG with words in it is attached, the message is sent, and the card
    that appears shows the **text the image contained** — the thing the
    attachment was uploaded for.
    """
    from playwright.sync_api import sync_playwright

    pytest.importorskip("PIL")
    from PIL import Image, ImageDraw

    base = isolated_server["base"]
    shot = tmp_path / "screenshot.png"
    image = Image.new("RGB", (600, 200), "white")
    draw = ImageDraw.Draw(image)
    draw.text((30, 40), "Project AMR status report", fill="black")
    image.save(shot)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(f"{base}/ai/chat", wait_until="networkidle")
        page.wait_for_timeout(1500)

        page.locator("#attachment-input").set_input_files(str(shot))
        page.wait_for_timeout(400)
        assert "screenshot.png" in page.locator("#attachment-status").inner_text(), (
            "the chosen file was not shown as staged")

        page.locator("#message-input").fill("here is a screenshot")
        page.locator("#send-button").click()

        # The first OCR call on a machine compiles a helper, so this is given
        # room; the timeout is the pipeline's, not the test's.
        for _ in range(90):
            page.wait_for_timeout(1000)
            if page.locator(".attachment-card").count():
                break
        assert page.locator(".attachment-card").count() == 1, (
            "the attachment's result was never shown")

        assert "screenshot.png" in page.locator(".attachment-name").first.inner_text()
        body = page.locator(".attachment-body").first.inner_text()
        assert "AMR" in body, (
            f"the card does not show the text the image contained: {body!r}")
        browser.close()
