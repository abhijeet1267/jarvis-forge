"""Design tools (v1.2).

Generates real artifacts — valid SVG diagrams, self-contained HTML mockups and
decks, and Markdown documentation — and keeps them as linkable assets.

Honesty rules carried through from the rest of the system:

* **Outlines are labelled as outlines.** Generated docs and decks contain
  placeholders and say so, rather than looking finished.
* **Every asset records its origin.** ``created_by`` is ``local`` unless an
  external integration produced it; nothing is passed off as coming from a tool
  that was not used.
* **Output stays in JARVIS's own design directory.** No arbitrary write paths.
* **Delete only removes assets this store created**, addressed by id.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..core.models import RiskLevel, ToolSpec
from ..logging_setup import get_logger
from ..design.assets import DesignAssetStore
from ..design.deck import presentation
from ..design.diagrams import architecture_diagram, flowchart
from ..design.docs import documentation
from ..design.integrations import describe_integrations, usable_integrations
from ..design.review import analyze
from ..design.ui import ui_concept
from .base import ToolContext, fail, ok

log = get_logger("tools.design")


def _store(settings=None) -> DesignAssetStore:
    return DesignAssetStore(settings)


def _finish(ctx: ToolContext, store: DesignAssetStore, asset, extra: Dict[str, Any]) -> Any:
    """Build the success result for a generated asset."""
    return ok(
        asset.kind + "_created",
        f"Created {asset.kind}: {asset.title} ({asset.format}) — {asset.filename}",
        device=ctx.device_id,
        asset=asset.as_dict(),
        path=str(store.path_of(asset)),
        **extra,
    )


# ---------------------------------------------------------------------------
# Diagram
# ---------------------------------------------------------------------------
async def create_architecture_diagram(
    ctx: ToolContext,
    title: str = "",
    components: str = "",
    links: str = "",
) -> Any:
    """Build an architecture diagram as SVG."""
    if not title:
        return fail(
            "create_architecture_diagram", "What should I call the diagram?", device=ctx.device_id
        )
    if not components:
        return fail(
            "create_architecture_diagram",
            "Which components? Use 'id:Label:tier' separated by commas.",
            device=ctx.device_id,
        )

    result = architecture_diagram(title, components, links)
    if not result["ok"]:
        return fail("create_architecture_diagram", result["error"], device=ctx.device_id)

    store = _store(ctx.settings)
    asset = store.save(
        title, "diagram", result["svg"], format="svg",
        summary=f"{result['components']} component(s), {result['links']} link(s)",
    )
    return _finish(
        ctx, store, asset,
        {"problems": result["problems"], "components": result["components"], "links": result["links"]},
    )


async def create_flowchart(
    ctx: ToolContext,
    title: str = "",
    steps: str = "",
    decisions: str = "",
) -> Any:
    """Build a flowchart as SVG."""
    if not title:
        return fail("create_flowchart", "What should I call the diagram?", device=ctx.device_id)
    if not steps:
        return fail(
            "create_flowchart",
            "Which steps? Give me them in order, separated by commas.",
            device=ctx.device_id,
        )

    result = flowchart(title, steps, decisions)
    if not result["ok"]:
        return fail("create_flowchart", result["error"], device=ctx.device_id)

    store = _store(ctx.settings)
    asset = store.save(title, "diagram", result["svg"], format="svg", summary=f"{result['steps']} step(s)")
    return _finish(ctx, store, asset, {"steps": result["steps"]})


# ---------------------------------------------------------------------------
# UI, docs, deck
# ---------------------------------------------------------------------------
async def create_ui_concept(
    ctx: ToolContext,
    title: str = "",
    sections: str = "",
    notes: str = "",
) -> Any:
    """Build a UI wireframe as self-contained HTML."""
    if not title:
        return fail("create_ui_concept", "What should I call the concept?", device=ctx.device_id)

    result = ui_concept(title, sections, notes)
    if not result["ok"]:
        return fail("create_ui_concept", result["error"], device=ctx.device_id)

    store = _store(ctx.settings)
    asset = store.save(
        title, "ui_concept", result["html"], format="html",
        summary="Wireframe: " + ", ".join(result["sections"]),
    )
    return _finish(
        ctx, store, asset, {"sections": result["sections"], "unknown": result["unknown"]}
    )


async def create_documentation(
    ctx: ToolContext,
    title: str = "",
    sections: str = "",
    audience: str = "",
) -> Any:
    """Build a Markdown document outline."""
    if not title:
        return fail("create_documentation", "What is the document called?", device=ctx.device_id)

    result = documentation(title, sections, audience)
    if not result["ok"]:
        return fail("create_documentation", result["error"], device=ctx.device_id)

    store = _store(ctx.settings)
    asset = store.save(
        title, "documentation", result["markdown"], format="markdown", extension="md",
        summary="Outline: " + ", ".join(result["sections"]),
    )
    return _finish(ctx, store, asset, {"sections": result["sections"]})


async def create_presentation(
    ctx: ToolContext,
    title: str = "",
    slides: str = "",
    theme: str = "light",
) -> Any:
    """Build a slide deck as self-contained HTML."""
    if not title:
        return fail("create_presentation", "What is the presentation called?", device=ctx.device_id)

    result = presentation(title, slides, theme)
    if not result["ok"]:
        return fail("create_presentation", result["error"], device=ctx.device_id)

    store = _store(ctx.settings)
    asset = store.save(
        title, "presentation", result["html"], format="html",
        summary="Deck: " + ", ".join(result["slides"]),
    )
    return _finish(ctx, store, asset, {"slides": result["slides"]})


# ---------------------------------------------------------------------------
# Manage
# ---------------------------------------------------------------------------
async def list_design_assets(
    ctx: ToolContext, project_id: str = "", kind: str = ""
) -> Any:
    """List generated assets."""
    store = _store(ctx.settings)
    assets = store.all(project_id=project_id or None, kind=kind or None)
    if not assets:
        return ok(
            "list_design_assets", "No design assets yet.", device=ctx.device_id, assets=[]
        )
    return ok(
        "list_design_assets",
        f"{len(assets)} asset(s).",
        device=ctx.device_id,
        assets=[a.as_dict() for a in assets],
        stats=store.stats(),
    )


async def read_design_asset(ctx: ToolContext, asset_id: str = "") -> Any:
    """Read a generated asset's contents."""
    if not asset_id:
        return fail("read_design_asset", "Which asset should I read?", device=ctx.device_id)

    store = _store(ctx.settings)
    content = store.read(asset_id)
    if content is None:
        return fail("read_design_asset", f"No asset {asset_id!r}.", device=ctx.device_id)

    asset = store.get(asset_id)
    return ok(
        "read_design_asset",
        f"{asset.title} ({asset.format}, {len(content)} bytes)",
        device=ctx.device_id,
        asset=asset.as_dict(),
        content=content[:8000],
    )


async def analyze_design(ctx: ToolContext, asset_id: str = "") -> Any:
    """Review an asset's measurable properties."""
    if not asset_id:
        return fail("analyze_design", "Which asset should I analyze?", device=ctx.device_id)

    store = _store(ctx.settings)
    asset = store.get(asset_id)
    if asset is None:
        return fail("analyze_design", f"No asset {asset_id!r}.", device=ctx.device_id)

    content = store.read(asset_id)
    if content is None:
        return fail("analyze_design", "I could not read that asset.", device=ctx.device_id)

    result = analyze(content, asset.format, asset.title)
    if not result["ok"]:
        return fail("analyze_design", result["error"], device=ctx.device_id)

    lines = [f"{asset.title} — {result['summary']}"]
    for finding in result["findings"]:
        lines.append(f"  [{finding['severity']}] {finding['check']}: {finding['detail']}")
    return ok("analyze_design", "\n".join(lines), device=ctx.device_id, analysis=result)


async def link_asset_to_project(
    ctx: ToolContext, asset_id: str = "", project_id: str = ""
) -> Any:
    """Connect an asset to a project in the graph."""
    if not asset_id or not project_id:
        return fail(
            "link_asset_to_project", "I need both an asset id and a project id.", device=ctx.device_id
        )

    store = _store(ctx.settings)
    asset = store.get(asset_id)
    if asset is None:
        return fail("link_asset_to_project", f"No asset {asset_id!r}.", device=ctx.device_id)

    store.link_project(asset_id, project_id)
    node = ctx.graph.add_node(
        project_id=project_id,
        kind="design_asset",
        label=asset.title,
        data={"asset_id": asset_id, "asset_kind": asset.kind, "format": asset.format},
    )
    return ok(
        "link_asset_to_project",
        f"Linked {asset.title} to {project_id}.",
        device=ctx.device_id,
        asset_id=asset_id,
        project_id=project_id,
        node=node,
    )


async def delete_design_asset(ctx: ToolContext, asset_id: str = "") -> Any:
    """Delete an asset JARVIS created."""
    if not asset_id:
        return fail("delete_design_asset", "Which asset should I delete?", device=ctx.device_id)

    store = _store(ctx.settings)
    asset = store.get(asset_id)
    if asset is None:
        return fail("delete_design_asset", f"No asset {asset_id!r}.", device=ctx.device_id)

    title = asset.title
    store.forget(asset_id)
    return ok("delete_design_asset", f"Deleted {title}.", device=ctx.device_id, asset_id=asset_id)


async def design_integrations(ctx: ToolContext) -> Any:
    """Report which external design tools are available."""
    info = describe_integrations(ctx.settings)
    usable = [i["name"] for i in info["integrations"] if i["usable"]]
    return ok(
        "design_integrations",
        (
            f"Generating locally. External tools available: {', '.join(usable) if usable else 'none'}."
        ),
        device=ctx.device_id,
        integrations=info,
    )


SPECS = [
    (
        ToolSpec(
            name="create_architecture_diagram",
            description="Create a tiered architecture diagram as SVG.",
            agent="design",
            risk=RiskLevel.LOW,
            reversible=True,
            allowed_devices=["laptop_main"],
            parameters={"title": "string", "components": "string", "links": "string"},
            required_parameters=["title", "components"],
            tags=["design", "diagram"],
        ),
        create_architecture_diagram,
    ),
    (
        ToolSpec(
            name="create_flowchart",
            description="Create a flowchart as SVG.",
            agent="design",
            risk=RiskLevel.LOW,
            reversible=True,
            allowed_devices=["laptop_main"],
            parameters={"title": "string", "steps": "string", "decisions": "string"},
            required_parameters=["title", "steps"],
            tags=["design", "diagram"],
        ),
        create_flowchart,
    ),
    (
        ToolSpec(
            name="create_ui_concept",
            description="Create a UI wireframe as self-contained HTML.",
            agent="design",
            risk=RiskLevel.LOW,
            reversible=True,
            allowed_devices=["laptop_main"],
            parameters={"title": "string", "sections": "string", "notes": "string"},
            required_parameters=["title"],
            tags=["design", "ui"],
        ),
        create_ui_concept,
    ),
    (
        ToolSpec(
            name="create_documentation",
            description="Create a Markdown document outline.",
            agent="design",
            risk=RiskLevel.LOW,
            reversible=True,
            allowed_devices=["laptop_main"],
            parameters={"title": "string", "sections": "string", "audience": "string"},
            required_parameters=["title"],
            tags=["design", "docs"],
        ),
        create_documentation,
    ),
    (
        ToolSpec(
            name="create_presentation",
            description="Create a slide deck as self-contained HTML.",
            agent="design",
            risk=RiskLevel.LOW,
            reversible=True,
            allowed_devices=["laptop_main"],
            parameters={"title": "string", "slides": "string", "theme": "string"},
            required_parameters=["title"],
            tags=["design", "deck"],
        ),
        create_presentation,
    ),
    (
        ToolSpec(
            name="list_design_assets",
            description="List generated design assets.",
            agent="design",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={"project_id": "string", "kind": "string"},
            tags=["design", "read"],
        ),
        list_design_assets,
    ),
    (
        ToolSpec(
            name="read_design_asset",
            description="Read a generated asset.",
            agent="design",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={"asset_id": "string"},
            required_parameters=["asset_id"],
            tags=["design", "read"],
        ),
        read_design_asset,
    ),
    (
        ToolSpec(
            name="analyze_design",
            description="Review an asset's measurable properties.",
            agent="design",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={"asset_id": "string"},
            required_parameters=["asset_id"],
            tags=["design", "read"],
        ),
        analyze_design,
    ),
    (
        ToolSpec(
            name="link_asset_to_project",
            description="Connect an asset to a project in the graph.",
            agent="design",
            risk=RiskLevel.LOW,
            reversible=True,
            allowed_devices=["laptop_main"],
            parameters={"asset_id": "string", "project_id": "string"},
            required_parameters=["asset_id", "project_id"],
            tags=["design", "graph"],
        ),
        link_asset_to_project,
    ),
    (
        ToolSpec(
            name="design_integrations",
            description="Report available external design tools.",
            agent="design",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={},
            tags=["design", "read"],
        ),
        design_integrations,
    ),
    (
        ToolSpec(
            name="delete_design_asset",
            description="Delete an asset JARVIS created.",
            agent="design",
            risk=RiskLevel.DESTRUCTIVE,
            requires_confirmation=True,
            reversible=False,
            allowed_devices=["laptop_main"],
            parameters={"asset_id": "string"},
            required_parameters=["asset_id"],
            tags=["design", "destructive"],
        ),
        delete_design_asset,
    ),
]
