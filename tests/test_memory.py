"""Tests for the memory store and the project-state graph."""

from __future__ import annotations

from jarvis.core.models import MemoryScope
from jarvis.memory.graph import ProjectGraph
from jarvis.memory.store import MemoryStore


def test_remember_and_search() -> None:
    store = MemoryStore()
    store.remember(
        "The AMR project uses a 24V motor driver",
        scope=MemoryScope.PROJECT,
        project_id="PROJ-amr",
    )

    hits = store.search("motor driver AMR")
    assert hits, "expected the stored fact to be retrievable"
    assert "motor driver" in hits[0].content


def test_scope_filtering_and_stats() -> None:
    store = MemoryStore()
    store.remember("session only fact", scope=MemoryScope.SESSION)
    store.remember("durable fact about robotics", scope=MemoryScope.LONG_TERM)

    long_term = store.all(scope=MemoryScope.LONG_TERM)
    assert all(m.scope is MemoryScope.LONG_TERM for m in long_term)

    stats = store.stats()
    assert stats["total"] >= 2


def test_forget_deletes() -> None:
    store = MemoryStore()
    item = store.remember("temporary note to delete")
    assert store.get(item.id) is not None

    store.forget(item.id)
    assert store.get(item.id) is None


def test_graph_node_and_edge() -> None:
    graph = ProjectGraph()
    project_id = graph.create_project("Sanjeevani", "biomedical device")

    motor = graph.add_node(project_id, "COMPONENT", "Motor Driver")
    failure = graph.add_node(project_id, "FAILURE", "Motor driver overheated")
    fix = graph.add_node(project_id, "FIX", "Revised driver with heatsink")
    graph.link(motor.node_id, failure.node_id, "failed_with")
    graph.link(failure.node_id, fix.node_id, "fixed_by")

    neighbors = graph.neighbors(failure.node_id)
    labels = {n.label for n in neighbors}
    assert "Motor Driver" in labels
    assert "Revised driver with heatsink" in labels

    summary = graph.summarize(project_id)
    assert "COMPONENT" in summary and "FAILURE" in summary


def test_explain_failures_returns_linked_context() -> None:
    graph = ProjectGraph()
    project_id = graph.create_project("AMR", "autonomous mobile robot")
    motor = graph.add_node(project_id, "COMPONENT", "Motor Driver")
    failure = graph.add_node(project_id, "FAILURE", "Motor driver overheated", {"current_a": 6.2})
    graph.link(motor.node_id, failure.node_id, "failed_with")

    explanations = graph.explain_failures(project_id)
    assert explanations
    assert explanations[0]["failure"] == "Motor driver overheated"
    assert any(item["kind"] == "COMPONENT" for item in explanations[0]["related"])


def test_unknown_node_kind_rejected() -> None:
    graph = ProjectGraph()
    project_id = graph.create_project("Test")
    try:
        graph.add_node(project_id, "NOT_A_KIND", "nope")
    except ValueError as exc:
        assert "unknown node kind" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")
