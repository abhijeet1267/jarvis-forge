"""Phase 10 — does a user-visible "forget" actually remove the fact?

The property under test, stated once and then hammered:

    **A deletion response must not claim complete forgetting while another
    retrievable copy of the fact remains.**

Reproduced from ``audit/PHASE9_FINAL_REPORT.md`` P0-5: one spoken
``remember that my old car plate is <X>`` writes **two** rows into the same
``memories`` table -- the fact itself, and an automatic ``session``-scoped echo
of the whole exchange written by ``JarvisCore._remember_outcome()``
(``jarvis/core/orchestrator.py``). ``forget_memory`` deleted only the row the
user named and answered with ``"Forgotten: <the fact>"`` and
``Verification.SUCCESS`` while the echo kept the sentence fully retrievable.

Every test here computes its own ground truth with raw SQL against the live
database rather than through the code under test, so a wrong helper cannot make
its own report look right. Each test uses its own canary token because the
session-wide test database accumulates rows across modules.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Dict, List

import pytest

from jarvis.core.models import CommandRequest, MemoryScope
from jarvis.core.orchestrator import JarvisCore
from jarvis.db.database import get_db
from jarvis.memory.embeddings import HashingEmbedder, RemoteEmbedder, get_embedder
from jarvis.memory.graph import ProjectGraph
from jarvis.memory.hybrid import hybrid_search
from jarvis.memory.store import MemoryStore
from jarvis.memory.vector_store import VectorIndex
from jarvis.security.privacy import PrivacyManager
from jarvis.tools.base import ToolContext
from jarvis.tools.memory_controls import export_memories, forget_memory
from jarvis.tools.memory_tools import search_memory

# ---------------------------------------------------------------------------
# Ground-truth oracles (deliberately independent of the code under test)
# ---------------------------------------------------------------------------


def _rows_holding(text: str, table: str = "memories") -> List[Dict[str, Any]]:
    """Every live row in ``table`` whose content mentions ``text``."""
    db = get_db()
    columns = [c["name"] for c in db.query(f"PRAGMA table_info({table})")]
    where = " OR ".join(f"CAST({c} AS TEXT) LIKE ?" for c in columns)
    rows = db.query(f"SELECT * FROM {table} WHERE {where}", tuple(f"%{text}%" for _ in columns))
    return [dict(r) for r in rows]


def _everywhere_holding(text: str) -> Dict[str, int]:
    """``table.column`` -> count, over the whole schema."""
    db = get_db()
    found: Dict[str, int] = {}
    for table in [r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")]:
        for column in [c["name"] for c in db.query(f"PRAGMA table_info({table})")]:
            try:
                n = db.query(
                    f"SELECT COUNT(*) AS n FROM {table} WHERE CAST({column} AS TEXT) LIKE ?",
                    (f"%{text}%",),
                )[0]["n"]
            except Exception:
                continue
            if n:
                found[f"{table}.{column}"] = n
    return found


def _memory_contents() -> List[str]:
    return [r["content"] for r in get_db().query("SELECT content FROM memories")]


def _fact(tag: str) -> str:
    """A fact whose distinctive token survives tokenisation and LIKE matching."""
    return f"my old car plate is {tag}{uuid.uuid4().hex[:6].upper()}"


def _token(fact: str) -> str:
    return fact.rsplit(" ", 1)[1]


async def _speak_remember(core: JarvisCore, fact: str) -> Dict[str, Any]:
    """Run the real pipeline for "remember that <fact>" and return its rows."""
    response = await core.handle(CommandRequest(text=f"remember that {fact}", source="test"))
    assert response.reply.startswith("Got it. I will remember:"), response.reply
    token = _token(fact)
    rows = {r["id"]: r for r in _rows_holding(token, "memories")}
    named = [r for r in rows.values() if r["content"] == fact]
    assert len(named) == 1, list(rows.values())
    return {"fact": fact, "token": token, "target": named[0], "all": rows}


# ---------------------------------------------------------------------------
# 1. The duplication itself
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_spoken_remember_writes_two_memory_rows(core: JarvisCore) -> None:
    """The premise of the defect: the fact is stored twice on one command."""
    stored = await _speak_remember(core, _fact("DUP-"))
    echo = next(r for r in stored["all"].values() if r["id"] != stored["target"]["id"])

    assert len(stored["all"]) == 2
    assert {r["scope"] for r in stored["all"].values()} == {"long_term", "session"}
    assert echo["source"] == "orchestrator"
    # The echo quotes the user's wording *and* the tool's confirmation, so the
    # secret appears twice inside one surviving row.
    assert echo["content"].count(stored["token"]) == 2


# ---------------------------------------------------------------------------
# 2. The headline triple: remember -> forget -> search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_headline_remember_forget_search_triple(core: JarvisCore) -> None:
    """The exact Phase 9 reproduction, as a failing test first.

    Before the fix the tool answered ``"Forgotten: my old car plate is
    MH12XY9345"`` with ``verification=SUCCESS`` and ``output={"memory_id": ...}``
    while ``search_memory`` still returned the sentence from the surviving echo.
    """
    fact = "my old car plate is MH12XY9345"
    assert _everywhere_holding("MH12XY9345") == {}, "canary must be unused elsewhere"
    stored = await _speak_remember(core, fact)
    echo_row = next(
        r for r in _rows_holding("MH12XY9345", "memories") if r["id"] != stored["target"]["id"]
    )

    result = await forget_memory(ToolContext(), memory_id=stored["target"]["id"])

    # The row the user named really is gone -- the fix must not break that.
    assert result.ok
    assert MemoryStore().get(stored["target"]["id"]) is None

    # ...and the fact is still retrievable through the surface the user would use.
    hits = hybrid_search("MH12XY9345", limit=5, store=MemoryStore())["results"]
    assert any("MH12XY9345" in h.item.content for h in hits), "precondition: copy survives"

    # The defect: an unqualified success claim.
    assert result.verified is False, (
        "forget_memory reported SUCCESS while a retrievable copy remained"
    )
    assert result.output["complete"] is False
    assert not result.message.startswith("Forgotten: my old car plate"), result.message
    assert [c["id"] for c in result.output["remaining_copies"]] == [echo_row["id"]]


@pytest.mark.asyncio
async def test_search_listing_and_export_still_answer_after_a_partial_forget(
    core: JarvisCore,
) -> None:
    """Nothing user-facing may imply the fact is gone while it is still served."""
    stored = await _speak_remember(core, _fact("SURF-"))
    token = stored["token"]
    await forget_memory(ToolContext(), memory_id=stored["target"]["id"])

    found = await search_memory(ToolContext(), query=token)
    assert found.ok
    assert any(token in r["content"] for r in found.output["results"])

    listed = _rows_holding(token, "memories")
    assert len(listed) == 1
    exported = await export_memories(ToolContext())
    assert any(token in m["content"] for m in exported.output["memories"])

    # Forgetting the row that carries the echo empties the memory surfaces; the
    # transcript and audit copies are disclosed separately, not silently ignored.
    report = await forget_memory(ToolContext(), memory_id=listed[0]["id"])
    assert report.output["remaining_copies"] == []
    assert report.output["complete"] is True
    assert report.verified is True
    assert report.output["retained"]["messages"] >= 2
    assert report.output["audit_trail_retained"] is True


# ---------------------------------------------------------------------------
# 3. The report's contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_report_claims_match_ground_truth_for_a_clean_fact() -> None:
    """A fact with exactly one row: complete deletion, honestly claimed."""
    store = MemoryStore()
    fact = _fact("CLEAN-")
    item = store.remember(fact, scope=MemoryScope.LONG_TERM)

    result = await forget_memory(ToolContext(), memory_id=item.id)

    assert result.ok
    assert store.get(item.id) is None
    assert result.output["complete"] is True
    assert result.output["removed_ids"] == [item.id]
    assert result.output["remaining_copies"] == []
    assert result.output["audit_trail_retained"] is False
    # Written by the library, not spoken: no transcript or audit copy exists, so
    # the database really is clean of this sentence.
    assert _everywhere_holding(_token(fact)) == {}
    assert result.verified is True
    assert item.id in result.message


@pytest.mark.asyncio
async def test_report_complete_flag_equals_independent_copy_scan() -> None:
    """``complete`` must mean exactly "no retrievable copy of the text remains"."""
    store = MemoryStore()
    ctx = ToolContext()

    # Case A: one row, nothing derived.
    solo_fact = _fact("SOLO-")
    a = await forget_memory(ctx, memory_id=store.remember(solo_fact).id)

    # Case B: the same text also stored under another id (an echo-shaped copy).
    duplicated = _fact("DUP2-")
    original = store.remember(duplicated, scope=MemoryScope.LONG_TERM)
    copy = store.remember(
        f"User asked: remember that {duplicated}. Result: ok", scope=MemoryScope.SESSION
    )
    b = await forget_memory(ctx, memory_id=original.id)

    # Case C: the text also sits in a project-graph node label.
    graphed = _fact("GRAPH-")
    g_item = store.remember(graphed, scope=MemoryScope.LONG_TERM)
    project_id = ProjectGraph().get_or_create_project(f"phase10-{_token(graphed)}")
    ProjectGraph().add_node(project_id, "FAILURE", graphed)
    c = await forget_memory(ctx, memory_id=g_item.id)

    assert a.output["complete"] is True, a.message
    assert a.verified is True

    assert b.output["complete"] is False, b.message
    assert [x["id"] for x in b.output["remaining_copies"]] == [copy.id]
    assert b.verified is False
    # The bare unqualified form the brief forbids.
    assert not b.message.startswith("Forgotten:")

    # Once the surviving copy is gone too, the report may say so.
    again = await forget_memory(ctx, memory_id=copy.id)
    assert again.output["complete"] is True
    assert _rows_holding(_token(duplicated), "memories") == []

    assert c.output["complete"] is False, (
        "a surviving graph node is a retrievable copy; the reply must not claim the fact is gone"
    )
    assert c.output["retained"]["graph_nodes"] == 1
    assert c.verified is False
    assert not c.message.startswith("Forgotten:")


@pytest.mark.asyncio
async def test_report_names_the_retained_audit_copies(core: JarvisCore) -> None:
    """The audit trail keeps the wording by design; the response must say so."""
    stored = await _speak_remember(core, _fact("AUDIT-"))
    token = stored["token"]

    result = await forget_memory(ToolContext(), memory_id=stored["target"]["id"])
    retained = result.output["retained"]

    # Both were written by the pipeline, not by the tool, and no memory deletion
    # reaches them. tests/test_privacy.py asserts the audit log must survive
    # memory deletion, so this is intended behaviour -- reported, not "fixed".
    assert retained["messages"] >= 2
    assert retained["action_logs"] >= 1
    assert result.output["audit_trail_retained"] is True
    assert "audit" in result.message.lower()

    dump = json.dumps(PrivacyManager().export(redact_output=False), default=str)
    assert token in dump, "the transcript/audit copies are export-visible; hence disclosed"

    from jarvis.config import get_settings

    audit_file = get_settings().audit_path
    assert audit_file.exists()
    assert token in audit_file.read_text(encoding="utf-8", errors="replace")


@pytest.mark.asyncio
async def test_response_text_does_not_recite_the_forgotten_fact(
    core: JarvisCore,
) -> None:
    """The response must not become a fresh plaintext copy of what it removed.

    ``_remember_outcome()`` and ``_log_message()`` both re-store whatever the
    tool says, so a reply that quotes the fact re-saves it after reporting it
    gone. The old ``"Forgotten: {content[:60]}"`` did exactly that.
    """
    store = MemoryStore()
    fact = _fact("REQUOTE-")
    item = store.remember(fact, scope=MemoryScope.LONG_TERM)

    clean = await forget_memory(ToolContext(), memory_id=item.id)
    assert fact not in clean.message
    assert fact not in json.dumps(clean.output, default=str)

    stored = await _speak_remember(core, _fact("REQUOTE2-"))
    partial = await forget_memory(ToolContext(), memory_id=stored["target"]["id"])
    assert stored["fact"] not in partial.message
    assert stored["fact"] not in json.dumps(partial.output, default=str)


# ---------------------------------------------------------------------------
# 4. Every other place a copy could live
# ---------------------------------------------------------------------------


def test_the_vector_for_a_deleted_memory_is_gone() -> None:
    """Vectors are a column on the row, so no second index can resurrect them."""
    store = MemoryStore()
    fact = _fact("VEC-")
    token = _token(fact)
    item = store.remember(fact, scope=MemoryScope.LONG_TERM, index=True)

    index = VectorIndex()
    assert item.id in {h.memory_id for h in index.search(token, limit=500)}
    indexed_before = index.stats()["indexed"]

    assert store.forget(item.id) is True

    assert item.id not in {memory_id for memory_id, _v in index._load()}
    assert item.id not in {h.memory_id for h in index.search(token, limit=500)}
    assert index.stats()["indexed"] == indexed_before - 1
    # Re-indexing cannot re-materialise a deleted row.
    index.index_all()
    assert item.id not in {memory_id for memory_id, _v in index._load()}
    assert not any(token in m.content for m in store.search(token, limit=50))


def test_there_is_no_fts_or_sidecar_index_to_clean() -> None:
    """Retrieval reads the ``memories`` rows, so deleting them is enough -- there."""
    db = get_db()
    objects = db.query("SELECT name, type, sql FROM sqlite_master WHERE sql IS NOT NULL")
    # An FTS index would be a virtual table; there is none, so the keyword side
    # really is computed from `memories.content` at query time.
    assert [o["name"] for o in objects if "virtual table" in (o["sql"] or "").lower()] == []
    tables = {r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")}
    assert [t for t in tables if re.search(r"(^|_)fts($|_)", t)] == []

    from jarvis.config import get_settings

    # No FAISS/ANN sidecar under the home directory: the whole "vector index" is
    # the embedding column, so there is nothing extra to purge on deletion.
    stray = [
        p.name
        for p in get_settings().home.rglob("*")
        if p.is_file() and p.suffix in {".faiss", ".index", ".npy", ".pkl"}
    ]
    assert stray == []


@pytest.mark.asyncio
async def test_a_graph_node_survives_memory_deletion() -> None:
    """Characterisation: ``ProjectGraph`` has no delete API, so this copy is permanent."""
    store = MemoryStore()
    fact = _fact("GHOST-")
    item = store.remember(fact, scope=MemoryScope.LONG_TERM)
    graph = ProjectGraph()
    project_id = graph.get_or_create_project(f"phase10-{_token(fact)}")
    graph.add_node(project_id, "FAILURE", fact)

    result = await forget_memory(ToolContext(), memory_id=item.id)

    assert store.get(item.id) is None
    assert [n.label for n in graph.nodes(project_id)] == [fact]
    assert graph.explain_failures(project_id)[0]["failure"] == fact  # a user-facing read
    assert result.output["retained"]["graph_nodes"] == 1
    assert result.output["complete"] is False
    assert "graph" in result.message.lower()


@pytest.mark.asyncio
async def test_clean_deletion_leaves_listing_and_export_empty_of_it() -> None:
    """The ``GET /memory``-equivalent path, for a fact the pipeline never echoed."""
    store = MemoryStore()
    fact = _fact("LIST-")
    token = _token(fact)
    item = store.remember(fact, scope=MemoryScope.LONG_TERM)
    assert any(token in c for c in _memory_contents())

    await forget_memory(ToolContext(), memory_id=item.id)

    assert [c for c in _memory_contents() if token in c] == []  # GET /memory
    assert [m for m in store.search(token, limit=50) if token in m.content] == []
    exported = await export_memories(ToolContext())
    assert [m for m in exported.output["memories"] if token in m["content"]] == []
    payload = PrivacyManager().export(redact_output=False)
    assert payload["totals"]["memories"] == len(_memory_contents())
    assert token not in json.dumps(payload, default=str)


@pytest.mark.asyncio
async def test_deleting_the_echo_reports_the_original_still_in_memory() -> None:
    """The check runs both ways, so no deletion order can claim completeness.

    Removing an echo must not read as "forgotten" while the shorter fact it
    quoted is still stored, even though no surviving row contains the echo.
    """
    store = MemoryStore()
    fact = _fact("REV-")
    original = store.remember(fact, scope=MemoryScope.LONG_TERM)
    echo = store.remember(
        f"User asked: remember that {fact}. Result: remember_fact: Got it.",
        scope=MemoryScope.SESSION,
    )

    result = await forget_memory(ToolContext(), memory_id=echo.id)

    assert store.get(echo.id) is None
    assert store.get(original.id) is not None
    assert [c["id"] for c in result.output["remaining_copies"]] == [original.id]
    assert result.output["complete"] is False
    assert result.verified is False
    assert _token(fact) not in result.message


def test_forget_reports_whether_a_row_actually_matched() -> None:
    """``forget()`` used to answer True for an id that was never there."""
    store = MemoryStore()
    item = store.remember(_fact("ROWCOUNT-"), scope=MemoryScope.SESSION)

    assert store.forget(item.id) is True
    assert store.forget(item.id) is False
    assert store.forget("MEM-doesnotexist") is False


@pytest.mark.asyncio
async def test_unknown_id_is_still_a_plain_failure() -> None:
    result = await forget_memory(ToolContext(), memory_id="MEM-notarealid")
    assert result.ok is False
    assert result.verified is False


# ---------------------------------------------------------------------------
# 5. Residuals this phase cannot fix (owned elsewhere)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def api_client():
    """An authenticated client: the HTTP forget route is a second, separate path."""
    import os

    from fastapi.testclient import TestClient

    from jarvis.api.app import app

    client = TestClient(app, base_url="http://127.0.0.1:8765")
    client.headers.update({"X-JARVIS-Token": os.environ["JARVIS_API_TOKEN"]})
    return client


@pytest.mark.xfail(
    reason=(
        "POST /memory/{id}/forget (jarvis/api/app.py) has its own handler and returns a "
        "hardcoded {'deleted': True}; it never goes through the forget_memory tool, so the "
        "honest report added there does not reach the HTTP surface. app.py is outside this "
        "phase's scope; MemoryStore.quoting() is public precisely so that route can report "
        "the same facts."
    ),
    strict=False,
)
@pytest.mark.asyncio
async def test_http_forget_route_also_has_to_stop_claiming_completeness(
    api_client, core: JarvisCore
) -> None:
    stored = await _speak_remember(core, _fact("HTTP-"))
    token = stored["token"]

    response = api_client.post(f"/memory/{stored['target']['id']}/forget")
    assert response.status_code == 200
    body = response.json()
    assert body["deleted"] is True
    assert _rows_holding(token, "memories"), "precondition: the echo still carries the fact"
    assert body.get("complete") is False, "the route answers 'deleted: true' and stops there"


@pytest.mark.asyncio
@pytest.mark.xfail(
    reason=(
        "Root cause is JarvisCore._remember_outcome() in jarvis/core/orchestrator.py, "
        "which this phase does not own: every executed command stores a session memory "
        "quoting the raw request and the tool reply, so the recall used to check a "
        "deletion writes another copy."
    ),
    strict=False,
)
async def test_verifying_a_deletion_does_not_create_a_new_echo(core: JarvisCore) -> None:
    stored = await _speak_remember(core, _fact("AMP-"))
    token = stored["token"]
    await forget_memory(ToolContext(), memory_id=stored["target"]["id"])

    before = len(_rows_holding(token, "memories"))
    await core.handle(CommandRequest(text=f"do you remember {token}?", source="test"))

    assert len(_rows_holding(token, "memories")) == before, (
        "the recall attempt added another copy of the forgotten fact"
    )
    assert stored["fact"] not in _memory_contents()


@pytest.mark.xfail(
    reason=(
        "RemoteEmbedder._cache (jarvis/memory/embeddings.py:168) is keyed by raw text "
        "and has no eviction API, so a deleted fact stays resident in the process. "
        "embeddings.py is outside this phase's scope."
    ),
    strict=False,
)
def test_no_in_process_embedding_cache_survives_deletion() -> None:
    store = MemoryStore()
    fact = _fact("CACHE-")
    item = store.remember(fact, scope=MemoryScope.LONG_TERM, index=False)

    embedder = get_embedder()
    assert isinstance(embedder, HashingEmbedder), "the suite must not run a remote embedder"
    assert not any(fact in str(value) for value in vars(embedder).values())

    remote = RemoteEmbedder()
    remote._cache[fact] = [0.0]  # exactly what RemoteEmbedder.embed() leaves behind
    store.forget(item.id)
    assert not any(fact in str(key) for key in remote._cache)
