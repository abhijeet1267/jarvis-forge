"""Advanced memory tests (v0.9).

Covers the vector layer, hybrid retrieval, the memory lifecycle and the user
controls. The property that matters most: **vector search is an enhancement, and
keyword search must keep working without it.**
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from jarvis.core.models import (
    MemoryScope,
    MemoryType,
    PlannedAction,
    PolicyDecision,
    PrivacyLevel,
    RiskLevel,
)
from jarvis.memory.embeddings import (
    DEFAULT_DIM,
    HashingEmbedder,
    cosine,
    describe_embeddings,
    features,
    get_embedder,
    reset_embedder,
)
from jarvis.memory.hybrid import RRF_K, fuse, hybrid_search
from jarvis.memory.store import MemoryStore
from jarvis.memory.vector_store import VectorIndex
from jarvis.tools.base import ToolContext
from jarvis.tools.memory_controls import (
    clear_memory_scope,
    export_memories,
    forget_memory,
    inspect_memory,
    memory_overview,
    set_memory_category,
    sweep_expired_memories,
)


@pytest.fixture()
def embedder() -> HashingEmbedder:
    return HashingEmbedder()


@pytest.fixture()
def ctx() -> ToolContext:
    return ToolContext()


@pytest.fixture()
def memory() -> MemoryStore:
    return MemoryStore()


# ---------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------
def test_vectors_have_the_configured_width(embedder: HashingEmbedder) -> None:
    assert len(embedder.embed("anything at all")) == DEFAULT_DIM


def test_vectors_are_unit_length(embedder: HashingEmbedder) -> None:
    vector = embedder.embed("a sentence about robots and budgets")
    assert math.isclose(math.sqrt(sum(v * v for v in vector)), 1.0, rel_tol=1e-9)


def test_embedding_is_deterministic(embedder: HashingEmbedder) -> None:
    """Must not rely on Python's hash(), which is randomised per process."""
    assert embedder.embed("stable input") == embedder.embed("stable input")


def test_empty_text_does_not_crash(embedder: HashingEmbedder) -> None:
    vector = embedder.embed("")
    assert len(vector) == DEFAULT_DIM
    assert all(v == 0.0 for v in vector)


def test_identical_text_scores_one(embedder: HashingEmbedder) -> None:
    text = "the AMR chassis uses aluminium"
    assert math.isclose(cosine(embedder.embed(text), embedder.embed(text)), 1.0, rel_tol=1e-6)


def test_typos_still_match(embedder: HashingEmbedder) -> None:
    """Character trigrams are what make this tolerant of misspellings."""
    score = cosine(embedder.embed("recieve the report"), embedder.embed("receive the report"))
    assert score > 0.4, score


def test_morphology_still_matches(embedder: HashingEmbedder) -> None:
    score = cosine(embedder.embed("the robot is running tests"), embedder.embed("the robot ran a test"))
    assert score > 0.2, score


def test_unrelated_text_scores_low(embedder: HashingEmbedder) -> None:
    score = cosine(embedder.embed("the robot is running tests"), embedder.embed("quarterly budget spreadsheet"))
    assert score < 0.15, score


def test_related_scores_above_unrelated(embedder: HashingEmbedder) -> None:
    base = embedder.embed("the AMR chassis uses 3mm aluminium")
    near = embedder.embed("what material is the chassis made of")
    far = embedder.embed("quarterly budget spreadsheet for marketing")
    assert cosine(base, near) > cosine(base, far)


def test_cosine_handles_degenerate_inputs() -> None:
    assert cosine([], []) == 0.0
    assert cosine([1.0, 2.0], [1.0]) == 0.0
    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_features_separate_words_from_trigrams(embedder: HashingEmbedder) -> None:
    extracted = features("robot robot arm")
    assert any(k.startswith("w:") for k in extracted)
    assert any(k.startswith("t:") for k in extracted)
    # Sublinear term frequency: repeating a word does not scale linearly.
    assert extracted["w:robot"] < 2.0


def test_describe_reports_the_local_embedder() -> None:
    info = describe_embeddings()
    assert info["active"] in ("hashing", "remote")
    assert info["local_available"] is True
    assert "dim" in info


# ---------------------------------------------------------------------------
# Vector index
# ---------------------------------------------------------------------------
def test_index_and_search(memory: MemoryStore) -> None:
    item = memory.remember("the AMR chassis uses 3mm aluminium", scope=MemoryScope.LONG_TERM)
    index = VectorIndex()

    hits = index.search("aluminium chassis", limit=5)
    assert any(hit.memory_id == item.id for hit in hits)


def test_index_stats_report_coverage(memory: MemoryStore) -> None:
    memory.remember("indexed on write", scope=MemoryScope.SESSION)
    stats = VectorIndex().stats()
    assert stats["memories"] >= 1
    assert 0.0 <= stats["coverage"] <= 1.0


def test_index_all_backfills_missing_vectors(memory: MemoryStore) -> None:
    from jarvis.db.database import get_db

    item = memory.remember("needs a vector", scope=MemoryScope.SESSION, index=False)
    get_db().execute("UPDATE memories SET embedding = NULL WHERE id = ?", (item.id,))

    assert VectorIndex().index_all() >= 1
    assert any(h.memory_id == item.id for h in VectorIndex().search("needs a vector"))


def test_rebuild_recomputes_everything(memory: MemoryStore) -> None:
    memory.remember("rebuild me", scope=MemoryScope.SESSION)
    assert VectorIndex().rebuild() >= 1


def test_legacy_memories_become_searchable_after_backfill(memory: MemoryStore) -> None:
    """Memories written before v0.9 have no vector.

    Without a backfill they would be invisible to semantic search forever, which
    is exactly the bug this guards: on the real database only 5 of 98 memories
    were indexed until the startup backfill ran.
    """
    from jarvis.db.database import get_db

    # Simulate a pre-v0.9 row: stored, but with no vector.
    legacy = memory.remember(
        "the gearbox failed during the endurance run", scope=MemoryScope.LONG_TERM
    )
    get_db().execute("UPDATE memories SET embedding = NULL WHERE id = ?", (legacy.id,))

    before = VectorIndex().stats()
    assert before["pending"] >= 1

    assert VectorIndex().index_all() >= 1

    after = VectorIndex().stats()
    assert after["pending"] == 0
    assert any(h.memory_id == legacy.id for h in VectorIndex().search("gearbox endurance"))


def test_backfill_is_idempotent(memory: MemoryStore) -> None:
    """A second pass must find nothing left to do."""
    memory.remember("already indexed", scope=MemoryScope.SESSION)
    VectorIndex().index_all()
    assert VectorIndex().index_all() == 0


def test_search_with_an_empty_query_returns_nothing() -> None:
    assert VectorIndex().search("") == []


# ---------------------------------------------------------------------------
# Hybrid fusion
# ---------------------------------------------------------------------------
def test_rrf_fuses_both_retrievers() -> None:
    fused = fuse(["a", "b"], ["b", "c"])

    # "b" appears in both lists, so it must outrank either single-source item.
    assert fused["b"]["score"] > fused["a"]["score"]
    assert fused["b"]["score"] > fused["c"]["score"]
    assert fused["b"]["keyword_rank"] == 2
    assert fused["b"]["vector_rank"] == 1
    assert fused["a"]["vector_rank"] is None


def test_rrf_scores_use_the_damping_constant() -> None:
    fused = fuse(["only"], [])
    assert math.isclose(fused["only"]["score"], 1.0 / (RRF_K + 1))


def test_rrf_handles_empty_inputs() -> None:
    assert fuse([], []) == {}


def test_hybrid_search_returns_results(memory: MemoryStore) -> None:
    memory.remember("the AMR chassis uses 3mm aluminium", scope=MemoryScope.LONG_TERM)
    outcome = hybrid_search("aluminium chassis", limit=5, store=memory)
    assert outcome["results"]
    assert outcome["retrieval"]["mode"] in ("hybrid", "keyword_only")


def test_hybrid_search_falls_back_to_keyword_only(memory: MemoryStore, monkeypatch) -> None:
    """Vector search must never be a hard dependency."""
    memory.remember("fallback target content", scope=MemoryScope.LONG_TERM)

    class Broken:
        def search(self, *args, **kwargs):
            raise RuntimeError("vector store exploded")

    outcome = hybrid_search("fallback target", limit=5, store=memory, index=Broken())
    assert outcome["retrieval"]["mode"] == "keyword_only"
    assert outcome["results"], "keyword search must still return results"
    assert "exploded" in outcome["retrieval"]["error"]


def test_hybrid_search_handles_an_empty_query(memory: MemoryStore) -> None:
    outcome = hybrid_search("", store=memory)
    assert outcome["results"] == []
    assert outcome["retrieval"]["mode"] == "none"


# ---------------------------------------------------------------------------
# Memory types and lifecycle
# ---------------------------------------------------------------------------
def test_all_documented_memory_types_exist() -> None:
    expected = {
        "preference", "fact", "task", "project", "decision", "experiment",
        "failure", "fix", "document", "conversation", "device", "relationship",
    }
    assert expected <= set(MemoryType.values())


@pytest.mark.parametrize("memory_type", [t.value for t in MemoryType])
def test_memory_type_round_trips(memory: MemoryStore, memory_type: str) -> None:
    item = memory.remember(
        f"a {memory_type} memory", scope=MemoryScope.SESSION, memory_type=MemoryType(memory_type)
    )
    assert memory.get(item.id).memory_type.value == memory_type


def test_importance_is_clamped(memory: MemoryStore) -> None:
    high = memory.remember("very important", scope=MemoryScope.SESSION, importance=5.0)
    low = memory.remember("not important", scope=MemoryScope.SESSION, importance=-3.0)
    assert high.importance == 1.0
    assert low.importance == 0.0


def test_expiry_is_stored_and_detected(memory: MemoryStore) -> None:
    past = datetime.now(timezone.utc) - timedelta(days=1)
    future = datetime.now(timezone.utc) + timedelta(days=1)

    expired = memory.remember("old fact", scope=MemoryScope.SESSION, expires_at=past)
    live = memory.remember("current fact", scope=MemoryScope.SESSION, expires_at=future)
    forever = memory.remember("timeless fact", scope=MemoryScope.SESSION)

    assert memory.get(expired.id).is_expired() is True
    assert memory.get(live.id).is_expired() is False
    assert memory.get(forever.id).is_expired() is False


def test_relationships_round_trip(memory: MemoryStore) -> None:
    item = memory.remember(
        "the fix for the AMR bug",
        scope=MemoryScope.PROJECT,
        memory_type=MemoryType.FIX,
        relationships={"fixes": ["MEM-abc123"], "related_to": ["MEM-def456"]},
    )
    loaded = memory.get(item.id)
    assert loaded.relationships["fixes"] == ["MEM-abc123"]


def test_existing_memories_keep_working(memory: MemoryStore) -> None:
    """A memory stored the old way must still load with sensible defaults."""
    item = memory.remember("plain old memory", scope=MemoryScope.SESSION)
    loaded = memory.get(item.id)
    assert loaded.memory_type is MemoryType.GENERAL
    assert loaded.importance == 0.5
    assert loaded.expires_at is None
    assert loaded.relationships == {}


# ---------------------------------------------------------------------------
# Controls
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_overview(ctx: ToolContext, memory: MemoryStore) -> None:
    memory.remember("overview target", scope=MemoryScope.SESSION)
    result = await memory_overview(ctx)
    assert result.ok
    assert result.output["total"] >= 1
    assert "by_type" in result.output


@pytest.mark.asyncio
async def test_inspect(ctx: ToolContext, memory: MemoryStore) -> None:
    item = memory.remember("inspect me", scope=MemoryScope.SESSION, importance=0.9)
    result = await inspect_memory(ctx, memory_id=item.id)
    assert result.ok
    assert result.output["memory"]["importance"] == 0.9


@pytest.mark.asyncio
async def test_inspect_unknown_id_fails(ctx: ToolContext) -> None:
    assert not (await inspect_memory(ctx, memory_id="MEM-nope")).ok


@pytest.mark.asyncio
async def test_export_withholds_sensitive_by_default(ctx: ToolContext, memory: MemoryStore) -> None:
    """An export is a file that gets copied around, so secrets stay out."""
    memory.remember("ordinary fact", scope=MemoryScope.LONG_TERM)
    memory.remember(
        "a private secret", scope=MemoryScope.PRIVATE, privacy=PrivacyLevel.SECRET
    )

    withheld_run = await export_memories(ctx)
    assert withheld_run.output["withheld"] >= 1
    assert all(m["privacy"] not in ("private", "secret") for m in withheld_run.output["memories"])

    full_run = await export_memories(ctx, include_sensitive=True)
    assert full_run.output["withheld"] == 0


@pytest.mark.asyncio
async def test_export_rejects_an_unknown_scope(ctx: ToolContext) -> None:
    assert not (await export_memories(ctx, scope="not_a_scope")).ok


@pytest.mark.asyncio
async def test_forget_deletes_one_memory(ctx: ToolContext, memory: MemoryStore) -> None:
    item = memory.remember("forget me", scope=MemoryScope.SESSION)
    result = await forget_memory(ctx, memory_id=item.id)
    assert result.ok
    assert memory.get(item.id) is None


@pytest.mark.asyncio
async def test_clear_scope_removes_only_that_scope(ctx: ToolContext, memory: MemoryStore) -> None:
    keep = memory.remember("keep me", scope=MemoryScope.LONG_TERM)
    memory.remember("drop me", scope=MemoryScope.SESSION)

    result = await clear_memory_scope(ctx, scope="session")
    assert result.ok
    assert memory.get(keep.id) is not None


@pytest.mark.asyncio
async def test_clear_scope_rejects_an_unknown_scope(ctx: ToolContext) -> None:
    assert not (await clear_memory_scope(ctx, scope="nonsense")).ok


@pytest.mark.asyncio
async def test_sweep_removes_only_expired(ctx: ToolContext, memory: MemoryStore) -> None:
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    future = datetime.now(timezone.utc) + timedelta(days=30)

    stale = memory.remember("stale", scope=MemoryScope.SESSION, expires_at=past)
    fresh = memory.remember("fresh", scope=MemoryScope.SESSION, expires_at=future)

    result = await sweep_expired_memories(ctx)
    assert result.ok
    assert result.output["removed"] >= 1
    assert memory.get(stale.id) is None
    assert memory.get(fresh.id) is not None


@pytest.mark.asyncio
async def test_set_category_toggles(ctx: ToolContext) -> None:
    off = await set_memory_category(ctx, category="preference", enabled=False)
    assert off.ok

    on = await set_memory_category(ctx, category="preference", enabled=True)
    assert on.ok


@pytest.mark.asyncio
async def test_set_category_rejects_an_unknown_name(ctx: ToolContext) -> None:
    assert not (await set_memory_category(ctx, category="telepathy")).ok


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "tool,params",
    [
        ("forget_memory", {"memory_id": "MEM-1"}),
        ("clear_memory_scope", {"scope": "session"}),
    ],
)
def test_deletion_is_destructive_and_confirmed(core, tool: str, params: dict) -> None:
    spec = core.tools.get(tool)
    assert spec.risk is RiskLevel.DESTRUCTIVE
    assert spec.requires_confirmation is True

    action = PlannedAction(tool=tool, params=params, device="laptop_main")
    assert core.policy.evaluate(action).decision is PolicyDecision.CONFIRM


@pytest.mark.parametrize(
    "tool,params",
    [
        ("memory_overview", {}),
        ("export_memories", {}),
        ("inspect_memory", {"memory_id": "MEM-1"}),
    ],
)
def test_reads_are_not_gated(core, tool: str, params: dict) -> None:
    action = PlannedAction(tool=tool, params=params, device="laptop_main")
    assert core.policy.evaluate(action).decision is PolicyDecision.EXECUTE


def test_hybrid_search_is_wired_into_search_memory(core) -> None:
    """The existing recall tool must use hybrid retrieval, not keyword alone."""
    from pathlib import Path

    source = Path("jarvis/tools/memory_tools.py").read_text()
    assert "hybrid_search" in source
