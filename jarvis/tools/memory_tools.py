"""Memory tools (sections 4.10, 5, 30).

Exposes the flat memory store and the project-state graph as callable tools so
the orchestrator can persist and recall facts through the same permissioned
path as everything else.
"""

from __future__ import annotations

from typing import Any

from ..core.models import MemoryScope, PrivacyLevel, RiskLevel, ToolSpec
from ..logging_setup import get_logger
from .base import ToolContext, fail, ok

log = get_logger("tools.memory")


async def remember_fact(
    ctx: ToolContext,
    content: str = "",
    scope: str = "long_term",
    project: str = "",
    privacy: str = "internal",
) -> Any:
    """Store a durable fact about the user or their projects."""
    if not content:
        return fail("remember_fact", "There is nothing to remember.", device=ctx.device_id)

    project_id = None
    if project:
        project_id = ctx.graph.get_or_create_project(project)

    try:
        scope_enum = MemoryScope(scope)
    except ValueError:
        scope_enum = MemoryScope.LONG_TERM
    try:
        privacy_enum = PrivacyLevel(privacy)
    except ValueError:
        privacy_enum = PrivacyLevel.INTERNAL

    item = ctx.memory.remember(
        content,
        scope=scope_enum,
        source="user",
        project_id=project_id,
        privacy=privacy_enum,
    )
    return ok(
        "remember_fact",
        f"Got it. I will remember: {content}",
        device=ctx.device_id,
        memory_id=item.id,
        scope=scope_enum.value,
        project_id=project_id,
    )


async def search_memory(ctx: ToolContext, query: str = "") -> Any:
    """Recall stored facts relevant to a query."""
    if not query:
        return fail("search_memory", "What should I look for?", device=ctx.device_id)

    # v0.9: hybrid retrieval (keyword + vector, fused with RRF). If the vector
    # side is unavailable this degrades to keyword search and says so.
    from ..memory.hybrid import hybrid_search

    outcome = hybrid_search(query, limit=5, store=ctx.memory)
    ranked = outcome["results"]
    retrieval = outcome["retrieval"]

    if not ranked:
        return ok(
            "search_memory",
            f"I have nothing stored about {query!r} yet.",
            device=ctx.device_id,
            results=[],
            retrieval=retrieval,
        )

    results = [
        {
            "id": h.item.id,
            "content": h.item.content,
            "scope": h.item.scope.value,
            "type": h.item.memory_type.value,
            "project_id": h.item.project_id,
            "confidence": h.item.confidence,
            "found_by": [
                name
                for name, rank in (("keyword", h.keyword_rank), ("vector", h.vector_rank))
                if rank is not None
            ],
        }
        for h in ranked
    ]
    top = results[0]["content"]
    return ok(
        "search_memory",
        f"Here is what I remember: {top}",
        device=ctx.device_id,
        query=query,
        results=results,
    )


async def create_project(ctx: ToolContext, name: str = "", description: str = "") -> Any:
    """Create or fetch a project in the project-state graph."""
    if not name:
        return fail("create_project", "I need a project name.", device=ctx.device_id)
    project_id = ctx.graph.get_or_create_project(name, description)
    return ok(
        "create_project",
        f"Project {name!r} is ready.",
        device=ctx.device_id,
        project_id=project_id,
        name=name,
    )


async def add_project_node(
    ctx: ToolContext,
    project: str = "",
    kind: str = "",
    label: str = "",
    detail: str = "",
) -> Any:
    """Add a node (COMPONENT, EXPERIMENT, FAILURE, FIX, ...) to a project."""
    if not (project and kind and label):
        return fail(
            "add_project_node",
            "I need a project, a node kind and a label.",
            device=ctx.device_id,
        )
    project_id = ctx.graph.get_or_create_project(project)
    try:
        node = ctx.graph.add_node(project_id, kind, label, {"detail": detail} if detail else {})
    except ValueError as exc:
        return fail("add_project_node", str(exc), device=ctx.device_id)

    return ok(
        "add_project_node",
        f"Linked {kind.upper()} {label!r} to project {project!r}.",
        device=ctx.device_id,
        node_id=node.node_id,
        project_id=project_id,
    )


async def explain_failures(ctx: ToolContext, project: str = "") -> Any:
    """Explain previously recorded failures for a project."""
    if not project:
        return fail("explain_failures", "Which project?", device=ctx.device_id)

    record = ctx.graph.find_project(project)
    if record is None:
        return fail("explain_failures", f"I have no project called {project!r}.", device=ctx.device_id)

    failures = ctx.graph.explain_failures(record["project_id"])
    if not failures:
        return ok(
            "explain_failures",
            f"I have no recorded failures for {record['name']}.",
            device=ctx.device_id,
            failures=[],
        )

    first = failures[0]
    related = ", ".join(f"{r['kind']}: {r['label']}" for r in first["related"]) or "nothing linked"
    return ok(
        "explain_failures",
        f"Last recorded failure in {record['name']}: {first['failure']}. Linked context: {related}.",
        device=ctx.device_id,
        failures=failures,
    )


async def project_summary(ctx: ToolContext, project: str = "") -> Any:
    """Summarise a project's graph."""
    record = ctx.graph.find_project(project) if project else None
    if record is None:
        projects = ctx.graph.list_projects()
        if not projects:
            return ok(
                "project_summary",
                "You have no projects tracked yet.",
                device=ctx.device_id,
                projects=[],
            )
        record = projects[0]

    summary = ctx.graph.summarize(record["project_id"])
    parts = [f"{kind.lower()}: {len(items)}" for kind, items in summary.items()]
    return ok(
        "project_summary",
        f"Project {record['name']} — " + (", ".join(parts) if parts else "no linked artefacts yet."),
        device=ctx.device_id,
        project_id=record["project_id"],
        summary=summary,
    )


SPECS = [
    (
        ToolSpec(
            name="remember_fact",
            description="Store a durable fact about the user or a project.",
            risk=RiskLevel.LOW,
            parameters={"content": "string", "scope": "string", "project": "string", "privacy": "string"},
            required_parameters=["content"],
            agent="memory",
            tags=["memory", "write"],
        ),
        remember_fact,
    ),
    (
        ToolSpec(
            name="search_memory",
            description="Recall stored facts relevant to a query.",
            risk=RiskLevel.READ,
            parameters={"query": "string"},
            required_parameters=["query"],
            agent="memory",
            tags=["memory", "read"],
        ),
        search_memory,
    ),
    (
        ToolSpec(
            name="create_project",
            description="Create or fetch a project in the project-state graph.",
            risk=RiskLevel.LOW,
            parameters={"name": "string", "description": "string"},
            required_parameters=["name"],
            agent="memory",
            tags=["memory", "project"],
        ),
        create_project,
    ),
    (
        ToolSpec(
            name="add_project_node",
            description="Link a component/experiment/failure/fix to a project.",
            risk=RiskLevel.LOW,
            parameters={"project": "string", "kind": "string", "label": "string", "detail": "string"},
            required_parameters=["project", "kind", "label"],
            agent="memory",
            tags=["memory", "project"],
        ),
        add_project_node,
    ),
    (
        ToolSpec(
            name="explain_failures",
            description="Explain previously recorded failures for a project.",
            risk=RiskLevel.READ,
            parameters={"project": "string"},
            required_parameters=["project"],
            agent="memory",
            tags=["memory", "read"],
        ),
        explain_failures,
    ),
    (
        ToolSpec(
            name="project_summary",
            description="Summarise the linked artefacts of a project.",
            risk=RiskLevel.READ,
            parameters={"project": "string"},
            agent="memory",
            tags=["memory", "read"],
        ),
        project_summary,
    ),
]
