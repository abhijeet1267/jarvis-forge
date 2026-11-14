"""R2.1: the model actually receives conversation history.

Before this, neither chat path threaded prior turns to the provider — a
follow-up question could not resolve, and the `conversation_id` the dashboard
already sends was ignored. These tests pin the contract at both layers:

1. payload construction — the provider adapters emit prior turns in their own
   wire shape (OpenAI-compatible message list; Gemini user/model alternation);
2. the endpoint — `/ai/chat/send` loads the conversation's prior turns from
   `ai_messages` and hands them to the provider, without duplicating the
   in-flight user message.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

from jarvis.gateway.providers import (
    GeminiProvider,
    OpenAIProvider,
    ProviderReply,
)
from tests.conftest import AUTH_HEADERS, TEST_TOKEN


@pytest.fixture
def client() -> TestClient:
    from jarvis.api.app import app
    return TestClient(app, base_url="http://127.0.0.1:8765")


# ---------------------------------------------------------------------------
# 1. payload construction
# ---------------------------------------------------------------------------
def test_openai_payload_carries_history_in_order():
    p = OpenAIProvider(None)
    history = [
        {"role": "user", "content": "what is my first task?"},
        {"role": "assistant", "content": "Your first task is the report."},
        {"role": "user", "content": "and the second?"},
        {"role": "assistant", "content": "The second is review."},
    ]
    _, payload = p._chat_request("sys", "and the third?", "gpt-4o", False, history)
    roles = [m["role"] for m in payload["messages"]]
    assert roles == ["system", "user", "assistant", "user", "assistant", "user"]
    assert payload["messages"][0]["content"] == "sys"
    assert payload["messages"][-1] == {"role": "user", "content": "and the third?"}
    # no turn lost, none reordered
    contents = [m["content"] for m in payload["messages"][1:]]
    assert contents == [m["content"] for m in history] + ["and the third?"]


def test_openai_payload_skips_invalid_history_turns():
    p = OpenAIProvider(None)
    history = [
        {"role": "system", "content": "injected"},   # not a conversation turn
        {"role": "user", "content": "   "},          # blank
        {"role": "assistant", "content": "ok"},
    ]
    _, payload = p._chat_request("", "hi", "m", False, history)
    roles = [m["role"] for m in payload["messages"]]
    assert roles == ["assistant", "user"]


def test_openai_payload_single_turn_unchanged_when_no_history():
    """Existing behavior is preserved: no history -> exactly system+user."""
    p = OpenAIProvider(None)
    _, payload = p._chat_request("sys", "hi", "m", False, ())
    assert payload["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ]


def test_gemini_payload_maps_roles_and_normalizes_alternation():
    p = GeminiProvider(None)
    history = [
        {"role": "user", "content": "a"},
        {"role": "user", "content": "b"},            # two in a row -> merged
        {"role": "assistant", "content": "c"},
        {"role": "assistant", "content": "d"},       # merged
    ]
    _, body = p._chat_request("sys", "e", "gemini", False, history)
    contents = body["contents"]
    # user+user merged, then model+model merged, then the new user turn
    assert [c["role"] for c in contents] == ["user", "model", "user"]
    assert contents[0]["parts"][0]["text"] == "a\nb"
    assert contents[1]["parts"][0]["text"] == "c\nd"
    assert contents[2]["parts"][0]["text"] == "e"
    assert body["systemInstruction"]["parts"][0]["text"] == "sys"


# ---------------------------------------------------------------------------
# 2. the endpoint threads history from the conversation
# ---------------------------------------------------------------------------
def _fake_history_provider() -> Any:
    calls: Dict[str, Any] = {}

    class _Provider:
        def __init__(self, settings):
            self.name = "openai"

        async def chat(self, system, user, *, model, json_mode=False, history=(),
                       timeout=30.0):
            calls["user"] = user
            calls["history"] = list(history)
            return ProviderReply(True, self.name, model, text=f"echo:{user}")

    return _Provider, calls


def _seed_conversation(conversation_id: str, turns) -> None:
    """Insert a chat + ordered messages directly (the dashboard's storage path)."""
    from jarvis.db.database import get_db
    from jarvis.core.models import utcnow

    db = get_db()
    base = utcnow()
    db.execute(
        "INSERT INTO ai_chats (id, title, created_at, updated_at) VALUES (?,?,?,?)",
        (conversation_id, "t", base.isoformat(), base.isoformat()),
    )
    for i, (role, content) in enumerate(turns):
        ts = base.timestamp() + i
        db.execute(
            "INSERT INTO ai_messages (id, conversation_id, role, content, timestamp) "
            "VALUES (?,?,?,?,?)",
            (f"{conversation_id}-msg-{i}", conversation_id, role, content,
             __import__("datetime").datetime.fromtimestamp(ts, tz=base.tzinfo).isoformat()),
        )


def test_chat_send_threads_prior_turns_to_the_provider(client: TestClient, monkeypatch):
    import sys
    api = sys.modules["jarvis.api.app"]
    fake, calls = _fake_history_provider()
    monkeypatch.setattr(api, "select_provider", lambda s: fake(s))
    monkeypatch.setattr(api.KeychainStorage, "retrieve", staticmethod(lambda pid: "sk-test"))
    monkeypatch.setattr(api.get_settings(), "ai_provider", "openai", raising=False)

    _seed_conversation("conv-1", [
        ("user", "what is my first task?"),
        ("assistant", "Your first task is the report."),
        ("user", "and the second?"),   # the in-flight message, pre-persisted
    ])

    resp = client.post(
        "/ai/chat/send",
        headers=AUTH_HEADERS,
        json={"text": "and the second?", "conversation_id": "conv-1"},
    )
    assert resp.status_code == 200
    assert calls["user"] == "and the second?"
    # History received: the prior turns, WITHOUT duplicating the in-flight one.
    assert calls["history"] == [
        {"role": "user", "content": "what is my first task?"},
        {"role": "assistant", "content": "Your first task is the report."},
    ]


def test_chat_send_without_conversation_sends_no_history(client: TestClient, monkeypatch):
    import sys
    api = sys.modules["jarvis.api.app"]
    fake, calls = _fake_history_provider()
    monkeypatch.setattr(api, "select_provider", lambda s: fake(s))
    monkeypatch.setattr(api.KeychainStorage, "retrieve", staticmethod(lambda pid: "sk-test"))
    monkeypatch.setattr(api.get_settings(), "ai_provider", "openai", raising=False)

    resp = client.post("/ai/chat/send", headers=AUTH_HEADERS, json={"text": "hello"})
    assert resp.status_code == 200
    assert calls["history"] == []


def test_chat_send_history_is_bounded(client: TestClient, monkeypatch):
    import sys
    api = sys.modules["jarvis.api.app"]
    fake, calls = _fake_history_provider()
    monkeypatch.setattr(api, "select_provider", lambda s: fake(s))
    monkeypatch.setattr(api.KeychainStorage, "retrieve", staticmethod(lambda pid: "sk-test"))
    monkeypatch.setattr(api.get_settings(), "ai_provider", "openai", raising=False)

    turns = [("user" if i % 2 == 0 else "assistant", f"turn {i}") for i in range(30)]
    turns.append(("user", "the latest"))  # in-flight
    _seed_conversation("conv-many", turns)

    client.post("/ai/chat/send", headers=AUTH_HEADERS,
                json={"text": "the latest", "conversation_id": "conv-many"})
    limit = api._CHAT_HISTORY_LIMIT
    # bounded window, and the in-flight message not duplicated
    assert len(calls["history"]) <= limit
    assert calls["history"][-1] != {"role": "user", "content": "the latest"} or \
        sum(1 for h in calls["history"] if h["content"] == "the latest") <= 1
