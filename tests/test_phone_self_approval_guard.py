"""A phone must not be able to approve its own consequential request.

The self-approval guard lives in the orchestrator: it refuses an approval
when the *same session* that raised the request answers it **and** the
approver is on a narrow surface (the phone). The second half of that
condition is the one that was not reaching the rule.

**The gap.** The companion gateway validates the session and learns its
surface ("phone"), then proxies the request to the backend with the **master**
token -- the phone's session token never touches the backend. The backend
therefore could not look the session up, and `/confirm` read the approver's
surface from a query parameter the phone never sent. It fell back to
"unknown", which is not a narrow surface, so **the guard was silently
disarmed for exactly the device it exists to constrain**: a phone could
approve its own `create_event`, and the action ran.

Measured, before the fix:

```
VERDICT PolicyDecision.CONFIRM
SURFACE_unknown  gateway (no surface forwarded) -> PolicyDecision.EXECUTE | Created Standup ...
SURFACE_phone    phone (surface known)         -> PolicyDecision.BLOCK   | You cannot approve your own requests.
```

**The fix, in two places.** The gateway forwards the surface it validated
(`X-Vantage-Surface`), and `/confirm` reads it. The header is set by the
*gateway*, not by the client, so a client cannot claim to be a different
surface than it is. The Android app also sends its session as `session_id`,
so the guard has the session to compare.

These tests pin the whole chain, from the gateway's header to the guard's
verdict.
"""

from __future__ import annotations

import asyncio
import json
import re

import pytest


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------
def _make_confirmation(core, tool="create_event", params=None, session_id="PHONE-1"):
    """Raise a confirmation the way the pipeline does."""
    from jarvis.core.models import PlannedAction

    action = PlannedAction(
        tool=tool,
        params=params or {"title": "Standup", "start": "2026-12-01 09:00"},
        reason="test",
    )
    verdict = core.policy.evaluate(action, session_id=session_id)
    assert verdict.decision.value == "CONFIRM", verdict
    assert verdict.confirmation_id
    return verdict.confirmation_id


def test_a_phone_cannot_approve_its_own_request(core):
    """The rule the whole thing exists for."""
    confirmation_id = _make_confirmation(core, session_id="PHONE-1")
    approved = asyncio.run(core.resolve_confirmation(
        confirmation_id, True, "PHONE-1",
        approver_session="PHONE-1", approver_surface="phone",
    ))
    assert approved.decision.value == "BLOCK", approved.reply
    assert "own request" in (approved.reply or "").lower(), approved.reply
    assert approved.actions == [], "a self-approved action must not run"


def test_a_request_raised_on_a_narrow_surface_needs_a_wider_approver(core):
    """A phone must not approve a phone request, even without a session to compare.

    This used to assert the opposite: that with no surface the approval went
    through, documenting the gap rather than closing it. The 2026-10-26 change
    made the rule depend on where the request was *raised*, which the
    confirmation already records -- so no matching identifier is required, and
    an unknown approver surface is not a way around the guard.

    It is deliberately conservative: it does not distinguish two handsets, only
    "narrow" from "not narrow", because telling handsets apart would mean
    storing session tokens in the database.
    """
    confirmation_id = _make_confirmation(core, session_id="PHONE-1")
    # Raise it on the phone surface, as the real pipeline does.
    record = core.policy.get_confirmation(confirmation_id)
    core.policy.db.execute(
        "UPDATE confirmations SET requested_surface = ? WHERE confirmation_id = ?",
        ("phone", confirmation_id))
    approved = asyncio.run(core.resolve_confirmation(
        confirmation_id, True, "PHONE-1",
        approver_session="PHONE-1", approver_surface="unknown",
    ))
    assert approved.decision.value == "BLOCK", (
        "a phone-raised request was approved without a wider surface")
    assert approved.actions == [], "a self-approved action must not run"


# ---------------------------------------------------------------------------
# The surface arrives
# ---------------------------------------------------------------------------
def test_the_gateway_forwards_the_surface_it_validated():
    """The gateway already knows the surface; it must now pass it on.

    Without this, the backend cannot tell a phone from the dashboard, and
    the guard cannot fire for a phone.
    """
    from pathlib import Path

    source = Path(__file__).resolve().parent.parent / "jarvis" / "companion" / "gateway.py"
    text = source.read_text(encoding="utf-8")
    assert '"X-Vantage-Surface"' in text, (
        "the gateway does not forward the surface it validated")
    # And it forwards the surface it resolved, not one the client chose.
    assert 'headers["X-Vantage-Surface"] = surface' in text, (
        "the forwarded surface is not the validated one")


def test_the_backend_reads_the_forwarded_surface():
    """`/confirm` reads the header the gateway sets."""
    from pathlib import Path

    source = Path(__file__).resolve().parent.parent / "jarvis" / "api" / "app.py"
    text = source.read_text(encoding="utf-8")
    assert "X-Vantage-Surface" in text, (
        "the backend does not read the forwarded surface")
    # The header is a *fallback*, not a replacement: a local caller's query
    # parameter still wins.
    assert "_surface_of(session_id)" in text, (
        "the local caller's session is no longer consulted")


def test_the_forwarded_surface_cannot_be_chosen_by_the_client():
    """A client must not be able to claim a different surface.

    The header is set by the gateway over the client's request, so a client
    that sends its own `X-Vantage-Surface` is ignored. That is a property of
    the gateway's proxy, and it is worth pinning: if the gateway ever
    forwarded the *client's* header instead of the validated one, a phone
    could claim to be the dashboard and the guard would not fire.
    """
    from pathlib import Path

    source = Path(__file__).resolve().parent.parent / "jarvis" / "companion" / "gateway.py"
    text = source.read_text(encoding="utf-8")
    # The forwarded value is the validated surface, never the request's header.
    assert 'headers["X-Vantage-Surface"] = surface' in text
    assert 'headers["X-Vantage-Surface"] = request.headers' not in text, (
        "the gateway forwards the client's own surface header -- a client could "
        "claim to be a different surface than it is")


# ---------------------------------------------------------------------------
# The Android client
# ---------------------------------------------------------------------------
def test_the_android_client_sends_its_session_when_approving():
    """The guard compares sessions, so the approval must carry one.

    Without it, `session_id` is empty and the guard has no "who" to compare
    -- the same empty-equals-empty case the orchestrator documents.
    """
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / "android" / "app" / "src"
              / "main" / "kotlin" / "com" / "vantage" / "companion" / "data"
              / "api" / "VantageApiClient.kt").read_text(encoding="utf-8")
    approve = re.search(r"suspend fun approveConfirmation.*?\n    \}", source, re.S)
    assert approve, "the approve call is gone"
    body = approve.group(0)
    assert "session_id=" in body, (
        "the approve call does not carry the approver's session")
    assert "approve=true" in body


def test_the_android_client_sends_its_session_when_denying():
    """A denial is recorded too -- who answered is a fact either way."""
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / "android" / "app" / "src"
              / "main" / "kotlin" / "com" / "vantage" / "companion" / "data"
              / "api" / "VantageApiClient.kt").read_text(encoding="utf-8")
    deny = re.search(r"suspend fun denyConfirmation.*?\n    \}", source, re.S)
    assert deny, "the deny call is gone"
    assert "session_id=" in deny.group(0), (
        "the deny call does not carry the approver's session")


# ---------------------------------------------------------------------------
# The phone's own web page
# ---------------------------------------------------------------------------
# The Android client was fixed to send its session. The PHONE'S OWN WEB PAGE --
# the one served at /phone, which is what a handset actually opens -- was not.
# It called `/confirm/{id}?approve=1` with no session at all, so the guard's
# condition could not be evaluated: `session_id` was empty, and an empty
# session has no "who" to compare. The guard was disarmed for the phone's own
# page exactly as it had been disarmed for the gateway.
#
# Measured, in the confirmation ledger, before the fix:
#
#   state=approved  requested_by_session=SESS-59a94a310292
#                   approved_by_session='SESS-59a94a310292'   <- the same session
#                   approved_surface='unknown'  requested_surface=phone
#
# After the fix the same tap answers:
#   "You cannot approve your own requests. Approve this on the laptop instead."

def _phone_page() -> str:
    from pathlib import Path

    return (Path(__file__).resolve().parent.parent / "dashboard"
            / "phone.html").read_text(encoding="utf-8")


def test_the_phone_page_sends_its_session_when_approving():
    """The guard compares sessions, so the approval must carry one.

    The phone page's `resolve()` called `/confirm/{id}?approve=1` with no
    session, which made the guard's own condition unevaluable and let the
    phone approve its own request.
    """
    source = _phone_page()
    resolve = re.search(r"async function resolve\(.*?\n      \}", source, re.S)
    assert resolve, "the phone page's resolve() is gone"
    body = resolve.group(0)
    assert "session_id=" in body, (
        "the phone page's approval does not carry the approver's session; "
        "the self-approval guard has no 'who' to compare and is disarmed")
    assert "encodeURIComponent" in body, (
        "the session id is not URL-encoded on the way to the query string")


def test_the_phone_page_keeps_the_session_the_server_hands_it():
    """The session id has to come from the server, not be invented.

    The page cannot read its own HttpOnly cookie, so the session id has to
    arrive in the `/command` response -- which carries it as `session_id`. A
    page that made one up would fail the comparison for the wrong reason.
    """
    source = _phone_page()
    assert "session_id" in source, (
        "the phone page does not use the server's session id at all")
    assignment = re.search(r"sessionId\s*=\s*data\.session_id", source)
    assert assignment, (
        "the phone page does not take the session id from the command "
        "response; it must use the one the server started the conversation with")


def test_the_phone_page_declares_the_session_it_keeps():
    """Same defect class as the other four: read before it is declared."""
    script = re.search(r"<script>(.*?)</script>", _phone_page(), re.S).group(1)
    assert re.search(r"\b(?:let|const|var)\s+sessionId\b", script), (
        "the phone page uses sessionId without declaring it")


# ---------------------------------------------------------------------------
# The real client shapes
#
# The tests above call resolve_confirmation directly. These drive the HTTP
# route the way a phone actually does, because that is where the guard was
# dead: the confirmation stored the *conversation* id (SESS-...) while the
# approver presented the *session token*, so `requested_session == session_id`
# could never be true.
# ---------------------------------------------------------------------------


#: The suite's fixed master token (see conftest / test_api_contract).
TEST_TOKEN = "test-token-do-not-use-in-production"


@pytest.fixture(scope="module")
def client():
    """An authenticated client on a loopback Host, which is what the gate expects."""
    from fastapi.testclient import TestClient

    from jarvis.api.app import app

    return TestClient(app, base_url="http://127.0.0.1:8765",
                      headers={"X-JARVIS-Token": TEST_TOKEN})


@pytest.fixture(scope="module")
def api_token():
    """The master token -- the Mac operator's credential."""
    return TEST_TOKEN


def _phone_session(client):
    import re as _re
    page = client.get("/phone").text
    bootstrap = _re.search(r'name="jarvis-bootstrap" content="([^"]+)"', page)
    assert bootstrap, "no phone bootstrap"
    response = client.post("/auth/session", json={"bootstrap": bootstrap.group(1)},
                           headers={"Origin": "http://127.0.0.1:8765"})
    assert response.status_code == 200, response.text
    return response.json()["session_token"]


def _raise_phone_write(client, token):
    headers = {"X-JARVIS-Token": token, "X-Vantage-Device-Id": "phone_main"}
    response = client.post("/command", json={
        "text": "add a task to Google Tasks Clean the garage",
        "device_id": "phone_main", "source": "mobile"}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json().get("confirmation_id")


@pytest.mark.parametrize("send_session_id", [False, True])
def test_a_real_phone_cannot_approve_its_own_write(client, send_session_id):
    """Neither shape may self-approve.

    ``send_session_id=False`` is the shape that was broken: the Android client
    does send it, but nothing guaranteed it, and the guard depended on it.
    """
    token = _phone_session(client)
    headers = {"X-JARVIS-Token": token}
    confirmation_id = _raise_phone_write(client, token)
    assert confirmation_id, "the write did not ask for confirmation"

    url = f"/confirm/{confirmation_id}?approve=true"
    if send_session_id:
        url += f"&session_id={token}"
    result = client.post(url, headers=headers, json={})
    reply = str(result.json().get("reply", ""))
    assert "cannot approve your own" in reply.lower(), (
        f"a phone approved its own request (session_id sent={send_session_id}): {reply[:120]}")


def test_a_phone_can_still_deny_its_own_request(client):
    """A denial is never a self-approval."""
    token = _phone_session(client)
    confirmation_id = _raise_phone_write(client, token)
    result = client.post(f"/confirm/{confirmation_id}?approve=false",
                         headers={"X-JARVIS-Token": token}, json={})
    reply = str(result.json().get("reply", ""))
    assert "cannot approve your own" not in reply.lower(), reply[:120]


def test_the_laptop_can_still_approve_a_phones_request(client, api_token):
    """The guard must not break the ordinary cross-device approval.

    The operator approving from the Mac is the intended flow, and the refusal
    text has always told people to do exactly this. It must not be caught by
    the rule that protects it.
    """
    token = _phone_session(client)
    confirmation_id = _raise_phone_write(client, token)
    result = client.post(f"/confirm/{confirmation_id}?approve=true",
                         headers={"X-JARVIS-Token": api_token}, json={})
    reply = str(result.json().get("reply", ""))
    assert "cannot approve your own" not in reply.lower(), (
        f"the laptop was refused: {reply[:120]}")


def test_a_refused_self_approval_leaves_the_request_answerable(client, api_token):
    """The refusal must not destroy what it tells the person to go and approve.

    This used to resolve the confirmation as *denied*: the phone was told
    "approve this on the laptop" while the record had already been marked
    denied, so the laptop could not approve it either. Measured on a live
    server. Refusing the attempt and cancelling the request are different acts.
    """
    token = _phone_session(client)
    confirmation_id = _raise_phone_write(client, token)

    refused = client.post(f"/confirm/{confirmation_id}?approve=true",
                          headers={"X-JARVIS-Token": token}, json={})
    body = refused.json()
    assert "cannot approve your own" in str(body.get("reply", "")).lower()
    assert body.get("needs_confirmation") is True, (
        "the refusal told the phone it was still waiting but cleared the flag")

    # The Mac can now answer it.
    approved = client.post(f"/confirm/{confirmation_id}?approve=true",
                           headers={"X-JARVIS-Token": api_token}, json={})
    reply = str(approved.json().get("reply", ""))
    assert "already denied" not in reply.lower(), (
        f"the refused attempt had cancelled the request: {reply[:120]}")
    assert "cannot approve your own" not in reply.lower(), (
        f"the laptop was refused: {reply[:120]}")


def test_the_local_phone_page_is_treated_like_any_other_handset(client):
    """A known limitation, asserted so it cannot silently change.

    The /phone page opened in a browser on this Mac gets a session labelled
    "phone", so it cannot approve a request it raised -- even though the person
    is sitting at the laptop. It is *not* special-cased, because over HTTP it
    and a remote handset are indistinguishable: the companion gateway proxies
    from 127.0.0.1 and swaps the session token for the master token, so with no
    X-Vantage-Surface present the two requests are identical. Both ways were
    measured.

    Between a convenience for one page and the invariant that a device may not
    approve its own consequential request, the invariant wins. The request is
    left pending, and the dashboard or the native app answers it.

    If this ever becomes worth fixing, the fix is a surface the page can earn
    (its own bootstrap label), not a loopback check.
    """
    token = _phone_session(client)
    confirmation_id = _raise_phone_write(client, token)
    result = client.post(f"/confirm/{confirmation_id}?approve=true",
                         headers={"X-JARVIS-Token": token}, json={})
    reply = str(result.json().get("reply", ""))
    assert "cannot approve your own" in reply.lower(), reply[:120]
    # Still answerable by the operator, which is what makes this tolerable.
    assert result.json().get("needs_confirmation") is True


def test_a_gateway_proxied_phone_still_cannot_approve_itself(client):
    """The gateway proxies from 127.0.0.1, so loopback alone must not mean "operator".

    It sets X-Vantage-Surface on the way through; that header -- not the peer
    address -- is what says the caller is a remote handset. Getting this wrong
    reopens the hole this whole module exists to close.
    """
    token = _phone_session(client)
    confirmation_id = _raise_phone_write(client, token)
    result = client.post(
        f"/confirm/{confirmation_id}?approve=true",
        headers={"X-JARVIS-Token": token, "X-Vantage-Surface": "phone"}, json={})
    reply = str(result.json().get("reply", ""))
    assert "cannot approve your own" in reply.lower(), (
        f"a gateway-proxied phone approved its own request: {reply[:120]}")
