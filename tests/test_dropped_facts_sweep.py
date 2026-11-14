"""A fact computed at one layer and never reaching the layer that needs it.

This tree has now produced four of them, and they are all the same shape: a
value is produced, carried part of the way, and then dropped before the rule
that depends on it can see it. The first three were found by accident; this
sweep looked for the shape deliberately.

| The fact | Produced at | Needed by | What happened |
| --- | --- | --- | --- |
| the confirmation's fields | the server's row | the macOS `Confirmation` model | the model named fields the server never sends, so decoding failed and the Approvals screen showed an error |
| the page's text | the browser controller | whoever asked to read the page | the controller could open, click and type, but not read |
| the approver's surface | the gateway | the self-approval guard | the guard was disarmed for exactly the phone it exists to constrain |
| the request's surface | the request's `source` | the self-approval guard | it was guessed from the request id with a heuristic that could never fire |

The last one is the subject of the two tests here. The others are recorded in
the engineering log and pinned by their own files.

**What makes a fact "dropped" is that it is produced and then read by nobody.**
So these tests are largely about *readers*: a fact nobody reads is a fact
nobody is relying on, and one that is read by the wrong rule is worse.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# The request's surface
# ---------------------------------------------------------------------------
def test_a_confirmation_records_the_surface_that_raised_it(core):
    """A phone's request is recorded as a phone's request.

    The value used to be guessed with ``"phone" if "/" in request_id`` — and a
    request id is ``REQ-<hex>``, so the guess could never be "phone" and every
    confirmation was recorded as "dashboard", whatever raised it.
    """
    import asyncio

    from jarvis.core.models import CommandRequest

    response = asyncio.run(core.handle(CommandRequest(
        text="schedule a meeting called Standup on 2026-12-01 09:00",
        source="mobile",
        device_id="phone_main",
        session_id="PHONE-SESSION-X",
    )))
    assert response.confirmation_id, response.reply

    rows = core.policy.pending()
    row = next(r for r in rows if r["confirmation_id"] == response.confirmation_id)
    assert row["requested_surface"] == "phone", (
        f"a request raised by a phone was recorded as {row['requested_surface']!r}"
    )


def test_a_local_request_is_not_recorded_as_a_phone(core):
    """The converse, so the fix is not just "everything is a phone"."""
    import asyncio

    from jarvis.core.models import CommandRequest

    response = asyncio.run(core.handle(CommandRequest(
        text="schedule a meeting called Review on 2026-12-01 09:00",
        source="cli",
        session_id="LOCAL-SESSION-X",
    )))
    rows = core.policy.pending()
    row = next(r for r in rows if r["confirmation_id"] == response.confirmation_id)
    assert row["requested_surface"] != "phone", row["requested_surface"]


def test_the_request_id_heuristic_is_gone():
    """The guess that could never fire must not be back.

    A request id is ``REQ-<hex>``; it never contains a slash. So the old
    expression was a constant "dashboard" wearing the costume of a decision.
    """
    source = (REPO / "jarvis" / "security" / "policy.py").read_text(encoding="utf-8")
    # Checked in the CODE, not in the file: the string appears in the comment
    # that explains why the heuristic was removed.
    code = "\n".join(
        line for line in source.splitlines()
        if not line.strip().startswith("#")
    )
    assert '"phone" if "/" in request_id' not in code, (
        "the request id heuristic is back -- it can never fire")
    assert 'requested_surface or "unknown"' in code, (
        "the requesting surface is not what gets recorded")


def test_the_surface_reaches_the_gate():
    """The orchestrator hands the surface to the policy, which records it."""
    source = (REPO / "jarvis" / "core" / "orchestrator.py").read_text(encoding="utf-8")
    assert "requested_surface=" in source, (
        "the orchestrator does not tell the gate which surface raised the request")


def test_the_self_approval_guard_uses_both_surfaces():
    """Either one being narrow is enough to refuse.

    The approver's surface and the request's surface are separate facts, and
    either being wrong must not be enough to let a self-approval through.
    """
    source = (REPO / "jarvis" / "core" / "orchestrator.py").read_text(encoding="utf-8")
    assert 'record.get("requested_surface"' in source, (
        "the guard does not look at the surface the request was raised on")
    assert "approver_surface in NARROW_SURFACES" in source, (
        "the guard does not look at the approver's surface")


# ---------------------------------------------------------------------------
# The phone client's session
# ---------------------------------------------------------------------------
def test_the_phone_client_sends_its_session_with_a_request():
    """A request the app raises must carry the session that raised it.

    It used to send ``null``, and the backend minted a fresh ``SESS-...`` id
    for every request. The self-approval guard compares the session that
    raised a request with the session that answers it — and with a fresh id
    on every call, the two could never be the same.
    """
    source = (REPO / "android" / "app" / "src" / "main" / "kotlin"
              / "com" / "vantage" / "companion" / "data" / "repository"
              / "CommandRepository.kt").read_text(encoding="utf-8")
    assert "sessionId = null" not in source, (
        "the phone client still sends a null session -- the request is "
        "unattributable")
    assert "sessionId = store.sessionToken()" in source, (
        "the phone client does not send the session it holds")


# ---------------------------------------------------------------------------
# The dead header
# ---------------------------------------------------------------------------
def test_the_device_id_header_is_either_used_or_gone():
    """A header nobody reads is a header that lies.

    The gateway forwards ``X-Vantage-Device-Id`` "for audit trails" and the
    backend never reads it. Either it becomes the identity the audit records,
    or it stops being forwarded. The first is the better answer: a request
    that came from a handset should say so.
    """
    gateway = (REPO / "jarvis" / "companion" / "gateway.py").read_text(encoding="utf-8")
    backend = (REPO / "jarvis" / "api" / "app.py").read_text(encoding="utf-8")

    forwarded = "X-Vantage-Device-Id" in gateway
    read = "X-Vantage-Device-Id" in backend
    assert forwarded and read, (
        "the device id header is forwarded by the gateway but read by nobody "
        "(or forwarded by nobody and read by nobody) -- it is either a dead "
        "header or a missing feature, and it should not be both"
    )


def test_the_phone_client_sends_its_device_id():
    """The device id travels with every request, so the gateway has one to forward.

    The header existed on the gateway and in the backend's vocabulary, but no
    client ever set it, so the gateway had nothing to forward. The chain has
    to be whole: client -> gateway -> backend.
    """
    source = (REPO / "android" / "app" / "src" / "main" / "kotlin"
              / "com" / "vantage" / "companion" / "data" / "api"
              / "AuthInterceptor.kt").read_text(encoding="utf-8")
    assert "X-Vantage-Device-Id" in source, (
        "the phone client does not send its device id")
    assert "DEVICE_ID_HEADER" in source, (
        "the device id header is named but not used")
    assert "deviceId" in source, (
        "the device id header is sent, but not the device's own id")


def test_the_command_route_prefers_the_forwarded_device_id():
    """The header wins over the body's claim.

    The body field is the client's own claim about which device it is; the
    header is the one the gateway saw for itself. When both are present, the
    gateway's is the one to believe.
    """
    source = (REPO / "jarvis" / "api" / "app.py").read_text(encoding="utf-8")
    assert 'request.headers.get("X-Vantage-Device-Id"' in source, (
        "the command route does not read the forwarded device id")
