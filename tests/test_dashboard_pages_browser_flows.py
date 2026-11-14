"""The dashboard's other pages, driven for real.

``dashboard/ai_chat.html`` was driven in a browser (see
``test_dashboard_browser_flows.py``) and three of its defects were found that
way: no working authentication, JSON POSTs with no content type, and a page
served without a bootstrap. That file covers ONE page.

The other five pages had never been run. Four of them turned out to be broken
in ways no source-reading test would have caught, and the same three defect
classes showed up again and again:

1. **A page that cannot authenticate.** ``mission_control.html`` posted
   ``{code}`` where the route's body is ``BootstrapBody{bootstrap}``, and read
   ``data.token`` where the route answers ``session_token``. Both mistakes
   meant the page held an empty token and every request it made came back
   401 -- the page was completely inert.

2. **A page that reads fields the server never sends.** ``mission_control.html``
   read ``p.id`` and ``p.name``; the provider card is
   ``{provider_id, display_name, ...}``. Every provider rendered as
   "undefined". It also read ``m.id``/``m.name`` for a model list that is a
   list of strings, and read ``health[p.id]`` for a payload that arrives as
   ``{"providers": {...}}`` -- one level too shallow.

3. **A page that calls a route that does not exist.** ``mission_control.html``
   called ``PUT /ai/providers/{id}/key`` and ``DELETE /ai/providers/{id}/key``.
   The real routes are ``POST /ai/providers/{id}/store`` and
   ``DELETE /ai/providers/{id}/credentials``. Both key operations failed.

4. **A page whose poll never ran.** ``phone.html`` assigned ``authLost``,
   ``lastConf`` and ``lastQuick`` and read them back, but never declared any
   of the three. In sloppy mode the assignment quietly creates a global, but
   the *read* comes first and throws ``ReferenceError``. ``refresh()`` -- the
   four-second poll -- died on its first statement, so the phone made **zero**
   ``/devices`` and ``/confirmations`` requests: no device list, and no
   approval button for a person to tap.

5. **A page that was never served at all.** ``ai_settings.html`` is an
   abandoned pre-Phase-72 prototype: no route serves it, and it calls
   ``/api/ai/providers`` and ``/api/ai/settings`` -- paths the backend has
   never had. It is a broken duplicate of ``ai_connections.html``.

The tests here are the ones that would have caught all of the above. They are
slow (a browser, a real backend) and they are skipped where Playwright is not
installed -- but the difference between a page that is *described* and a page
that *works* is exactly this.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List

import pytest

REPO = Path(__file__).resolve().parent.parent
DASHBOARD = REPO / "dashboard"
#: Set per-test from the ``isolated_server`` fixture -- a real server on a
#: scratch home, never a developer's live backend. See
#: :mod:`tests.isolated_server` for why that matters.
BASE = ""


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


def _page(name: str) -> str:
    return (DASHBOARD / name).read_text(encoding="utf-8")


def _script_of(name: str) -> str:
    """The page's own inline script, without the HTML around it."""
    source = _page(name)
    match = re.search(r"<script>(.*?)</script>", source, re.S)
    assert match, f"{name} has no inline script"
    return match.group(1)


# ---------------------------------------------------------------------------
# The pages' own source, checked without a browser
# ---------------------------------------------------------------------------
def test_mission_control_spends_its_bootstrap_the_way_the_route_reads_it():
    """The body must be ``{bootstrap}``.

    The route's body is ``BootstrapBody{bootstrap}``. The page posted
    ``{code}``, so the code never arrived and the request was refused -- which
    is what the page's own console showed: "Bootstrap failed: 403".
    """
    source = _page("mission_control.html")
    body = re.search(r"JSON\.stringify\(\{\s*bootstrap:\s*code\s*\}\)", source)
    assert body, (
        "the page does not send its bootstrap as {bootstrap}; the route's body "
        "is BootstrapBody{bootstrap}, so {code} is refused"
    )


def test_mission_control_reads_the_session_field_the_route_sends():
    """The route answers ``session_token``.

    The page read ``data.token`` -- a field the route has never sent -- so the
    session was stored as ``undefined`` and every request went out
    unauthenticated.
    """
    source = _page("mission_control.html")
    assert "data.session_token" in source, (
        "the page does not read the session the route actually returns "
        "(session_token)")
    assert not re.search(r"=\s*data\.token\s*;", source), (
        "the page still reads data.token, which the route never sends")


def test_mission_control_reads_the_provider_fields_the_server_sends():
    """A provider card is ``{provider_id, display_name, ...}``.

    The page read ``p.id`` and ``p.name``, so every provider rendered as
    "undefined" and every option carried the value "undefined".
    """
    source = _page("mission_control.html")
    assert "p.provider_id" in source, (
        "the page does not read provider_id, which is the field the provider "
        "card carries")
    assert "p.display_name" in source, (
        "the page does not read display_name, which is the label the provider "
        "card carries")
    assert not re.search(r"\$\{p\.id\}|\$\{p\.name\}", source), (
        "the page still renders p.id / p.name, which are undefined on the wire")


def test_mission_control_reads_the_health_payload_where_it_is():
    """``/ai/connection-health`` answers ``{"providers": {...}}``.

    The page read the top level as if it were the provider map, so every
    provider's state was looked up one level too high and never found.
    """
    source = _page("mission_control.html")
    assert "data.providers" in source or "health.providers" in source, (
        "the page does not read the health payload's 'providers' wrapper")


def test_mission_control_calls_routes_that_exist():
    """The key routes are ``POST .../store`` and ``DELETE .../credentials``.

    The page called ``PUT /ai/providers/{id}/key`` and
    ``DELETE /ai/providers/{id}/key`` -- a route the backend has never had, so
    every save and every delete failed.
    """
    source = _page("mission_control.html")
    assert "/ai/providers/${providerId}/store" in source, (
        "the page does not store keys through the route that exists")
    assert "/ai/providers/${providerId}/credentials" in source, (
        "the page does not delete keys through the route that exists")
    assert "/ai/providers/${providerId}/key" not in source, (
        "the page still calls a key route that does not exist")


def test_the_phone_declares_the_variables_it_reads():
    """``authLost``, ``lastConf``, ``lastQuick`` must be declared.

    Each was assigned and read back but never declared. Sloppy mode lets the
    assignment create a global; the *read* comes first and throws
    ``ReferenceError``. The read is in ``refresh()``, the four-second poll --
    so the poll died on its first statement.
    """
    script = _script_of("phone.html")
    for name in ("authLost", "lastConf", "lastQuick"):
        declared = re.search(
            rf"\b(?:let|const|var)\s+{name}\b", script)
        assert declared, (
            f"phone.html reads `{name}` before it is ever declared; the read "
            "throws a ReferenceError and kills the poll"
        )


def test_the_phone_declares_its_poll_timer():
    """``pollTimer`` is assigned by ``setInterval`` and cleared on sign-out."""
    script = _script_of("phone.html")
    assert re.search(r"\b(?:let|const|var)\s+pollTimer\b", script), (
        "phone.html assigns pollTimer without declaring it")


def test_the_orphan_page_is_not_served_and_not_linked():
    """``ai_settings.html`` is a pre-Phase-72 prototype the product never ships.

    It calls ``/api/ai/providers`` and ``/api/ai/settings`` -- paths the
    backend has never had -- and it has no bootstrap at all, so every route
    it calls would answer 401. ``ai_connections.html`` is the working page
    that replaced it.

    The file is left on disk rather than deleted: it is untracked work that
    predates this audit, and removing somebody's file is not this test's
    call. What is asserted here is the property that matters -- it is not
    reachable from any surface, so it cannot be mistaken for a live page.
    """
    orphan = DASHBOARD / "ai_settings.html"
    if not orphan.exists():
        return  # already removed by whoever owns it

    api = (REPO / "jarvis" / "api" / "app.py").read_text(encoding="utf-8")
    assert "ai_settings.html" not in api, (
        "ai_settings.html is being served, but it is an abandoned prototype "
        "that calls routes the backend has never had")
    for page in DASHBOARD.glob("*.html"):
        assert "ai_settings" not in page.read_text(encoding="utf-8"), (
            f"{page.name} links to ai_settings.html, an unreachable page")


def test_the_ownership_doc_names_the_page_that_ships():
    """``docs/FILE_OWNERSHIP.md`` named the page that was deleted."""
    doc = (REPO / "docs" / "FILE_OWNERSHIP.md").read_text(encoding="utf-8")
    assert "ai_settings.html" not in doc, (
        "the ownership doc still names ai_settings.html, which is not a page "
        "the product serves")
    assert "ai_connections.html" in doc, (
        "the ownership doc should name the page that actually ships")


# ---------------------------------------------------------------------------
# The pages, in a browser
# ---------------------------------------------------------------------------
@playwright_required
def test_mission_control_establishes_a_session_and_loads_its_providers(isolated_server):
    """The page works: a session, a real provider list, no console noise."""
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
        page.goto(f"{base}/mission-control", wait_until="networkidle")
        page.wait_for_timeout(3000)

        assert page.title(), "the page has no title"
        assert page.evaluate("() => !!S.token"), (
            "the page did not establish a session")
        assert page.evaluate("() => S.providers.length") > 0, (
            "the page loaded no providers")
        browser.close()

    assert not page_errors, f"the page raised: {page_errors}"
    assert not console_errors, f"the page logged errors: {console_errors}"


@playwright_required
def test_mission_control_names_its_providers_and_their_models(isolated_server):
    """No provider may render as "undefined".

    This is the defect that survived the auth fix: the session was fine, the
    request was fine, and the page still showed five providers called
    "undefined" with a dropdown full of empty options.
    """
    from playwright.sync_api import sync_playwright

    base = isolated_server["base"]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(f"{base}/mission-control", wait_until="networkidle")
        page.wait_for_timeout(3000)

        options = page.locator("#provider-select option").all_inner_texts()
        assert options, "the provider dropdown is empty"
        assert not any("undefined" in o.lower() for o in options), (
            f"the provider dropdown renders undefined: {options}")

        # Every option must carry the provider's own id, so a later action
        # addresses the provider the user picked.
        values = page.locator("#provider-select option").evaluate_all(
            "els => els.map(e => e.value)")
        assert all(v and v != "undefined" for v in values), (
            f"a provider option carries no usable value: {values}")

        models = page.locator("#model-select option").all_inner_texts()
        assert models, "the model dropdown is empty"
        assert not any(m.strip() == "" for m in models), (
            f"a model option is blank: {models}")
        browser.close()


@playwright_required
def test_mission_control_renders_a_key_row_per_provider(isolated_server):
    """The key list names the provider, not "undefined"."""
    from playwright.sync_api import sync_playwright

    base = isolated_server["base"]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(f"{base}/mission-control", wait_until="networkidle")
        page.wait_for_timeout(3000)

        names = page.locator(".key-provider-name").all_inner_texts()
        assert names, "no key rows were rendered"
        assert not any("undefined" in n.lower() for n in names), (
            f"a key row names an undefined provider: {names}")
        browser.close()


@playwright_required
def test_the_phone_actually_polls(isolated_server):
    """The phone's four-second poll must make requests.

    Before the fix the poll threw on its first statement, so the page made
    ZERO /devices and /confirmations requests: no device list, and no
    approval button for a person to tap.
    """
    from playwright.sync_api import sync_playwright

    base = isolated_server["base"]

    seen: List[str] = []
    page_errors: List[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.on("pageerror", lambda e: page_errors.append(str(e)[:120]))
        page.on("response",
                lambda r: seen.append(r.url) if any(
                    k in r.url for k in ("/devices", "/confirmations")) else None)
        page.goto(f"{base}/phone", wait_until="networkidle")
        page.wait_for_timeout(9000)
        browser.close()

    assert not page_errors, f"the page raised: {page_errors}"
    assert any("/devices" in u for u in seen), (
        f"the phone never asked for its devices; it saw only: {seen}")
    assert any("/confirmations" in u for u in seen), (
        f"the phone never asked for pending approvals; it saw only: {seen}")


@playwright_required
def test_the_phone_shows_its_state_without_an_error(isolated_server):
    """The page must render, not stand on a ReferenceError.

    The status line is the check that matters: with the poll dead it was still
    "connected" -- the *login* worked -- so a page that could authenticate but
    could not do anything looked healthy. The state the phone reports about
    itself has to come from the poll, and the poll has to have run.
    """
    from playwright.sync_api import sync_playwright

    base = isolated_server["base"]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(f"{base}/phone", wait_until="networkidle")
        page.wait_for_timeout(4000)

        status = page.locator("#status").inner_text()
        assert status.strip(), "the phone shows no status at all"
        assert "not defined" not in status, status
        # The poll sets the pairing state. Before the fix the poll died on its
        # first statement and the status stayed at the login's "connected" --
        # a page that could authenticate but could not do anything.
        assert status.strip() != "connected", (
            f"the phone still reports only the login state ({status!r}); the "
            "poll that sets the pairing state never ran")
        # The approval list must be a real element with real content -- an
        # empty approval list is a state; a missing one is a bug.
        assert page.locator("#conf").count() == 1, (
            "the phone has no approval list at all")
        browser.close()
