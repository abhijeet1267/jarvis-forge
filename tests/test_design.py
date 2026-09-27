"""Design Agent tests (v1.2).

The claim being tested throughout: **the artifacts are real**. A "diagram" that
is not parseable SVG, or an "HTML" file that is not HTML, would be exactly the
kind of thing this agent must not produce.
"""

from __future__ import annotations

import html.parser
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from jarvis.core.models import PlannedAction, PolicyDecision, RiskLevel
from jarvis.design.assets import DesignAssetStore, safe_filename
from jarvis.design.deck import presentation
from jarvis.design.diagrams import architecture_diagram, flowchart, parse_components, parse_links
from jarvis.design.docs import documentation
from jarvis.design.integrations import describe_integrations, get_integration
from jarvis.design.review import analyze
from jarvis.design.ui import KNOWN_SECTIONS, ui_concept
from jarvis.tools.base import ToolContext
from jarvis.tools.design import (
    analyze_design,
    create_architecture_diagram,
    create_documentation,
    create_flowchart,
    create_presentation,
    create_ui_concept,
    delete_design_asset,
    design_integrations,
    list_design_assets,
    read_design_asset,
)

SVG_NS = "{http://www.w3.org/2000/svg}"


class _HTMLValidator(html.parser.HTMLParser):
    """Minimal well-formedness check: every start tag has a matching end tag."""

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[str] = []
        self.errors: list[str] = []

    VOID = {"meta", "br", "img", "input", "hr", "link"}

    def handle_startendtag(self, tag, attrs):
        # <foo /> is balanced by definition; the default implementation calls
        # both handlers, which would falsely report an end tag for void elements.
        pass

    def handle_starttag(self, tag, attrs):
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack:
            self.errors.append(f"stray </{tag}>")
        elif self.stack[-1] != tag:
            self.errors.append(f"expected </{self.stack[-1]}>, got </{tag}>")
        else:
            self.stack.pop()


def assert_valid_html(content: str) -> None:
    parser = _HTMLValidator()
    parser.feed(content)
    assert not parser.errors, parser.errors
    assert not parser.stack, f"unclosed tags: {parser.stack}"


@pytest.fixture()
def ctx() -> ToolContext:
    return ToolContext()


# ---------------------------------------------------------------------------
# Diagrams: real SVG
# ---------------------------------------------------------------------------
def test_architecture_diagram_is_parseable_svg() -> None:
    result = architecture_diagram(
        "Pipeline", "ui:Web UI:0, api:API:1, db:DB:2", "ui>api, api>db"
    )
    assert result["ok"]
    root = ET.fromstring(result["svg"])
    assert root.tag == SVG_NS + "svg"
    # One box per component, plus the canvas.
    assert len(root.findall(f".//{SVG_NS}rect")) == 4
    # Two links, plus the arrowhead definition inside <defs>.
    assert len(root.findall(f".//{SVG_NS}path")) == 3
    assert root.find(f".//{SVG_NS}title") is not None


def test_diagram_declares_the_svg_namespace() -> None:
    result = architecture_diagram("X", "a:A:0")
    assert 'xmlns="http://www.w3.org/2000/svg"' in result["svg"]


def test_diagram_escapes_special_characters() -> None:
    result = architecture_diagram("A <b> & c", "x:X & Y:0")
    ET.fromstring(result["svg"])  # would raise if unescaped
    assert "&lt;b&gt;" in result["svg"]


def test_diagram_reports_unparseable_input_instead_of_inventing() -> None:
    result = architecture_diagram("X", "a:Alpha:0, broken", "a>ghost")
    assert result["ok"]
    assert any("broken" in p for p in result["problems"])
    assert any("ghost" in p for p in result["problems"])
    assert result["components"] == 1


def test_diagram_needs_at_least_one_component() -> None:
    assert architecture_diagram("X", "")["ok"] is False
    assert architecture_diagram("X", "nonsense")["ok"] is False


def test_component_and_link_parsers() -> None:
    components, problems = parse_components("a:Alpha:0, b:Beta:2, bad")
    assert len(components) == 2
    assert components[1]["tier"] == 2
    assert problems

    links, link_problems = parse_links("a>b, b>c:label, malformed", known=["a", "b", "c"])
    assert len(links) == 2
    assert links[1]["label"] == "label"
    assert link_problems


def test_flowchart_is_parseable_svg_with_decisions() -> None:
    result = flowchart("Deploy", "Build, Test, Review, Ship", decisions="Review")
    assert result["ok"]
    root = ET.fromstring(result["svg"])
    assert len(root.findall(f".//{SVG_NS}polygon")) == 1  # the decision diamond
    assert len(root.findall(f".//{SVG_NS}rect")) == 4     # three steps + canvas


def test_flowchart_needs_steps() -> None:
    assert flowchart("X", "")["ok"] is False


# ---------------------------------------------------------------------------
# UI, docs, deck: real HTML and Markdown
# ---------------------------------------------------------------------------
def test_ui_concept_is_valid_self_contained_html() -> None:
    result = ui_concept("Dashboard", "header,hero,cards")
    assert result["ok"]
    assert_valid_html(result["html"])
    assert "https://" not in result["html"], "must work offline"
    assert "Wireframe" in result["html"]


def test_ui_concept_reports_unknown_sections() -> None:
    result = ui_concept("X", "header,telepathy")
    assert result["unknown"] == ["telepathy"]
    assert "header" in result["sections"]


def test_ui_concept_defaults_to_a_sensible_layout() -> None:
    result = ui_concept("X", "")
    assert result["sections"]
    assert set(result["sections"]) <= KNOWN_SECTIONS


def test_ui_concept_escapes_the_title() -> None:
    result = ui_concept("A <script>alert(1)</script>")
    assert "<script>alert(1)</script>" not in result["html"]


def test_documentation_is_real_markdown() -> None:
    result = documentation("Guide", "Overview, Setup")
    assert result["ok"]
    assert result["markdown"].startswith("# Guide")
    assert "## Overview" in result["markdown"]
    assert "## Setup" in result["markdown"]
    assert "_To be written._" in result["markdown"], "says it is a skeleton"


def test_documentation_says_it_is_an_outline() -> None:
    result = documentation("Guide")
    assert "generated outline" in result["markdown"].lower()


def test_presentation_is_valid_html_deck() -> None:
    result = presentation("Update", "Context, Results")
    assert result["ok"]
    assert_valid_html(result["html"])
    assert "class=\"slide\"" in result["html"]
    assert "ArrowRight" in result["html"], "keyboard navigation"


def test_presentation_supports_a_dark_theme() -> None:
    dark = presentation("X", theme="dark")
    assert "#111418" in dark["html"]
    assert "color-scheme: dark" in dark["html"]


def test_presentation_defaults_to_standard_slides() -> None:
    assert presentation("X")["slides"]


# ---------------------------------------------------------------------------
# Asset store
# ---------------------------------------------------------------------------
def test_assets_stay_inside_the_design_directory(tmp_area: Path, monkeypatch) -> None:
    store = DesignAssetStore()
    asset = store.save("Test", "documentation", "# Test", format="markdown", extension="md")

    path = store.path_of(asset)
    assert path is not None
    # Compare resolved paths: on macOS /tmp resolves to /private/tmp, so a
    # naive string prefix check would fail.
    assert store.directory.resolve() in path.resolve().parents


def test_safe_filename_strips_unsafe_characters() -> None:
    assert "/" not in safe_filename("a/b", "svg")
    assert safe_filename("", "html").startswith("asset-")


def test_read_and_delete_round_trip() -> None:
    store = DesignAssetStore()
    asset = store.save("Doc", "documentation", "# Doc", format="markdown", extension="md")

    assert store.read(asset.asset_id) == "# Doc"
    assert store.forget(asset.asset_id)
    assert store.read(asset.asset_id) is None


def test_a_tampered_filename_is_refused() -> None:
    """A filename edited to escape the design dir must not resolve to a path."""
    store = DesignAssetStore()
    asset = store.save("Esc", "documentation", "x", format="markdown", extension="md")
    asset.filename = "../../etc/passwd"

    assert store.path_of(asset) is None


def test_assets_can_be_filtered_by_kind() -> None:
    store = DesignAssetStore()
    store.save("Doc", "documentation", "x", format="markdown", extension="md")
    assert len(store.all(kind="documentation")) >= 1
    assert len(store.all(kind="diagram")) == 0


# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------
def test_review_of_an_svg_asset() -> None:
    svg = architecture_diagram("X", "a:A:0")["svg"]
    result = analyze(svg, "svg", "X")
    assert result["ok"]
    assert result["warnings"] == 0  # our own output has title and namespace


def test_review_flags_a_missing_title() -> None:
    result = analyze('<svg xmlns="http://www.w3.org/2000/svg"></svg>', "svg", "X")
    assert result["warnings"] >= 1
    assert any(f["check"] == "accessible title" for f in result["findings"])


def test_review_of_html_and_markdown() -> None:
    deck = presentation("X")["html"]
    assert analyze(deck, "html", "X")["ok"]

    doc = documentation("X")["markdown"]
    markdown_result = analyze(doc, "markdown", "X")
    assert markdown_result["ok"]
    assert any(f["check"] == "completeness" for f in markdown_result["findings"])


def test_review_refuses_an_unknown_format() -> None:
    assert analyze("x", "psd")["ok"] is False


# ---------------------------------------------------------------------------
# Integrations
# ---------------------------------------------------------------------------
def test_no_integration_is_usable_by_default() -> None:
    info = describe_integrations()
    assert info["default_backend"] == "local"
    assert all(not i["usable"] for i in info["integrations"])


def test_unknown_integration_resolves_to_nothing() -> None:
    assert get_integration("made-up-tool") is None


def test_integration_policy_is_stated() -> None:
    assert "configured AND enabled" in describe_integrations()["policy"]


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_create_architecture_diagram_tool(ctx: ToolContext) -> None:
    result = await create_architecture_diagram(
        ctx, title="Pipeline", components="ui:UI:0, api:API:1", links="ui>api"
    )
    assert result.ok
    assert result.output["asset"]["format"] == "svg"
    # The artifact really exists on disk.
    assert Path(result.output["path"]).is_file()
    ET.fromstring(Path(result.output["path"]).read_text())


@pytest.mark.asyncio
async def test_create_flowchart_tool(ctx: ToolContext) -> None:
    result = await create_flowchart(ctx, title="Flow", steps="One, Two")
    assert result.ok


@pytest.mark.asyncio
async def test_create_ui_concept_tool(ctx: ToolContext) -> None:
    result = await create_ui_concept(ctx, title="Screen", sections="header,hero")
    assert result.ok
    assert_valid_html(Path(result.output["path"]).read_text())


@pytest.mark.asyncio
async def test_create_documentation_tool(ctx: ToolContext) -> None:
    result = await create_documentation(ctx, title="Guide", sections="A, B")
    assert result.ok
    assert result.output["asset"]["format"] == "markdown"


@pytest.mark.asyncio
async def test_create_presentation_tool(ctx: ToolContext) -> None:
    result = await create_presentation(ctx, title="Deck", slides="One, Two")
    assert result.ok


@pytest.mark.parametrize(
    "tool",
    [create_architecture_diagram, create_flowchart, create_ui_concept,
     create_documentation, create_presentation],
)
@pytest.mark.asyncio
async def test_every_generator_needs_a_title(ctx: ToolContext, tool) -> None:
    assert not (await tool(ctx)).ok


@pytest.mark.asyncio
async def test_list_read_and_analyze_tools(ctx: ToolContext) -> None:
    created = await create_architecture_diagram(ctx, title="Listed", components="a:A:0")
    asset_id = created.output["asset"]["asset_id"]

    assert (await list_design_assets(ctx)).ok
    assert (await read_design_asset(ctx, asset_id=asset_id)).ok
    assert (await analyze_design(ctx, asset_id=asset_id)).ok


@pytest.mark.asyncio
async def test_read_unknown_asset_fails(ctx: ToolContext) -> None:
    assert not (await read_design_asset(ctx, asset_id="ASSET-nope")).ok


@pytest.mark.asyncio
async def test_integrations_tool_reports_local_generation(ctx: ToolContext) -> None:
    result = await design_integrations(ctx)
    assert result.ok
    assert "Generating locally" in result.message


@pytest.mark.asyncio
async def test_delete_tool_removes_the_asset(ctx: ToolContext) -> None:
    created = await create_documentation(ctx, title="Doomed")
    asset_id = created.output["asset"]["asset_id"]

    result = await delete_design_asset(ctx, asset_id=asset_id)
    assert result.ok
    assert not (await read_design_asset(ctx, asset_id=asset_id)).ok


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------
def test_deleting_an_asset_is_destructive_and_confirmed(core) -> None:
    spec = core.tools.get("delete_design_asset")
    assert spec.risk is RiskLevel.DESTRUCTIVE
    assert spec.requires_confirmation is True

    action = PlannedAction(
        tool="delete_design_asset", params={"asset_id": "ASSET-1"}, device="laptop_main"
    )
    assert core.policy.evaluate(action).decision is PolicyDecision.CONFIRM


@pytest.mark.parametrize(
    "tool,params",
    [
        ("list_design_assets", {}),
        ("read_design_asset", {"asset_id": "ASSET-1"}),
        ("analyze_design", {"asset_id": "ASSET-1"}),
        ("design_integrations", {}),
        ("create_ui_concept", {"title": "X"}),
    ],
)
def test_reading_and_generating_are_not_gated(core, tool: str, params: dict) -> None:
    action = PlannedAction(tool=tool, params=params, device="laptop_main")
    assert core.policy.evaluate(action).decision is PolicyDecision.EXECUTE


def test_design_agent_is_registered(core) -> None:
    assert "design" in core.agents
    assert core.agents["design"].owns("create_architecture_diagram")
