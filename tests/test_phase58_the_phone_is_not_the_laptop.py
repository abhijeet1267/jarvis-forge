"""Phase 58 -- the phone is a surface, not a second copy of the laptop.

Measured before writing any of it: a phone that finishes ``POST /auth/session`` today holds a
token that reaches **every** protected route -- `/privacy/delete`, `/audit`, memory, workspaces,
schedules -- although `dashboard/phone.html` calls ten paths and nothing else. The credential is
header-only and unscoped, `source: "phone"` is metadata rather than authority, and no session can
be revoked selectively (`clear_dashboard_credentials` has no production caller and expires
everything at once). A phone is the one surface that gets left in a bag.

The shape chosen here, and why:

* **The code carries the surface, not the client.** `/phone` mints a phone-scoped bootstrap and
  `/dashboard` mints a dashboard-scoped one, at issue time. A device cannot ask for a wider
  surface in the redeem body, because the answer was decided before it held anything.
* **Enforce at the gate, in one place.** A refusal must not depend on every route remembering to
  check. And a refusal may not half-run the action: the state has to be untouched.
* **The laptop keeps its full console, deliberately.** The asymmetry is recorded rather than
  tidied away -- the panel is the operator's console on the machine itself; the phone is remote,
  small, and losable. Widening or narrowing it is a decision, not a detail.
* **Nothing may be called native.** The store build is impossible on this machine (no Xcode, no
  iOS SDK, zero signing identities, no Android SDK, no handset). The manifest and the shipped
  pages must not imply otherwise, and a guard says so.

The service worker's most important property is already true and is only true by accident: dynamic
endpoints are never served from cache, so a stale approvals list cannot be presented as live. This
phase makes that a tested property instead of a coincidence.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH_HEADERS, TEST_TOKEN

# Every path the phone page actually touches, grepped out of dashboard/phone.html
# rather than remembered. Adding a call to the page means adding it here, in review.
PHONE_PATHS = {
    "/auth/session",
    "/command",
    "/confirmations",
    "/confirm/whatever",
    "/devices",
    "/devices/pair/request",
    "/emergency_stop",
    "/tools",
    "/voice/status",
    "/voice/transcribe",
}

# Reached with a phone session today, and must not be.
NOT_FOR_A_PHONE = [
    "/privacy/delete",
    "/audit",
    "/health",
    "/metrics",
    "/permissions",
    "/permissions/policy",
    "/persistence",
    "/privacy/export",
]


def _api():
    return importlib.import_module("jarvis.api.app")


# The origin gate refuses /auth/session without a same-origin Origin header. A browser always
# sends one; TestClient never does, so the helper sends what the handset would send rather than
# turning the gate off.
ORIGIN = {"Origin": "http://127.0.0.1:8765"}


def _phone_session(client) -> str:
    """Authenticate the way the page does: fetch /phone, take the bootstrap out of the HTML,
    exchange it. Anything else would test a path no client can take."""
    html = client.get("/phone").text
    found = re.search(r'name="jarvis-bootstrap" content="([^"]+)"', html)
    assert found, "the served phone page carries no bootstrap code"
    res = client.post("/auth/session", json={"bootstrap": found.group(1)}, headers=ORIGIN)
    assert res.status_code == 200, res.text
    token = res.json().get("session_token")
    assert token, res.text
    return token


@pytest.fixture()
def client():
    with TestClient(_api().app, base_url="http://127.0.0.1:8765") as test_client:
        yield test_client


@pytest.fixture()
def phone(client):
    return _phone_session(client)


# ---------------------------------------------------------------------------
# 1. the code carries the surface, decided before any client holds it
# ---------------------------------------------------------------------------


def test_a_phone_code_mints_a_phone_session(client):
    token = _phone_session(client)
    surface = _api()._surface_of(token)
    assert surface == "phone"


def test_a_dashboard_code_mints_a_dashboard_session(client):
    html = client.get("/dashboard").text
    found = re.search(r'name="jarvis-bootstrap" content="([^"]+)"', html)
    assert found, "the served dashboard carries no bootstrap code"
    res = client.post("/auth/session", json={"bootstrap": found.group(1)}, headers=ORIGIN)
    assert res.status_code == 200
    assert _api()._surface_of(res.json()["session_token"]) == "dashboard"


def test_the_redeem_body_cannot_choose_a_wider_surface(client):
    """The phone page cannot ask to be a console, and a stolen handset cannot either: the surface
    was fixed when the code was minted, by the route that minted it."""
    html = client.get("/phone").text
    code = re.search(r'name="jarvis-bootstrap" content="([^"]+)"', html).group(1)
    res = client.post("/auth/session", json={"bootstrap": code, "surface": "dashboard"},
                      headers=ORIGIN)
    assert res.status_code == 200
    token = res.json()["session_token"]
    assert _api()._surface_of(token) == "phone"
    assert client.get("/health", headers={"X-JARVIS-Token": token}).status_code == 403


def test_an_unknown_or_missing_code_is_refused_before_anything_is_issued(client):
    res = client.post("/auth/session", json={"bootstrap": "0" * 32}, headers=ORIGIN)
    assert res.status_code == 403, res.text   # the route's own word for "no such code"
    assert not res.json().get("session_token"), res.text
    body = client.get("/phone")
    assert body.status_code == 200  # the page itself stays reachable; pairing is a two-sided act


# ---------------------------------------------------------------------------
# 2. the gate, in one place, refusing without half-running the action
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", sorted(PHONE_PATHS - {"/auth/session"}))
def test_the_phone_reaches_every_route_the_page_uses(client, phone, path):
    headers = {"X-JARVIS-Token": phone}
    target = "/confirm/whatever" if path.startswith("/confirm/") else path
    verb = client.get if path in ("/command", "/confirm/whatever") else client.get
    # /command and /confirm/{id} are POST-only; probe with the right verb so a 405 is never
    # mistaken for a scoping answer.
    if path == "/command":
        res = client.post("/command", json={"text": "what time is it", "source": "phone"},
                          headers=headers)
    elif path.startswith("/confirm/"):
        res = client.post("/confirm/does-not-exist?approve=0", json={}, headers=headers)
    elif path in ("/devices/pair/request", "/emergency_stop", "/voice/transcribe"):
        res = client.post(path, json={}, headers=headers)
    else:
        res = verb(target, headers=headers)
    assert res.status_code not in (401, 403), (path, res.status_code, res.text)
    del phone, client  # keep the parametrised ids readable


def test_a_phone_session_cannot_reach_the_laptop_only_routes(client, phone):
    headers = {"X-JARVIS-Token": phone}
    for path in NOT_FOR_A_PHONE:
        # /privacy/delete is POST-only; asking it with GET would read a 405 from
        # the router and never reach the gate under test.
        res = (client.post(path, json={"scope": "all"}, headers=headers)
               if path == "/privacy/delete" else client.get(path, headers=headers))
        assert res.status_code == 403, (path, res.status_code, res.text)


def test_the_refusal_names_the_surface_and_not_a_secret(client, phone):
    res = client.post("/privacy/delete", json={"scope": "all"},
                      headers={"X-JARVIS-Token": phone})
    assert res.status_code == 403
    detail = str(res.json()).lower()
    assert "phone" in detail, res.text
    assert TEST_TOKEN not in res.text
    assert phone not in res.text, "a refusal that echoes the credential is a disclosure"


def test_a_refusal_leaves_the_world_unchanged(client, phone):
    """The point of a gate is that the thing behind it did not happen. /privacy/delete is the
    worst case to get wrong, so the row count is read before and after."""
    headers = {"X-JARVIS-Token": phone}
    full = client.get("/memory/overview", headers=AUTH_HEADERS)
    before = full.json().get("total") if full.status_code == 200 else None
    res = client.post("/privacy/delete", json={"scope": "all"}, headers=headers)
    assert res.status_code == 403, res.text
    after = client.get("/memory/overview", headers=AUTH_HEADERS).json().get("total")
    if before is not None:
        assert after == before, "a refused deletion must delete nothing"


def test_the_master_token_still_reaches_everything(client):
    """The CLI and the laptop's own tooling are documented against the token, not a session;
    scoping sessions must not quietly cut them off."""
    for path in ("/health", "/permissions", "/persistence", "/metrics"):
        assert client.get(path, headers=AUTH_HEADERS).status_code == 200, path


def test_a_dashboard_session_still_reaches_the_console(client):
    html = client.get("/dashboard").text
    code = re.search(r'name="jarvis-bootstrap" content="([^"]+)"', html).group(1)
    token = client.post("/auth/session", json={"bootstrap": code},
                        headers=ORIGIN).json()["session_token"]
    headers = {"X-JARVIS-Token": token}
    for path in ("/health", "/permissions", "/persistence", "/audit", "/metrics"):
        assert client.get(path, headers=headers).status_code == 200, path


def test_a_session_issued_from_a_session_keeps_its_surface(client, phone):
    """/auth/token hands a fresh session to a caller that already has one. Inheriting the surface
    is the whole point: a phone cannot launder its way to a console through a re-issue."""
    res = client.get("/auth/token", headers={"X-JARVIS-Token": phone, **ORIGIN})
    assert res.status_code == 200, res.text
    fresh = res.json().get("session_token") or res.json().get("token")
    assert fresh, res.text
    assert _api()._surface_of(fresh) == "phone"
    assert client.get("/health", headers={"X-JARVIS-Token": fresh}).status_code == 403


# ---------------------------------------------------------------------------
# 3. signing out, and revoking one session without killing every other
# ---------------------------------------------------------------------------


def test_signing_out_ends_that_session_and_only_that_session(client):
    first = _phone_session(client)
    second = _phone_session(client)
    res = client.delete("/auth/session", headers={"X-JARVIS-Token": first})
    assert res.status_code == 200, res.text
    assert client.get("/tools", headers={"X-JARVIS-Token": first}).status_code == 401
    assert client.get("/tools", headers={"X-JARVIS-Token": second}).status_code == 200


def test_signing_out_twice_reports_the_same_truth(client, phone):
    assert client.delete("/auth/session", headers={"X-JARVIS-Token": phone}).status_code == 200
    again = client.delete("/auth/session", headers={"X-JARVIS-Token": phone})
    assert again.status_code == 401, again.text


def test_a_laptop_can_list_sessions_without_showing_any_token(client):
    """Phase 10's finding applies to sessions as much as to responses: the list must identify
    without disclosing. A person deciding which handset to cut off needs surface and age, not the
    credential."""
    token = _phone_session(client)
    rows = client.get("/auth/sessions", headers=AUTH_HEADERS).json()
    assert rows, rows
    entry = rows[-1]
    assert entry["surface"] == "phone"
    assert "expires_in" in entry
    assert token not in str(rows), "a session list that prints tokens is a token store"
    assert TEST_TOKEN not in str(rows)


def test_a_laptop_can_end_one_session_by_its_reference(client):
    token = _phone_session(client)
    rows = client.get("/auth/sessions", headers=AUTH_HEADERS).json()
    ref = rows[-1]["session_ref"]
    assert client.delete(f"/auth/sessions/{ref}", headers=AUTH_HEADERS).status_code == 200
    assert client.get("/tools", headers={"X-JARVIS-Token": token}).status_code == 401


def test_an_unknown_session_reference_is_not_an_invitation_to_guess(client):
    res = client.delete("/auth/sessions/nonexistent-ref", headers=AUTH_HEADERS)
    assert res.status_code == 404


# ---------------------------------------------------------------------------
# 4. the worker may never make yesterday look like now
# ---------------------------------------------------------------------------


def test_the_worker_caches_the_shell_and_nothing_that_changes(client):
    sw = client.get("/sw.js")
    assert sw.status_code == 200
    body = sw.text
    precached = re.search(r"PRECACHE\s*=\s*\[([^\]]*)\]", body)
    assert precached, "the shell list is gone"
    paths = re.findall(r'"(/[^"]+)"', precached.group(1))
    assert "/phone" in paths
    for live in ("/command", "/confirmations", "/devices", "/health", "/audit", "/tasks"):
        assert live not in paths, f"{live} is dynamic and must not be in the precache"


def test_a_dynamic_request_is_never_answered_from_the_cache(client):
    """Property, not coincidence: the fetch handler returns a bare fetch() for anything that is
    not the shell, so an approvals list cannot be served stale."""
    body = client.get("/sw.js").text
    # The branch that matters is the one after the shell block returns: that is where every
    # API call lands, and it must be a bare network fetch with no cache in sight.
    dynamic = body[body.rindex("event.respondWith"):]
    assert "caches" not in dynamic, \
        "a dynamic path must not be answered from a cache"
    assert "fetch(request)" in dynamic


def test_the_cache_name_carries_a_version_so_an_update_replaces_it(client):
    body = client.get("/sw.js").text
    names = set(re.findall(r"phone-shell-v(\d+)", body))
    assert names, "no versioned shell cache"
    assert "2" in names, \
        "the shell changed this phase; the cache name has to change with it or phones keep v1"


# ---------------------------------------------------------------------------
# 5. landing where you tapped, and naming what this is
# ---------------------------------------------------------------------------


def test_a_refused_credential_stops_the_page_from_polling():
    """Found by pressing Sign out in a real browser: the page kept its four-second loop and
    every later call came back 401, which is 159 identical console errors in three minutes and
    no information for the person holding the phone. A refused credential is not a transient
    failure -- the next poll will fail for the same reason -- so the loop has to end and say so
    once."""
    html = Path("dashboard/phone.html").read_text(encoding="utf-8")
    assert "clearInterval" in html, "the poll never stops"
    timer = re.search(r"(?:pollTimer|timer)\s*=\s*setInterval", html)
    assert timer, "the interval is not kept, so it cannot be stopped"
    assert html.count("authLost") >= 2, "nothing remembers that the credential was refused"
    # And the check has to be reachable. The first version put it in refresh()'s catch,
    # which the swallowed rejections never reach: the guard existed and did nothing.
    body = html[html.index("async function refresh("):]
    guard = body.index("if (authLost)")
    assert guard < body.index("Promise.all"), \
        "the guard runs after the calls it is meant to prevent, so it prevents nothing"


def test_a_link_can_open_the_approvals_directly(client):
    """A notification or a saved link should land on the decision, not on the top of a page the
    user then has to scroll to find."""
    page = client.get("/phone?focus=approvals")
    assert page.status_code == 200
    html = Path("dashboard/phone.html").read_text(encoding="utf-8")
    assert "focus" in html
    assert re.search(r"URLSearchParams|location\.search", html), \
        "the parameter is read by the page, not by the server"


def test_the_deep_link_lands_after_the_page_has_finished_growing():
    """Measured in a 320x568 viewport: the one-shot version scrolled before /tools had rendered
    the quick actions above the card, so by the time the page settled the approvals card was
    below the fold again -- the link "worked" in the DOM and failed on the screen. The landing is
    re-applied once, after the render that moves everything, and only once: taking the scroll
    position back every four seconds would be worse than the bug it fixes."""
    html = Path("dashboard/phone.html").read_text(encoding="utf-8")
    assert html.count("applyFocus") >= 2, \
        "the focus landing is applied at load only, which the small-viewport case defeats"
    refresh_block = html[html.index("async function refresh("):]
    refresh_block = refresh_block[:refresh_block.index("\nasync function")
                                  if "\nasync function" in refresh_block[1:] else 4000]
    assert "applyFocus" in refresh_block, \
        "it has to run after the data renders, not only when the script is parsed"
    assert "focusApplied" in html, "and it has to stop after the first time"


def test_nothing_in_the_shipped_phone_surface_claims_to_be_native(client):
    """No Xcode, no iOS SDK, zero signing identities, no Android SDK, no handset: a store build
    cannot be produced or verified here, so no shipped file may imply one exists."""
    for name in ("phone.html", "sw.js", "manifest.webmanifest"):
        text = (Path("dashboard") / name).read_text(encoding="utf-8").lower()
        assert "native app" not in text, name
        assert "install the app from the app store" not in text, name
    manifest = client.get("/manifest.webmanifest").json()
    assert manifest["name"] == "Vantage on this laptop"


# ---------------------------------------------------------------------------
# 6. the audit says what was refused
# ---------------------------------------------------------------------------


def test_a_refused_scope_is_written_to_the_audit(client, phone):
    client.post("/privacy/delete", json={"scope": "all"}, headers={"X-JARVIS-Token": phone})
    rows = client.get("/audit?limit=20", headers=AUTH_HEADERS).json()
    entry = next((r for r in rows if "scope" in str(r).lower() and r.get("result") == "denied"),
                 None)
    assert entry is not None, rows
