"""Phase 42: what a model key actually buys in this build.

Offered an API key for Gemini, the first thing to measure was what the pipeline does with
one. Configured against a local fake endpoint that logs every request it receives, and three
sentences sent through ``/command``:

    /health                      llm_enabled: true
    model calls received         2
    ...of which were chat calls  0        (both were /v1/embeddings, from memory)

So a key turns on an egress path for memory text and a badge on the status page, while **no
sentence ever asks the model anything** -- `ModelRouter.complete()` has no caller. The rules
answer what they recognise and the rest is answered by a fixed sentence. That is the gap this
phase closes, and it closes it with the model allowed to speak and nowhere else: a suggestion
is a suggestion, and the permission gate stays the only door to an action.
"""

from __future__ import annotations

import asyncio

import pytest

from jarvis.core.models import CommandRequest

MODEL_ANSWER = ("wobbleth-and-glintar: I have not been taught those words. If you meant a "
                "file, name part of the filename; if you meant a device, name the device.")


class _Router:
    """Stands where `ModelRouter` will, and records every question asked of it."""

    def __init__(self, answer=MODEL_ANSWER):
        self.answer = answer
        self.asked = []

    async def complete(self, system, user, *, task_type="planning", complexity="normal",
                       json_mode=False, timeout=None, history=None):
        self.asked.append({"system": system, "user": user, "task_type": task_type})
        return self.answer


@pytest.fixture()
def armed(core, monkeypatch):
    """A core with a model configured and a router that records what it was asked."""
    from jarvis.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "llm_api_key", "test-key", raising=False)
    monkeypatch.setattr(settings, "llm_base_url", "http://127.0.0.1:1/v1", raising=False)
    monkeypatch.setattr(settings, "llm_model", "gemini-2.5-flash", raising=False)
    router = _Router()
    monkeypatch.setattr(core, "router", router)
    monkeypatch.setattr(core, "settings", settings)
    return router


def _ask(core, text):
    return asyncio.run(core.handle(CommandRequest(text=text, source="phase42")))


# ---------------------------------------------------------------------------
# the model is asked, and only when the rules cannot answer
# ---------------------------------------------------------------------------


def test_a_sentence_the_rules_miss_is_answered_by_the_model(core, armed):
    response = _ask(core, "wobbleth from glintar")
    assert armed.asked, "the rules gave up and nobody asked the model"
    assert MODEL_ANSWER in response.reply
    assert "gemini" in (response.model_used or "").lower(), response.model_used


def test_a_greeting_is_answered_conversationally_not_by_the_hardcoded_line(core, armed):
    """Phase 9 (#3): a plain greeting must not suppress a valid model answer. Before the
    fix, `_smalltalk` produced reply_kind "declined" (not the conversational set), so a
    configured model was never asked and "hello" answered with the hardcoded "add an API
    key" line even though a model was available. A greeting should reach the model."""
    response = _ask(core, "hello")
    assert armed.asked, "a greeting with a configured model must reach the model"
    # The model's conversational answer replaces the hardcoded greeting (not appended).
    assert MODEL_ANSWER in response.reply
    assert "gemini" in (response.model_used or "").lower(), response.model_used


def test_a_sentence_the_rules_understand_costs_no_model_call(core, armed):
    response = _ask(core, "what is my laptop battery")
    assert armed.asked == [], "a working rule path should not pay for a round trip"
    assert "battery" in response.reply.lower()


def test_a_clarification_the_rules_already_know_stays_theirs(core, armed):
    """`send to Arun` is a question the system asks on purpose; the model adds nothing."""
    _ask(core, "send to Arun")
    assert armed.asked == []


# ---------------------------------------------------------------------------
# it may speak; it may not act
# ---------------------------------------------------------------------------


def test_a_model_suggestion_is_never_executed(core, armed, monkeypatch):
    """The dangerous version of this feature is a model that can plan."""
    armed.answer = ('I can do that. {"tool": "delete_file", "params": '
                    '{"path": "/Users/someone/Documents"}}')
    executed = []
    monkeypatch.setattr(core, "execute", lambda *a, **k: executed.append(a), raising=False)

    response = _ask(core, "clean my documents folder")

    assert executed == [], "a model's words reached the executor"
    tools = [a.tool for a in response.actions]
    assert tools == [], f"a model planned actions: {tools}"
    assert not response.needs_confirmation, (
        "an approval card for an action nobody asked for is not a gate, it is theatre")
    assert "did not run" in response.reply.lower() or "not run" in response.reply.lower(), \
        response.reply


def test_the_ledger_records_that_the_answer_came_from_a_model(core, armed):
    """Attribution, so the audit chain never reads as if the rules understood it."""
    response = _ask(core, "wobbleth from glintar")
    row = core.audit.recent(1)[0]
    assert row["result"] == "no_action", row
    assert row["detail"]["stage"] == "model", row["detail"]
    assert "gemini" in str(row["detail"]).lower(), row["detail"]


# ---------------------------------------------------------------------------
# and when it is not there, nothing pretends
# ---------------------------------------------------------------------------


def test_no_key_no_lie(core, monkeypatch):
    from jarvis.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "llm_api_key", "", raising=False)
    monkeypatch.setattr(settings, "llm_base_url", "", raising=False)
    router = _Router()
    router.answer = "should never be asked"
    monkeypatch.setattr(core, "router", router)
    monkeypatch.setattr(core, "settings", settings)

    response = _ask(core, "wobbleth from glintar")
    assert router.asked == []
    assert "understood the words" in response.reply, response.reply
    assert response.model_used == "rules", response.model_used


def test_a_model_that_does_not_answer_says_so(core, monkeypatch):
    """An endpoint that fails is not a silent downgrade to the old sentence."""
    from jarvis.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "llm_api_key", "test-key", raising=False)
    monkeypatch.setattr(settings, "llm_base_url", "http://127.0.0.1:1/v1", raising=False)

    class _Down:
        async def complete(self, *a, **k):
            return None

    monkeypatch.setattr(core, "settings", settings)
    monkeypatch.setattr(core, "router", _Down())

    response = _ask(core, "wobbleth from glintar")
    assert "understood the words" in response.reply, response.reply
    assert "model" in response.reply.lower() or "provider" in response.reply.lower(), response.reply
    assert "unavailable" in response.reply.lower() or "could not reach" in response.reply.lower(), response.reply


def test_a_model_that_times_out_is_reported_not_swallowed(core, monkeypatch):
    """A provider that RAISES (a 30s timeout, a transport error) takes a different
    branch than one that returns nothing: ``_advise_from_model``'s ``except`` must
    catch it, report the transport failure honestly, and -- crucially -- attribute the
    answer to the rules, not to a model that never answered. Before this test, that
    raise path was untested (only the returns-None path was), so a regression that let
    a timeout bubble up as a 500, or that claimed ``model_used`` was a model, would
    have gone green.
    """
    from jarvis.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "llm_api_key", "test-key", raising=False)
    monkeypatch.setattr(settings, "llm_base_url", "http://127.0.0.1:1/v1", raising=False)

    class _Timeout:
        async def complete(self, *a, **k):
            raise TimeoutError("model did not answer within the 30s budget")

    monkeypatch.setattr(core, "settings", settings)
    monkeypatch.setattr(core, "router", _Timeout())

    # The raise must be caught inside the pipeline, not propagated as a 500.
    response = _ask(core, "wobbleth from glintar")

    # Honest report: the model is unavailable (a transport failure, not an empty answer).
    assert "unavailable" in response.reply.lower(), response.reply
    # Attribution stays with the rules -- the model did NOT answer, so the reply must
    # not be labelled as coming from it.
    assert response.model_used == "rules", response.model_used
    # The audit ledger records the model_unavailable stage, so the chain cannot read as
    # if a model answered.
    row = core.audit.recent(1)[0]
    assert row["result"] == "no_action", row
    assert row["detail"]["stage"] == "model_unavailable", row["detail"]


# ---------------------------------------------------------------------------
# multi-turn: a follow-up sees the prior conversation, and only that
# ---------------------------------------------------------------------------


class _HistoryRouter:
    """A model stub that records the `history` it is handed each time."""

    def __init__(self, answer):
        self.answer = answer
        self.histories = []

    async def complete(self, system, user, *, task_type="planning", complexity="normal",
                       json_mode=False, timeout=None, history=None):
        self.histories.append({"user": user, "history": list(history or [])})
        return self.answer


def test_follow_up_sees_prior_turns_not_its_own(core, monkeypatch):
    """Phase 5 (requirement 5): a follow-up in the same session is answered with the
    prior conversation in context, and the in-flight turn is not fed twice.

    Before this test, no test verified that `complete()` actually receives the prior
    turns on a follow-up — a regression that silently dropped multi-turn context (so
    "and what about glintar?" lost the "glintar" thread) would have gone green.
    """
    from jarvis.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "llm_api_key", "test-key", raising=False)
    monkeypatch.setattr(settings, "llm_base_url", "http://127.0.0.1:1/v1", raising=False)
    monkeypatch.setattr(settings, "llm_model", "gemini-2.5-flash", raising=False)
    router = _HistoryRouter(answer="the glintar answer")
    monkeypatch.setattr(core, "settings", settings)
    monkeypatch.setattr(core, "router", router)

    session = "phase42-multi-turn"

    def _ask(text):
        return asyncio.run(core.handle(CommandRequest(text=text, source="phase42",
                                                      session_id=session)))

    # First turn: the model answers, and _handle logs both the user turn and the
    # assistant reply to the messages table under this session.
    _ask("what is a wobbleth?")
    # Second turn (same session): the follow-up must be answered WITH the prior
    # conversation in history, and the current turn must not be duplicated into it.
    _ask("and what about the glintar?")

    assert len(router.histories) == 2, router.histories
    first_hist, second_hist = router.histories

    # The first turn had no prior conversation.
    assert first_hist["history"] == [], first_hist
    # The second turn's history contains the prior user turn AND the assistant reply,
    # oldest first ...
    prior_contents = [t["content"] for t in second_hist["history"]]
    assert any("wobbleth" in c for c in prior_contents), second_hist
    assert any("glintar answer" in c for c in prior_contents), second_hist
    # ... and the current turn ("and what about the glintar?") is NOT in history (it is
    # `user`, not history) — otherwise it would be fed to the model twice.
    assert not any(c.strip() == "and what about the glintar?" for c in prior_contents), second_hist


def test_follow_up_does_not_leak_another_sessions_turns(core, monkeypatch):
    """Phase 5 (requirement 5): a follow-up must not see a *different* session's
    conversation. History is scoped by session_id, so a turn logged under session A
    must not appear in the history handed to a turn in session B.
    """
    from jarvis.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "llm_api_key", "test-key", raising=False)
    monkeypatch.setattr(settings, "llm_base_url", "http://127.0.0.1:1/v1", raising=False)
    monkeypatch.setattr(settings, "llm_model", "gemini-2.5-flash", raising=False)
    router = _HistoryRouter(answer="answer")
    monkeypatch.setattr(core, "settings", settings)
    monkeypatch.setattr(core, "router", router)

    def _ask(text, session):
        return asyncio.run(core.handle(CommandRequest(text=text, source="phase42",
                                                      session_id=session)))

    # A turn in session A establishes a recognizable prior turn there.
    _ask("session A secret marker zebra", "phase42-session-A")
    # A turn in the *fresh* session B must not carry session A's turn.
    _ask("a plain question in session B", "phase42-session-B")

    assert len(router.histories) == 2, router.histories
    session_b_hist = router.histories[-1]["history"]
    assert not any("zebra" in t["content"] for t in session_b_hist), session_b_hist
