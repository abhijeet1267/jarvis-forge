"""R2.2: one conversation model, shared by every chat surface.

Vantage had two chat surfaces and they shared nothing:

* ``/command`` -- the full pipeline, with its conversation kept in the
  ``messages`` table, keyed by ``session_id``;
* ``/ai/chat/send`` -- a direct provider call, with its conversation kept in
  ``ai_messages``, keyed by ``conversation_id``.

Two stores, two schemas, two history loaders. A follow-up that resolved in one
surface did not resolve in the other, and the second surface could not act on
anything at all.

These tests pin the shared model:

* one service reads both histories, oldest first, de-duplicated;
* a turn is written to whichever store the surface uses, and to both when the
  caller supplies both identifiers;
* the chat surface can now propose and run an action -- through the *same*
  permission gate the pipeline uses;
* a chat proposal that needs approval waits, and does not run;
* both endpoints still work, and their existing response shapes are unchanged.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

import pytest

from jarvis.core.conversation import ConversationService
from jarvis.core.models import CommandRequest, PolicyDecision


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
class _Router:
    """A stub router that answers by kind.

    The conversational call and the planning call are different questions, so
    they get different answers. A single ordered list would hand the
    conversational answer to the planner, which is exactly the kind of
    cross-wiring this test file exists to rule out.
    """

    def __init__(self, answers: Optional[List[Any]] = None, plans: Optional[List[Any]] = None):
        self.answers = list(answers or [])
        self.plans = list(plans or [])
        self.asked: List[Dict[str, Any]] = []
        self.json_asked: List[Dict[str, Any]] = []

    async def complete(self, system, user, *, task_type="conversation", complexity="normal",
                       json_mode=False, timeout=None, history=None):
        self.asked.append({"system": system, "user": user, "history": history})
        if not self.answers:
            return None
        answer = self.answers.pop(0)
        return answer if isinstance(answer, str) else json.dumps(answer)

    async def complete_json(self, system, user, *, task_type="planning", complexity="normal",
                            timeout=30.0, history=None):
        self.json_asked.append({"system": system, "user": user, "history": history})
        if not self.plans:
            return None
        answer = self.plans.pop(0)
        if isinstance(answer, dict):
            return answer
        try:
            return json.loads(answer)
        except (json.JSONDecodeError, TypeError):
            return None


@pytest.fixture()
def service():
    """A conversation service over the test database."""
    from jarvis.db.database import get_db
    return ConversationService(get_db())


@pytest.fixture()
def client():
    """A test client over the real API app."""
    from fastapi.testclient import TestClient
    from jarvis.api.app import app
    return TestClient(app, base_url="http://127.0.0.1:8765")


@pytest.fixture()
def armed(core, monkeypatch):
    from jarvis.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "llm_api_key", "test-key", raising=False)
    monkeypatch.setattr(settings, "llm_base_url", "http://127.0.0.1:1/v1", raising=False)
    monkeypatch.setattr(settings, "llm_model", "test-model", raising=False)
    router = _Router()
    monkeypatch.setattr(core, "router", router)
    monkeypatch.setattr(core, "settings", settings)
    return router


# ---------------------------------------------------------------------------
# One service, both histories
# ---------------------------------------------------------------------------
def test_a_turn_written_to_the_session_store_is_read_back(service):
    service.append("user", "what is my battery", session_id="S1")
    service.append("assistant", "It is at 74 percent.", session_id="S1")
    history = service.history(session_id="S1")
    assert [t["role"] for t in history] == ["user", "assistant"]
    assert history[0]["content"] == "what is my battery"


def test_a_turn_written_to_the_chat_store_is_read_back(service):
    service.append("user", "hello there", conversation_id="C1")
    service.append("assistant", "Hello!", conversation_id="C1")
    history = service.history(conversation_id="C1")
    assert [t["role"] for t in history] == ["user", "assistant"]


def test_a_turn_written_with_both_identifiers_lands_in_both_stores(service):
    service.append("user", "shared turn", session_id="S2", conversation_id="C2")
    assert service.history(session_id="S2"), "the session store should have the turn"
    assert service.history(conversation_id="C2"), "the chat store should have the turn"


def test_the_two_histories_are_merged_into_one_conversation(service):
    """A conversation that crossed surfaces is one conversation."""
    service.append("user", "first I asked on the app", session_id="S3")
    service.append("assistant", "and I answered there", session_id="S3")
    service.append("user", "then I asked in the dashboard", conversation_id="C3")
    service.append("assistant", "and I answered here", conversation_id="C3")

    history = service.history(session_id="S3", conversation_id="C3")
    contents = [t["content"] for t in history]
    assert len(contents) == 4, contents
    assert "first I asked on the app" in contents
    assert "then I asked in the dashboard" in contents


def test_a_turn_present_in_both_stores_is_not_sent_twice(service):
    """The same turn written to both stores is one turn to the model."""
    service.append("user", "the same sentence", session_id="S4", conversation_id="C4")
    history = service.history(session_id="S4", conversation_id="C4")
    assert len(history) == 1, history


def test_the_in_flight_turn_is_not_sent_twice(service):
    """The turn being sent now is dropped from the history."""
    service.append("user", "the question", session_id="S5")
    history = service.history(session_id="S5", current_text="the question")
    assert history == [], history


def test_the_history_is_bounded(service):
    """A long conversation never inflates the prompt."""
    for index in range(30):
        service.append("user", f"turn {index}", session_id="S6")
    history = service.history(session_id="S6", limit=5)
    assert len(history) == 5
    assert history[-1]["content"] == "turn 29", "the newest turns are the ones kept"


def test_an_empty_conversation_is_an_empty_history(service):
    assert service.history(session_id="nothing-here") == []
    assert service.history() == []


def test_an_error_turn_is_not_sent_to_the_model(service):
    """A failed call is not something to say back to the model as if the person had."""
    service.append("error", "the provider timed out", conversation_id="C7")
    service.append("user", "try again", conversation_id="C7")
    history = service.history(conversation_id="C7")
    assert [t["role"] for t in history] == ["user"]


def test_an_empty_turn_is_not_recorded(service):
    service.append("user", "   ", session_id="S8")
    assert service.history(session_id="S8") == []


# ---------------------------------------------------------------------------
# The pipeline surface uses the shared service
# ---------------------------------------------------------------------------
def test_the_pipeline_reads_its_history_through_the_shared_service(core, armed):
    """The orchestrator's history read is the service's, not a second copy."""
    core.conversations.append("user", "an earlier question", session_id="S9")
    core.conversations.append("assistant", "an earlier answer", session_id="S9")

    history = core._recent_history("S9", "a new question")
    assert [t["content"] for t in history] == ["an earlier question", "an earlier answer"]


def test_a_follow_up_in_the_pipeline_sees_the_prior_turns(core, armed):
    """The follow-up resolves against what was just said."""
    core.conversations.append("user", "what is my battery", session_id="S10")
    core.conversations.append("assistant", "It is at 74 percent.", session_id="S10")

    armed.answers.append("It is still at 74 percent.")
    response = asyncio.run(core.handle(
        CommandRequest(text="and now?", source="r22", session_id="S10")
    ))
    assert armed.asked, "the model was never asked"
    sent = armed.asked[0]["history"]
    assert sent, "the follow-up saw no prior turns"
    assert any("battery" in turn["content"] for turn in sent)


def test_two_sessions_do_not_leak_into_each_other(core, armed):
    """One session's conversation is not another's."""
    core.conversations.append("user", "a private question about the car", session_id="S11")
    core.conversations.append("assistant", "a private answer", session_id="S11")

    armed.answers.append("A different answer.")
    asyncio.run(core.handle(
        CommandRequest(text="and now?", source="r22", session_id="S12")
    ))
    sent = armed.asked[0]["history"]
    assert not any("car" in turn["content"] for turn in sent), sent


# ---------------------------------------------------------------------------
# The chat surface can act -- through the same gate
# ---------------------------------------------------------------------------
def test_the_chat_surface_can_propose_and_run_a_read_action(core, armed):
    """A chat-surface proposal runs through the real gate and really runs."""
    armed.answers.append("Let me check that for you.")
    armed.plans.append(
        {"kind": "action", "tool": "get_battery_status", "params": {}}
    )
    outcome = asyncio.run(core.propose_and_execute(
        CommandRequest(text="wobbleth from glintar", source="ai_chat", session_id="S13")
    ))
    assert outcome.plan is not None
    assert outcome.plan.actions
    assert outcome.actions, "the proposed action did not run"
    assert outcome.actions[0].tool == "get_battery_status"
    assert outcome.actions[0].ok


def test_a_chat_proposal_that_needs_approval_waits_and_does_not_run(core, armed):
    """The gate is the same gate. A risky chat proposal is not a grant."""
    armed.answers.append("Let me create that.")
    armed.plans.append(
        {"kind": "action", "tool": "create_event",
         "params": {"title": "Standup", "start": "2026-01-02 09:00"}}
    )
    outcome = asyncio.run(core.propose_and_execute(
        CommandRequest(text="wobbleth from glintar", source="ai_chat", session_id="S14")
    ))
    assert outcome.decision is PolicyDecision.CONFIRM, outcome.decision
    assert outcome.needs_confirmation is True
    assert outcome.confirmation_id
    assert outcome.actions == [], "nothing may run before the approval"


def test_a_chat_proposal_that_is_refused_runs_nothing(core, armed):
    """A proposal the model is not allowed to originate produces no action."""
    armed.answers.append("Sure.")
    armed.plans.append(
        {"kind": "action", "tool": "delete_file",
         "params": {"path": "/tmp/whatever", "confirm": True}}
    )
    outcome = asyncio.run(core.propose_and_execute(
        CommandRequest(text="wobbleth from glintar", source="ai_chat", session_id="S15")
    ))
    assert outcome.actions == [], "a destructive proposal must not run"
    if outcome.plan is not None:
        assert outcome.plan.actions == []


def test_a_chat_proposal_is_recorded_in_the_conversation(core, armed):
    """What was done is part of the conversation, on the surface that did it."""
    armed.answers.append("Let me check that for you.")
    armed.plans.append(
        {"kind": "action", "tool": "get_battery_status", "params": {}}
    )
    asyncio.run(core.propose_and_execute(
        CommandRequest(text="wobbleth from glintar", source="ai_chat", session_id="S16")
    ))
    history = core.conversations.history(session_id="S16")
    assert any("wobbleth" in turn["content"] for turn in history), history


# ---------------------------------------------------------------------------
# The API contract of the chat surface
# ---------------------------------------------------------------------------
def test_the_chat_endpoint_keeps_its_response_shape_and_adds_an_action_field(client):
    """The existing fields are unchanged; the action is an addition.

    A client that reads `ok`, `response`, `provider` and `model` must not
    break. The new field is additive, and it is empty when nothing was
    proposed -- never a fabricated action.
    """
    import sys
    api = sys.modules["jarvis.api.app"]

    class _Fake:
        name = "openai"

        async def chat(self, system, user, *, model=None, history=None, timeout=None):
            from jarvis.gateway.providers import ProviderReply
            return ProviderReply(True, "openai", model or "gpt-4o-mini", text="hello there")

    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(api, "select_provider", lambda s: _Fake())
        monkey.setattr(api.KeychainStorage, "retrieve", staticmethod(lambda pid: "sk-test"))
        resp = client.post("/ai/chat/send", json={"text": "hi"},
                            headers={"X-JARVIS-Token": "test-token-do-not-use-in-production",
                                     "Host": "127.0.0.1"})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["ok"] is True
        assert data["response"] == "hello there"
        assert data["provider"] == "openai"
        # The action field is present and empty -- not absent, and not a
        # fabricated proposal.
        assert data["action"] == {}
    finally:
        monkey.undo()


def test_the_chat_endpoint_reports_a_proposal_without_claiming_it_ran(client, monkeypatch):
    """A proposal is reported as a proposal.

    The chat surface can now act, but a proposal that has not run must say so.
    """
    import sys
    api = sys.modules["jarvis.api.app"]

    class _Fake:
        name = "openai"

        async def chat(self, system, user, *, model=None, history=None, timeout=None):
            from jarvis.gateway.providers import ProviderReply
            return ProviderReply(True, "openai", model or "gpt-4o-mini", text="sure")

    monkeypatch.setattr(api, "select_provider", lambda s: _Fake())
    monkeypatch.setattr(api.KeychainStorage, "retrieve", staticmethod(lambda pid: "sk-test"))

    core = api.get_core()

    class _Router:
        async def complete(self, *a, **k):
            return "sure"

        async def complete_json(self, *a, **k):
            return {"kind": "action", "tool": "create_event",
                    "params": {"title": "Standup", "start": "2026-01-02 09:00"}}

    monkeypatch.setattr(core, "router", _Router())

    resp = client.post("/ai/chat/send",
                        json={"text": "wobbleth from glintar", "session_id": "SCHAT1"},
                        headers={"X-JARVIS-Token": "test-token-do-not-use-in-production",
                                 "Host": "127.0.0.1"})
    assert resp.status_code == 200, resp.text
    action = resp.json()["action"]
    assert action["tool"] == "create_event"
    # The model *proposed* it. Whether it ran, and whether it is waiting for
    # approval, are separate facts and both are reported.
    assert action["needs_confirmation"] is True
    assert action["executed"] is False
    assert action["results"] == []


# ---------------------------------------------------------------------------
# R2.2b: a pipeline session is listable, and resumes, from the chat surface
#
# The macOS app talks to `/command`, which writes `messages` keyed by its
# session id. Nothing listed those sessions: `GET /ai/chats` read `ai_chats`
# only, so the app's conversations existed on the server and were unreachable
# from every client that lists conversations -- including the app's own
# History panel after a reload.
# ---------------------------------------------------------------------------

TOKEN = {"X-JARVIS-Token": "test-token-do-not-use-in-production", "Host": "127.0.0.1"}


def _record_pipeline_turn(core, session_id: str, question: str, answer: str) -> None:
    """Write one turn the way `/command` does."""
    core._log_message(session_id, "user", question)
    core._log_message(session_id, "assistant", answer)


def test_a_pipeline_session_appears_in_the_conversation_list(client, core):
    """The app's conversation is listable, named after what the person asked."""
    _record_pipeline_turn(core, "S-APP-1", "what is on my calendar", "Nothing today.")

    listed = client.get("/ai/chats", headers=TOKEN).json()
    entry = next((c for c in listed if c["id"] == "S-APP-1"), None)

    assert entry is not None, f"the pipeline session is not listed: {listed}"
    assert entry["title"] == "what is on my calendar", entry["title"]
    assert entry["surface"] == "pipeline"
    assert entry["message_count"] == 2


def test_a_listed_pipeline_session_resumes_with_its_own_turns(client, core):
    """Opening it returns both turns, so the History panel is not empty."""
    _record_pipeline_turn(core, "S-APP-2", "remember the milk", "Noted.")

    messages = client.get("/ai/chats/S-APP-2/messages", headers=TOKEN).json()

    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "remember the milk"
    assert messages[1]["content"] == "Noted."


def test_a_conversation_written_by_both_surfaces_is_one_entry(client, core):
    """Continuing a session from the chat surface must not fork it."""
    _record_pipeline_turn(core, "S-BOTH", "a question", "an answer")
    # The chat surface appends to the same id.
    client.post("/ai/chats/S-BOTH/messages",
                json={"role": "user", "content": "a follow-up"}, headers=TOKEN)

    listed = client.get("/ai/chats", headers=TOKEN).json()
    matches = [c for c in listed if c["id"] == "S-BOTH"]

    assert len(matches) == 1, f"the conversation forked: {matches}"
    assert matches[0]["surface"] == "both"
    # Two pipeline turns plus the one chat turn: a person counts all three.
    assert matches[0]["message_count"] == 3


def test_registering_a_session_keeps_the_first_title(core):
    """The name a conversation gets first is the one a reader recognises."""
    core.conversations.register_session("S-TITLE", opening_text="the first question")
    core.conversations.register_session("S-TITLE", opening_text="a later message")

    listed = [c for c in core.conversations.list_conversations()
              if c["id"] == "S-TITLE"]
    assert listed[0]["title"] == "the first question"


def test_an_empty_session_id_is_not_listed(client, core):
    """A blank session is not a conversation, and must not become one."""
    core.conversations.register_session("", opening_text="orphan")
    core.conversations.register_session("   ", opening_text="orphan")

    listed = client.get("/ai/chats", headers=TOKEN).json()
    assert all(c["id"].strip() for c in listed), listed


def test_deleting_a_pipeline_conversation_removes_its_turns(client, core):
    """Delete has to reach the store the conversation actually lives in.

    Deleting only the `ai_chats`/`ai_messages` rows left the pipeline's turns
    in `messages`, and the listing -- built from `messages` -- returned the
    conversation again on the next read. A delete that reports success and
    changes nothing visible is a false success.
    """
    _record_pipeline_turn(core, "S-DEL", "delete me", "deleting")

    resp = client.delete("/ai/chats/S-DEL", headers=TOKEN)
    assert resp.status_code == 200 and resp.json()["deleted"] is True

    listed = client.get("/ai/chats", headers=TOKEN).json()
    assert not [c for c in listed if c["id"] == "S-DEL"], (
        f"the conversation came back after deletion: {listed}")

    messages = client.get("/ai/chats/S-DEL/messages", headers=TOKEN).json()
    assert messages == [], f"its turns survived: {messages}"
