"""The AI Connections page must describe the providers the server actually has.

**The page described a provider that does not exist, and hid the one that
does.** The page hardcoded three provider cards -- ``openai``, ``gemini`` and
``shree`` -- while the server's registry has five:

    openai, gemini, groq, 9router, openai_compatible

So the page offered "Shree AI", a provider that appears **nowhere** in the
backend: ``POST /ai/providers/shree/test`` answers ``404 {"detail":"Provider
shree not found"}``, and the page's own Test button rendered that refusal as
"Connection failed" against a "Failed" badge. Meanwhile **9Router** -- the
provider actually in use, the one the assistant is answering with -- had no
card at all, so there was no way to see or manage its key from this page.

Two more defects in the same page:

* **The provider dropdown could not be changed.** The page re-read
  ``/ai/selection`` on the dropdown's ``change`` event, and the server's
  answer set the dropdown back to the *current* provider. Picking "gemini"
  snapped straight back to "9router", so "Save selection" would have saved
  the old provider and the assistant's vendor could not be changed here at
  all.
* **The "AI Usage Controls" checkboxes did nothing.** "Use free/plan
  allowance", "Allow paid API fallback" and "Require confirmation before paid
  fallback" had no handler, no route, and no setting behind them --
  ``find_compatible_providers`` -- the only code that reads ``free_first`` and
  ``allow_paid_fallback`` -- is not called from anywhere in the live path. They
  were a control that looked real and did nothing.

The page is now built from ``GET /ai/providers``, so a provider added to the
backend appears without a second edit that can be forgotten, and the three
checkboxes are gone rather than left pretending.

The rules are pinned from the **server's** registry, not from a list written
here, so a provider that disappears from the backend fails this file rather
than leaving a card behind.
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


def _page(name: str = "ai_connections.html") -> str:
    return (DASHBOARD / name).read_text(encoding="utf-8")


def _registry() -> List[str]:
    """The provider ids the backend actually has.

    Read from the adapter registry -- the one dictionary the whole project
    treats as the source of truth -- rather than from a list written here.
    """
    from jarvis.gateway.providers import PROVIDERS

    return sorted(PROVIDERS)


# ---------------------------------------------------------------------------
# The page's own source, checked without a browser
# ---------------------------------------------------------------------------
def test_the_page_offers_exactly_the_providers_the_server_has():
    """No card for a provider the server lacks, and none missing.

    The page used to hardcode ``openai``, ``gemini`` and ``shree`` -- offering
    a provider that does not exist and omitting 9Router, the one in use.
    """
    source = _page()
    registry = _registry()

    # The page must not name a provider the registry does not have.
    for name in re.findall(r'data-provider="([a-z0-9_]+)"', source):
        assert name in registry, (
            f"the page offers a provider named {name!r}, which the server does "
            f"not have; the server knows: {registry}")

    assert "shree" not in source.lower(), (
        "the page still names a provider the backend has never had")

    # And the page must not hardcode a list of provider cards at all: the list
    # comes from GET /ai/providers.
    assert "/ai/providers" in source, (
        "the page does not read the provider list from the server")
    assert "known_providers" in source, (
        "the page does not read the provider list the server reports")


def test_the_page_does_not_hardcode_provider_cards():
    """The cards are built from the server's list, not written into the HTML.

    A hardcoded card is a card that goes stale: the page carried one for
    "Shree AI" for weeks, and the server has never had that provider.
    """
    source = _page()
    assert not re.search(r'data-provider="openai"', source), (
        "the page hardcodes a provider card; the cards must be built from "
        "GET /ai/providers")
    assert "buildCard" in source, (
        "the page does not build provider cards from the server's list")


def test_the_usage_controls_that_did_nothing_are_gone():
    """Three checkboxes that did nothing must not remain on the page.

    "Use free/plan allowance", "Allow paid API fallback" and "Require
    confirmation before paid fallback" had no handler and no setting behind
    them. The only code that reads ``free_first`` and ``allow_paid_fallback``
    is ``find_compatible_providers``, which nothing in the live path calls.

    A control that looks real and does nothing is worse than no control: it
    says "paid fallback is off" when nothing was ever reading the switch.
    """
    source = _page()
    for name in ("use-free-first", "allow-paid-fallback",
                 "require-paid-confirmation"):
        assert name not in source, (
            f"the page still shows the {name!r} control, which does nothing")
    assert "AI Usage Controls" not in source, (
        "the page still shows the AI Usage Controls card")


def test_the_page_stores_and_removes_keys_through_the_routes_that_exist():
    """``POST .../store`` and ``DELETE .../credentials``.

    Those are the real routes. A page that calls ``PUT .../key`` or
    ``DELETE .../key`` fails at every key operation.
    """
    source = _page()
    assert "/store" in source, (
        "the page does not store keys through POST /ai/providers/{id}/store")
    assert "/credentials" in source, (
        "the page does not delete keys through DELETE "
        "/ai/providers/{id}/credentials")


def test_the_save_button_only_appears_after_a_test_passes():
    """A key that does not work is never offered for storage.

    The Save button starts hidden and is revealed by the test handler. The
    button must be in the page with that hidden state -- a Save button that
    is always visible would store a key the provider never accepted.
    """
    source = _page()
    assert "js-save-api" in source, "the page has no Save Key button"
    assert re.search(r'js-save-api[^"]*"\s*\n?[^>]*classList\.remove\("hidden"\)', source) \
        or "classList.remove(\"hidden\")" in source, (
        "the page never reveals the Save Key button")
    # The reveal must be inside the *success* branch of the test, not before it.
    success = re.search(
        r"if \(res\.ok && data\.success\) \{(.*?)\n {12}\}", source, re.S)
    assert success, "the test handler has no success branch"
    assert "js-save-api" in success.group(1), (
        "the Save Key button is not revealed by a successful test")


def test_a_failed_test_does_not_offer_the_save_button():
    """The failure branch must hide the button again."""
    source = _page()
    failure = re.search(
        r"\} else \{\s*\n\s*setMessage\(\s*\n?\s*card\.message,", source)
    assert failure, "the test handler has no failure branch"
    tail = source[failure.start(): failure.start() + 900]
    assert "js-save-api" in tail and "hidden" in tail, (
        "a failed test does not withdraw the Save Key button")


def test_the_page_does_not_print_a_stored_key():
    """The key is never shown again once it is stored.

    The page clears the input after a successful store. A key left in the
    field would be sitting in the DOM, in a screenshot, and in the browser's
    history for whoever comes next.
    """
    source = _page()
    assert "card.input.value = \"\"" in source or ".value = \"\"" in source, (
        "the page does not clear the key field after storing it")


# ---------------------------------------------------------------------------
# The page, in a browser
# ---------------------------------------------------------------------------
@playwright_required
def test_the_page_lists_the_servers_providers_and_no_others(isolated_server):
    """One card per provider the server has, and a card for each.

    This is the check that would have caught the "Shree AI" card and the
    missing 9Router card.
    """
    from playwright.sync_api import sync_playwright

    base = isolated_server["base"]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        errors: List[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)[:150]))
        page.goto(f"{base}/ai-connections", wait_until="networkidle")
        page.wait_for_timeout(2500)

        cards = page.locator(".provider-card").evaluate_all(
            "els => els.map(e => e.id.replace('provider-', ''))")
        registry = _registry()

        assert sorted(cards) == registry, (
            f"the page shows {sorted(cards)}; the server has {registry}")
        assert "shree" not in cards, (
            "the page still offers a provider the server does not have")
        assert "9router" in cards, (
            "the page has no card for the provider actually in use")
        browser.close()

    assert not errors, f"the page raised: {errors}"


@playwright_required
def test_the_provider_dropdown_can_actually_be_changed(isolated_server):
    """Picking a provider must hold.

    The page used to re-read ``/ai/selection`` on ``change``, which set the
    dropdown back to the current provider -- so a person could pick "gemini"
    and watch it snap back to "9router", and "Save selection" would have saved
    the old provider.
    """
    from playwright.sync_api import sync_playwright

    base = isolated_server["base"]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(f"{base}/ai-connections", wait_until="networkidle")
        page.wait_for_timeout(2000)

        before = page.locator("#active-provider").input_value()
        other = next(
            (x for x in page.locator("#active-provider option").evaluate_all(
                "els => els.map(e => e.value)") if x != before),
            "",
        )
        assert other, "the dropdown offers no other provider to pick"

        page.select_option("#active-provider", other)
        page.wait_for_timeout(1000)

        after = page.locator("#active-provider").input_value()
        assert after == other, (
            f"the dropdown snapped back from {other!r} to {after!r}; the "
            "assistant's vendor could not be changed from this page")
        browser.close()


@playwright_required
def test_a_failed_test_never_offers_the_save_button(isolated_server):
    """A key the provider refuses is not offered for storage.

    The Save button is hidden until a real test passes. This drives the
    failure path with a key the provider cannot accept.
    """
    from playwright.sync_api import sync_playwright

    base = isolated_server["base"]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(f"{base}/ai-connections", wait_until="networkidle")
        page.wait_for_timeout(2000)

        # A key too short to be a key: the page refuses it before any request.
        page.locator('.js-show-api-input[data-provider="groq"]').click()
        page.wait_for_timeout(200)
        page.fill('.api-key-input[data-provider="groq"]', "short")
        page.locator('.js-test-api[data-provider="groq"]').click()
        page.wait_for_timeout(800)

        message = page.locator('.message[data-provider="groq"]').inner_text().strip()
        assert message, "the page said nothing about the rejected key"
        assert not page.locator(
            '.js-save-api[data-provider="groq"]').is_visible(), (
            "the Save Key button appeared for a key that was never accepted")
        browser.close()
