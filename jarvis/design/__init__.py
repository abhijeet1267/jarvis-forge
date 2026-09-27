"""Design Agent support (v1.2).

Generates UI concepts, architecture and system diagrams, documentation and
presentations, and maintains the resulting assets.

What it produces is **real**: valid standalone SVG, self-contained HTML, and
real Markdown. What it does not do is invent content — outlines come with
placeholders and say so, and an artifact is always labelled with whether it was
generated locally or came from an external tool.

External integrations are permissioned and off by default.
"""

from .assets import DesignAsset, DesignAssetStore, safe_filename
from .deck import presentation
from .diagrams import architecture_diagram, flowchart, parse_components, parse_links
from .docs import documentation
from .integrations import describe_integrations, get_integration, usable_integrations
from .review import analyze
from .ui import ui_concept

__all__ = [
    "DesignAsset",
    "DesignAssetStore",
    "safe_filename",
    "architecture_diagram",
    "flowchart",
    "parse_components",
    "parse_links",
    "ui_concept",
    "documentation",
    "presentation",
    "analyze",
    "describe_integrations",
    "get_integration",
    "usable_integrations",
]
