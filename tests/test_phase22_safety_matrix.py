"""Phase 22: the plan's own safety and failure matrices, taken literally.

Section 45 of `JARVIS_Forge_Full_Working_Plan.md` lists six phrases JARVIS must
handle as Reject, Clarify, or Require-confirmation, and six disconnections it must
survive honestly. All six phrases were posted through `/command` before this file was
written (with `HOME` pointed at a scratch tree, because the list is deliberately
dangerous). Five of six behaved: blanket delete, untrusted executable, physical
security and privilege escalation each reached the guard, and each left an audit row.

The ones that did not, and what this file pins:

* **"turn on the light"** -- four devices tie on score and the resolver silently
  returned *Kitchen light*, then executed against it. The user named no room, so the
  system chose one and never said it was choosing. The plan asks for Clarify here.
* **"send to Arun"** -- answered with the generic "I understood the words but not the
  goal" help text. A request that names a verb and a recipient is not unrecognised;
  it is missing a payload, and the plan asks for Clarify.
* **offline device control** was audited `unverified` while the offline routine the
  same gateway serves was audited `failure` -- two ledger tokens for one observed
  condition -- and its reply read "I could not on Kitchen light".
* **an embedding outage** was swallowed into an empty vector result, so
  keyword-only-by-failure looked exactly like keyword-only-because-nothing-matched,
  and `/health`'s `llm_enabled` is a claim about configuration, never about reach.

Written test-first: each test here failed before the code it describes existed.
"""

from __future__ import annotations

import pytest
from jarvis.core.models import CommandRequest
from jarvis.home.gateway import HomeGateway, get_home_gateway, reset_home_gateway
from jarvis.memory import hybrid as hybrid_module
from jarvis.memory.vector_store import VectorIndex
from jarvis.tools import home as home_module
from jarvis.tools.home import control_home_device


@pytest.fixture(autouse=True)
def _fresh_gateway():
    """The gateway is process-wide; hand each test a clean one and leave it clean."""
    reset_home_gateway(None)
    yield
    reset_home_gateway(None)


def _reply(core, text: str):
    import asyncio

    return asyncio.run(core.handle(CommandRequest(text=text, source="test")))


def _last_audit(core):
    return core.db.query("SELECT * FROM action_logs ORDER BY rowid DESC LIMIT 1")[0]


# ---------------------------------------------------------------------------
# 22-A -- an ambiguous device word is a question, not a coin toss
# ---------------------------------------------------------------------------


def test_turn_on_the_light_does_not_pick_a_device_the_user_never_named(core):
    response = _reply(core, "Jarvis, turn on the light.")

    assert not response.actions, (
        f"an ambiguous request actuated {[a.tool for a in response.actions]}; several "
        "devices tied on score and one was chosen silently")
    assert "which" in response.reply.lower() or "?" in response.reply, response.reply


def test_the_clarification_offers_the_rooms_the_user_has_to_choose_between(core):
    response = _reply(core, "Jarvis, turn on the light.")

    rooms = {d.room.lower() for d in HomeGateway().devices if d.kind == "light"}
    mentioned = {room for room in rooms if room.split()[0] in response.reply.lower()}
    assert len(mentioned) >= 2, (
        f"a clarification that does not show what it is choosing between is not a "
        f"clarification; rooms offered: {mentioned or 'none'} of {sorted(rooms)}")


def test_an_ambiguous_request_still_leaves_an_audit_row(core):
    response = _reply(core, "Jarvis, turn on the light.")

    row = _last_audit(core)
    assert row["result"] == "no_action", (
        "a clarification is a decision; Phase 16 made that accountable, and it stays so")
    assert not response.actions


def test_a_word_that_matches_one_device_still_executes(core):
    """The floor must not become a wall: unambiguous phrasing still acts."""
    response = _reply(core, "Jarvis, turn on the living room fan.")

    assert any(a.tool == "control_home_device" for a in response.actions), response.reply


def test_the_resolver_reports_ambiguity_instead_of_choosing():
    gateway = HomeGateway()
    matches = gateway.ambiguous_matches("light")

    assert len(matches) > 1, "the registry has several lights; the tie is the point"
    assert gateway.resolve("living room fan").name == "Living room fan"
    assert not gateway.ambiguous_matches("living room fan")


# ---------------------------------------------------------------------------
# 22-B / 22-C -- the offline condition is one condition, in the ledger and in English
# ---------------------------------------------------------------------------


async def test_offline_device_control_is_audited_as_a_failure(core):
    result = await control_home_device(
        _ctx(core), device="kitchen light", action="on")

    assert result.verification is not None and result.verification.value == "failed", (
        "offline control must not reach the ledger as 'unverified' while the offline "
        "routine already reports 'failure' for the same observed condition")
    assert result.output.get("provider_status") == "unavailable"


async def test_offline_device_control_speaks_a_sentence(core):
    result = await control_home_device(
        _ctx(core), device="kitchen light", action="on")

    assert "could not on" not in result.message, result.message
    assert "switch on" in result.message.lower()


async def test_offline_set_action_is_described_by_what_it_tried_to_do(core):
    result = await control_home_device(
        _ctx(core), device="study ac", action="set", value=22)

    assert "could not set study ac" not in result.message.lower() or "to 22" in result.message.lower(), (
        result.message)


def _ctx(core):
    from jarvis.tools.base import ToolContext

    return ToolContext(settings=core.settings, apps=core.apps, devices=core.devices,
                       memory=core.memory, graph=core.graph, core=core)


# ---------------------------------------------------------------------------
# 22-D -- "send to Arun" is missing a thing, not a vocabulary word
# ---------------------------------------------------------------------------


def test_send_to_a_person_with_no_payload_asks_what_to_send(core):
    response = _reply(core, "Jarvis, send to Arun.")

    assert not response.actions
    assert "arun" in response.reply.lower(), response.reply
    assert "understood the words but not the goal" not in response.reply.lower(), (
        "a named recipient and verb is a missing-payload question, not an unknown "
        "capability; the plan asks for Clarify")
    assert "what" in response.reply.lower() or "draft" in response.reply.lower()


def test_a_send_with_a_recipient_and_a_body_is_still_a_draft_request(core):
    """The clarification must not swallow the sentences that do have a payload."""
    response = _reply(core, "Jarvis, send an email to Arun saying the build is green.")

    assert response.actions or "arun" in response.reply.lower()


# ---------------------------------------------------------------------------
# 22-F -- "what do you remember about X" has a topic, and deserves its content
# ---------------------------------------------------------------------------


def test_a_topical_recall_reaches_the_search_not_the_counts(core):
    response = _reply(core, "Jarvis, what do you remember about the deploy runbook?")

    tools = [a.tool for a in response.actions]
    assert "memory_overview" not in tools, (
        f"a question naming a topic was answered with item counts: {response.reply}")
    assert "search_memory" in tools, response.reply


def test_a_bare_recall_still_answers_with_the_overview(core):
    response = _reply(core, "Jarvis, what do you remember?")

    assert [a.tool for a in response.actions] == ["memory_overview"], response.reply


def test_the_outage_reaches_a_recall_the_user_can_actually_phrase(core):
    """22-E only matters if the disclosure sits on a reachable path."""
    _reply(core, "Jarvis, remember that the deploy runbook lives in docs/deploy.md.")
    response = _reply(core, "Jarvis, what do you remember about the deploy runbook?")

    assert any(a.tool == "search_memory" for a in response.actions)
    assert "deploy" in response.reply.lower(), response.reply


# ---------------------------------------------------------------------------
# 22-E -- a broken semantic layer must not look like an empty one
# ---------------------------------------------------------------------------


class _FailingEmbedder:
    name = "remote"

    def embed(self, text):
        raise RuntimeError("embeddings endpoint unreachable")

    def describe(self):
        return {"name": "remote", "semantic": True}


def test_an_embedding_outage_reaches_the_retrieval_block(core):
    """The outage is swallowed *inside* VectorIndex.search, so the outer layer's
    carefully-recorded vector_error can never be set. One observed condition has to
    become one reported condition."""
    index = VectorIndex(embedder=_FailingEmbedder())
    result = hybrid_module.hybrid_search("build", limit=3, store=core.memory, index=index)

    assert result["retrieval"]["mode"] == "keyword_only"
    assert "unreachable" in result["retrieval"]["error"], (
        "keyword-only because the model is down must be distinguishable from "
        "keyword-only because nothing semantically matched")


async def test_recall_discloses_a_semantic_outage(core, monkeypatch):
    from jarvis.tools.memory_tools import search_memory

    def _broken_index(*args, **kwargs):
        return VectorIndex(embedder=_FailingEmbedder())

    monkeypatch.setattr(hybrid_module, "VectorIndex", _broken_index)
    result = await search_memory(_ctx(core), query="build")

    assert "semantic" in result.message.lower() or "vector" in result.message.lower(), (
        f"the reply reports an answer without reporting that half the retriever was "
        f"down: {result.message}")


async def test_a_healthy_semantic_layer_says_nothing_about_outages(core):
    from jarvis.tools.memory_tools import search_memory

    result = await search_memory(_ctx(core), query="anything")

    assert "unavailable" not in result.message.lower(), result.message


def test_an_indexing_outage_is_counted_not_dropped(core, monkeypatch):
    """Memories that could not be embedded are 'pending', which is true but blind:
    it cannot tell a never-indexed store from one whose embedder is failing."""
    index = VectorIndex()

    class _FailingEmbedder:
        name = "remote"

        def embed(self, text):
            raise RuntimeError("embeddings endpoint unreachable")

        def describe(self):
            return {"name": "remote", "semantic": True}

    monkeypatch.setattr(index, "embedder", _FailingEmbedder())
    ok_flag = index.index_item("nope-1", "some content")

    assert ok_flag is False
    stats = index.stats()
    assert stats.get("last_error"), (
        "the reason an index is empty belongs in the same place its emptiness is reported")
    assert "unreachable" in str(stats["last_error"])
