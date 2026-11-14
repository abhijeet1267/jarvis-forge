"""The macOS Command Center is a conversation, not a queue of one-shot questions.

The app's Command Center is the surface the owner actually talks to, and it
was not conversational. Three defects, none of which a build or a view test
would have caught:

1. **The request carried no session.** `CommandRequest.init` hardcoded
   `session_id = nil`. The backend mints a fresh `SESS-` id for every request
   that arrives without one, so **every message started a new conversation**:
   the second message could not see the first, and a follow-up like "explain
   the second part" had nothing to refer to. The backend threads history per
   session — the app simply never used the mechanism that exists.

2. **The response model read fields the server does not send.** The struct
   declared `status`, `message`, `confirmation_required` and `elapsed_ms`.
   The server sends `reply`, `decision`, `needs_confirmation`, `duration_ms`,
   `model_used`, `intent` and `session_id`. Because the wrong ones were all
   optional, **decoding succeeded** and the app silently showed less than the
   backend had said: no model attribution, no timing, and a pending approval
   that was announced only by its own absence.

3. **Nothing distinguished who answered.** A rules answer and a model answer
   looked identical, so "Vantage answered from the model" was not something
   the app could honestly show.

These tests pin the contract from the **server** side — the shape the app
decodes is the shape the backend actually sends, checked against a real
response — and check the Swift source for the behaviour the server cannot
prove. The Swift unit tests (`macos/Vantage/Tests/VantageTests/ConversationTests.swift`)
pin the same contract from the app's side, but **XCTest cannot run in this
environment** (only the Command Line Tools are installed: `no such module
'XCTest'`), so these are the ones that actually execute here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
MACOS = REPO / "macos" / "Vantage"


def _swift(*parts: str) -> str:
    return MACOS.joinpath("Sources", *parts).read_text(encoding="utf-8")


def _swift_test(*parts: str) -> str:
    return MACOS.joinpath("Tests", "VantageTests", *parts).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The contract, server side
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from jarvis.api.app import app
    return TestClient(app, base_url="http://127.0.0.1:8765")


def test_command_sends_the_fields_the_app_decodes(client):
    """Every field the app's model declares is one the server actually sends.

    The app's `CommandResponse` declares `reply`, `decision`,
    `needs_confirmation`, `model_used`, `intent`, `duration_ms` and
    `session_id`. Each one has to be a real field of the server's response —
    a field the server never sends decodes as nil and the app shows less
    than it was told.
    """
    response = client.post(
        "/command",
        json={"text": "what is a solar panel", "source": "native"},
        headers={"X-JARVIS-Token": _master_token()},
    )
    assert response.status_code == 200, response.text[:300]
    body = response.json()

    for field in ("reply", "decision", "needs_confirmation", "model_used",
                  "intent", "duration_ms", "session_id"):
        assert field in body, (
            f"the server's /command response has no `{field}`, but the macOS "
            f"app's model declares it; it would always decode as nil")
    # The reply is the answer, and it is never empty.
    assert isinstance(body["reply"], str) and body["reply"], (
        "the server answered with no reply text")
    assert isinstance(body["duration_ms"], int)
    assert isinstance(body["needs_confirmation"], bool)


def test_the_server_echoes_the_session_it_used(client):
    """The app adopts the server's session so the next turn joins the thread."""
    response = client.post(
        "/command",
        json={"text": "hello", "source": "native"},
        headers={"X-JARVIS-Token": _master_token()},
    )
    assert response.status_code == 200, response.text[:200]
    body = response.json()
    assert body["session_id"], (
        "the server did not say which session the turn belongs to; the app "
        "cannot continue a conversation without it")


def test_a_request_that_names_a_session_keeps_that_session(client):
    """The session in the request is the session the turn is filed under.

    This is what makes a follow-up question resolve. If the server minted a
    fresh id for every request, the app's session would be cosmetic and the
    conversation would not exist.
    """
    response = client.post(
        "/command",
        json={"text": "hello", "session_id": "SESS-app-test-1", "source": "native"},
        headers={"X-JARVIS-Token": _master_token()},
    )
    assert response.status_code == 200, response.text[:200]
    body = response.json()
    assert body["session_id"] == "SESS-app-test-1", (
        "the server did not honour the session the app sent, so the app's "
        "conversation cannot hold")


def _master_token() -> str:
    import os
    return os.environ["JARVIS_API_TOKEN"]


# ---------------------------------------------------------------------------
# The Swift source
# ---------------------------------------------------------------------------
def test_the_app_sends_a_session_with_its_request():
    """The request must carry a session id.

    It was hardcoded to `nil`, which made every message a brand new
    conversation on the server's side.
    """
    source = _swift("APIClient", "APIModels.swift")
    request = re.search(r"struct CommandRequest.*?\n    \}", source, re.S)
    assert request, "the CommandRequest struct is gone"
    body = request.group(0)
    assert "session_id" in body, "the request no longer carries a session id"
    assert "self.session_id = nil" not in body, (
        "the request still hardcodes a nil session; every message starts a "
        "new conversation and a follow-up has nothing to refer to")
    assert "sessionId" in body, (
        "the request does not take a session from its caller")


def test_the_app_reads_the_servers_own_fields():
    """The response model must declare the fields the server sends.

    The old model declared `status`, `message`, `confirmation_required` and
    `elapsed_ms` — none of which the server sends.
    """
    source = _swift("APIClient", "APIModels.swift")
    response = re.search(r"struct CommandResponse.*?\n    \}", source, re.S)
    assert response, "the CommandResponse struct is gone"
    body = response.group(0)
    for field in ("reply", "decision", "needs_confirmation", "model_used",
                  "duration_ms", "session_id"):
        assert field in body, (
            f"the app's response model does not declare `{field}`, so it can "
            f"never show it")


def test_the_conversation_is_its_own_service_not_the_view():
    """The session and the transcript belong to a service, not to the view.

    A view that owns the conversation loses it on every rebuild, and cannot
    be tested without a window.
    """
    service = MACOS / "Sources" / "Services" / "ConversationService.swift"
    assert service.exists(), (
        "there is no conversation service; the view is holding the session "
        "and the transcript itself")
    text = service.read_text(encoding="utf-8")
    assert "/command" in text, (
        "the conversation service does not talk to /command")
    assert "sessionId:" in text, (
        "the conversation service does not send the session with the request")


def test_the_view_does_not_hardcode_its_own_session():
    """The view must not mint a session id of its own.

    The backend mints the session and echoes it back; a client-side id would
    be a second, disagreeing source of truth about which conversation a turn
    belongs to.
    """
    view = _swift("Views", "CommandCenterView.swift")
    assert "SESS-" not in view, (
        "the view is minting its own session id; the server's id is the one "
        "that scopes the conversation")
    assert "CommandRequest(text:" in view or "chat.send(" in view


def test_the_transcript_shows_who_answered():
    """The caption must be able to say which model answered.

    A rules answer and a model answer have to be distinguishable, or "Vantage
    answered from the model" is not something the app can honestly claim.
    """
    view = _swift("Views", "CommandCenterView.swift")
    assert "model" in view.lower(), (
        "the transcript has no place to show which model answered")
    service = MACOS / "Sources" / "Services" / "ConversationService.swift"
    text = service.read_text(encoding="utf-8")
    assert "model_used" in text, (
        "the conversation service does not read the server's `model_used`")


def test_a_new_conversation_clears_the_session_too():
    """Starting a new conversation must not carry the old session.

    Clearing only the transcript would leave the session id in place, and the
    next message would be threaded onto the conversation that just ended.
    """
    conversation = MACOS / "Sources" / "Services" / "Conversation.swift"
    assert conversation.exists(), (
        "the conversation model is missing; the view is holding it")
    text = conversation.read_text(encoding="utf-8")
    reset = re.search(r"func startNewConversation\(\).*?\n    \}", text, re.S)
    assert reset, "there is no way to start a new conversation"
    body = reset.group(0)
    assert "sessionID = nil" in body, (
        "a new conversation keeps the old session id, so the next message "
        "continues the conversation that just ended")


def test_the_composer_can_be_submitted_from_the_keyboard():
    """Return sends. A composer that needs the mouse is a half-feature.

    Checked against the code, not the prose: the file's own comments mention
    ``.onSubmit`` and ``keyboardShortcut(.return``, so a plain substring
    search would pass against a file that had neither.
    """
    view = _swift("Views", "CommandCenterView.swift")
    code = "\n".join(
        line for line in view.splitlines()
        if not line.strip().startswith("//")
    )
    assert ".onSubmit" in code, (
        "the composer cannot be submitted from the keyboard")
    assert "keyboardShortcut(.return" in code, (
        "the send button has no Return shortcut")


def test_the_transcript_scrolls_to_a_new_message():
    """A new message has to come into view."""
    view = _swift("Views", "CommandCenterView.swift")
    assert "scrollTo" in view, (
        "the transcript does not scroll to a new message")


def test_a_failure_is_shown_and_can_be_retried():
    """A failed turn is visible, and the person can try again.

    The old code showed the error as a message but offered nothing to do
    about it, and re-typing a long question to retry is not a retry.
    """
    service = MACOS / "Sources" / "Services" / "ConversationService.swift"
    text = service.read_text(encoding="utf-8")
    assert "func retry()" in text, (
        "there is no way to retry a failed turn")
    assert "lastPrompt" in text, (
        "the failed turn is not kept, so a retry would need the question retyped")
    view = _swift("Views", "CommandCenterView.swift")
    assert "lastError" in view or "errorMessage" in view, (
        "a failure is never shown")


def test_the_transcript_has_an_empty_state():
    """An empty transcript must explain what the assistant can do.

    Checked against the *use*, not the definition: a struct that is declared
    but never shown is not an empty state.
    """
    view = _swift("Views", "CommandCenterView.swift")
    code = "\n".join(
        line for line in view.splitlines()
        if not line.strip().startswith("//")
    )
    # Matched on the call, not on a fixed argument list: the empty state now
    # takes the provider's state so it can stop promising a conversational
    # answer when none is configured. What must not change is that it is used.
    assert re.search(r"EmptyConversationState\s*\(", code), (
        "the transcript has no empty state; a blank screen reads as broken")
    # And it must say something useful, not just be a shape.
    body = re.search(r"struct EmptyConversationState: View \{.*?\n\}", view, re.S)
    assert body, "the empty state struct is gone"
    assert "Ask" in body.group(0) or "Vantage" in body.group(0), (
        "the empty state says nothing about what the assistant can do")


# ---------------------------------------------------------------------------
# The app must know whether the AI path is answering
# ---------------------------------------------------------------------------
def test_the_app_reads_the_providers_state_from_health():
    """The app has to know whether a model is answering.

    With no provider configured the backend falls back to a Wikipedia lookup,
    and for a question like "explain how solar panels generate electricity"
    that lookup returns the "Solar power" article — UK generation statistics,
    which is a *different subject* from the one that was asked about. The
    answer looks exactly like an answer. The app can only tell the difference
    if it reads the provider's state.
    """
    service = MACOS / "Sources" / "Services" / "ConversationService.swift"
    text = service.read_text(encoding="utf-8")
    assert "/health" in text, (
        "the conversation service never reads /health, so it cannot know "
        "whether a provider is configured")
    assert "llm_enabled" in text, (
        "the conversation service does not read the backend's llm_enabled "
        "flag")
    assert "aiAvailable" in text, (
        "the conversation service does not track whether the AI path is up")


def test_the_view_warns_when_the_ai_path_is_not_answering():
    """The notice has to be on screen, and it has to be conditional.

    A permanent warning is noise; no warning at all is a lie.
    """
    view = _swift("Views", "CommandCenterView.swift")
    assert "aiAvailable" in view, (
        "the Command Center does not consider whether the AI path is up")
    # The notice was a bespoke `ProviderNotice`; the redesign expresses it with
    # the shared `Notice` component, which carries a warning glyph, the reason
    # and a "Check again" action. What must remain true is that *some* notice is
    # drawn and that it is conditional on the AI path being unavailable.
    code = "\n".join(
        line for line in view.splitlines()
        if not line.strip().startswith("//")
    )
    assert "Notice(" in code and "aiUnavailableReason" in code, (
        "the Command Center has no notice for an unavailable AI path")
    assert "if !chat.aiAvailable" in code, (
        "the provider notice is not conditional; it must appear only when the "
        "AI path is actually unavailable")


def test_a_failed_turn_can_actually_be_retried():
    """`retry()` existed but nothing called it.

    The service had the method and the view never used it, so the claim that
    a failed turn could be retried was true of the code and false of the
    product. The button has to be on screen and wired to it.
    """
    view = _swift("Views", "CommandCenterView.swift")
    assert "chat.retry()" in view, (
        "nothing in the Command Center calls the retry path, so a failed turn "
        "cannot be retried")
    # The retry affordance was a bespoke `RetryRow`; it is now a `Notice` whose
    # action is "Retry". The product property — a visible control wired to the
    # retry path, offered only when a failed turn exists — is what is pinned.
    assert '"Retry"' in view, "the Command Center has no visible retry control"
    assert "chat.lastError" in view, (
        "retry is not conditioned on a failed turn; it would offer itself always")
    service = MACOS / "Sources" / "Services" / "ConversationService.swift"
    text = service.read_text(encoding="utf-8")
    assert "func retry()" in text, (
        "the conversation service lost its retry path")


def test_a_lookup_answer_is_not_presented_as_a_model_answer():
    """The server's `model_used` is "rules" for a lookup answer.

    A lookup can return an article about a different subject, so the app must
    not show it as if a model had answered.
    """
    conversation = MACOS / "Sources" / "Services" / "Conversation.swift"
    text = conversation.read_text(encoding="utf-8")
    assert "isModelAnswer" in text, (
        "the message metadata cannot tell a model answer from a lookup answer")
    assert '"rules"' in text, (
        "the model-answer check does not recognise the server's 'rules' label")
    service = MACOS / "Sources" / "Services" / "ConversationService.swift"
    stext = service.read_text(encoding="utf-8")
    assert "isModelAnswer" in stext, (
        "the conversation service does not use the model-answer check when it "
        "labels an answer")
