"""Diagram generators (v1.2).

These produce **real SVG files**, not descriptions of diagrams. The output is
valid standalone SVG that renders in a browser or editor.

Two shapes are supported because they cover most of what people actually ask
for:

* :func:`architecture_diagram` — components arranged in tiers, linked by arrows.
* :func:`flowchart` — a sequence of steps with decisions and branches.

Inputs are simple text specifications so they can come straight from a spoken
request:

    components = "ui:Web UI:0, api:API server:1, db:Database:2"
    links      = "ui>api, api>db"

Anything unparseable is skipped and reported in the result, rather than being
invented into a box.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from xml.sax.saxutils import escape

# ---------------------------------------------------------------------------
# Layout constants
# ---------------------------------------------------------------------------
BOX_W = 168
BOX_H = 58
GAP_X = 28
GAP_Y = 78
MARGIN = 48
TITLE_H = 46

FONT = "ui-sans-serif, -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"

PALETTE = {
    "edge": "#8a8f98",
    "text": "#1f2328",
    "muted": "#5b6169",
    "canvas": "#ffffff",
}

#: Fill/stroke per tier, so layers read as layers.
TIER_STYLES = [
    ("#e7f0fe", "#2f6fdb"),
    ("#e6f4ea", "#1f7a3d"),
    ("#fdf0dc", "#9a6700"),
    ("#f0e8fb", "#6b3fa0"),
]


def _style_for(tier: int) -> Tuple[str, str]:
    return TIER_STYLES[tier % len(TIER_STYLES)]


def parse_components(spec: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Parse ``"id:Label:tier, id2:Label2:tier"`` into component dicts."""
    components: List[Dict[str, Any]] = []
    problems: List[str] = []

    for chunk in (spec or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = [p.strip() for p in chunk.split(":")]
        if len(parts) < 2:
            problems.append(f"skipped {chunk!r} (needs at least id and label)")
            continue

        node_id, label = parts[0], parts[1]
        try:
            tier = int(parts[2]) if len(parts) > 2 and parts[2] else 0
        except ValueError:
            tier = 0
            problems.append(f"{node_id}: tier {parts[2]!r} was not a number, used 0")

        components.append({"id": node_id, "label": label, "tier": max(0, tier)})

    return components, problems


def parse_links(spec: str, known: Optional[List[str]] = None) -> Tuple[List[Dict[str, str]], List[str]]:
    """Parse ``"a>b, b>c:label"`` into link dicts."""
    links: List[Dict[str, str]] = []
    problems: List[str] = []

    for chunk in (spec or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ">" not in chunk:
            problems.append(f"skipped {chunk!r} (use 'from>to')")
            continue

        left, _, right = chunk.partition(">")
        source = left.strip()
        rest = [p.strip() for p in right.split(":")]
        target = rest[0]
        label = rest[1] if len(rest) > 1 else ""

        if known is not None:
            if source not in known:
                problems.append(f"link source {source!r} is not a known component")
                continue
            if target not in known:
                problems.append(f"link target {target!r} is not a known component")
                continue

        links.append({"source": source, "target": target, "label": label})

    return links, problems


def _arrow_marker() -> str:
    return (
        '  <defs>\n'
        '    <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
        'markerHeight="7" orient="auto-start-reverse">\n'
        '      <path d="M 0 1 L 9 5 L 0 9 z" fill="{edge}"/>\n'
        '    </marker>\n'
        '  </defs>\n'.format(edge=PALETTE["edge"])
    )


def architecture_diagram(
    title: str,
    components: str,
    links: str = "",
) -> Dict[str, Any]:
    """Build a tiered architecture diagram as SVG."""
    nodes, node_problems = parse_components(components)
    if not nodes:
        return {"ok": False, "error": "I need at least one component (id:label[:tier])."}

    known = [n["id"] for n in nodes]
    edges, link_problems = parse_links(links, known=known)

    # --- layout -------------------------------------------------------------
    tiers: Dict[int, List[Dict[str, Any]]] = {}
    for node in nodes:
        tiers.setdefault(node["tier"], []).append(node)

    positions: Dict[str, Dict[str, float]] = {}
    max_width = 0
    for tier_index in sorted(tiers):
        row = tiers[tier_index]
        row_width = len(row) * BOX_W + (len(row) - 1) * GAP_X
        max_width = max(max_width, row_width)

        start_x = MARGIN + (max_width - row_width) / 2
        y = MARGIN + TITLE_H + tier_index * GAP_Y
        for column, node in enumerate(row):
            x = start_x + column * (BOX_W + GAP_X)
            positions[node["id"]] = {
                "x": x, "y": y, "label": node["label"], "tier": tier_index
            }

    height = MARGIN + TITLE_H + len(tiers) * GAP_Y + MARGIN
    width = max_width + MARGIN * 2

    # --- svg ----------------------------------------------------------------
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{int(width)}" height="{int(height)}" '
        f'viewBox="0 0 {int(width)} {int(height)}" role="img">',
        f'  <title>{escape(title)}</title>',
        f'  <desc>Architecture diagram: {escape(title)}</desc>',
        _arrow_marker(),
        f'  <rect width="100%" height="100%" fill="{PALETTE["canvas"]}"/>',
        f'  <text x="{MARGIN}" y="{MARGIN - 10}" font-family="{FONT}" font-size="17" '
        f'font-weight="600" fill="{PALETTE["text"]}">{escape(title)}</text>',
    ]

    # Edges first, so boxes draw over them.
    for edge in edges:
        source = positions.get(edge["source"])
        target = positions.get(edge["target"])
        if not source or not target:
            continue
        x1 = source["x"] + BOX_W / 2
        y1 = source["y"] + BOX_H
        x2 = target["x"] + BOX_W / 2
        y2 = target["y"]
        mid_y = y1 + (y2 - y1) / 2
        path = f"M {int(x1)} {int(y1)} C {int(x1)} {int(mid_y)}, {int(x2)} {int(mid_y)}, {int(x2)} {int(y2)}"
        parts.append(
            f'  <path d="{path}" fill="none" stroke="{PALETTE["edge"]}" stroke-width="1.5" '
            f'marker-end="url(#arrow)"/>'
        )
        if edge["label"]:
            parts.append(
                f'  <text x="{int((x1 + x2) / 2)}" y="{int(mid_y) - 5}" font-family="{FONT}" '
                f'font-size="11" fill="{PALETTE["muted"]}" text-anchor="middle">{escape(edge["label"])}</text>'
            )

    for node in nodes:
        position = positions[node["id"]]
        fill, stroke = _style_for(node["tier"])
        x, y = int(position["x"]), int(position["y"])
        parts.append(
            f'  <rect x="{x}" y="{y}" width="{BOX_W}" height="{BOX_H}" rx="9" fill="{fill}" '
            f'stroke="{stroke}" stroke-width="1.5"/>'
        )
        parts.append(
            f'  <text x="{x + BOX_W // 2}" y="{y + BOX_H // 2 + 5}" font-family="{FONT}" '
            f'font-size="13" font-weight="500" fill="{PALETTE["text"]}" text-anchor="middle">'
            f'{escape(node["label"])}</text>'
        )

    parts.append("</svg>")

    return {
        "ok": True,
        "svg": "\n".join(parts),
        "components": len(nodes),
        "links": len(edges),
        "problems": node_problems + link_problems,
        "width": int(width),
        "height": int(height),
    }


def flowchart(title: str, steps: str, decisions: str = "") -> Dict[str, Any]:
    """Build a vertical flowchart as SVG.

    ``steps`` is ``"Start, Validate, Save, Done"``. ``decisions`` marks which of
    those are decision points, e.g. ``"Validate"``.
    """
    labels = [s.strip() for s in (steps or "").split(",") if s.strip()]
    if not labels:
        return {"ok": False, "error": "I need at least one step."}

    decision_set = {d.strip().lower() for d in (decisions or "").split(",") if d.strip()}

    width = 420
    height = MARGIN + TITLE_H + len(labels) * GAP_Y + MARGIN
    centre = width / 2

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{int(height)}" '
        f'viewBox="0 0 {width} {int(height)}" role="img">',
        f'  <title>{escape(title)}</title>',
        f'  <desc>Flowchart: {escape(title)}</desc>',
        _arrow_marker(),
        f'  <rect width="100%" height="100%" fill="{PALETTE["canvas"]}"/>',
        f'  <text x="{MARGIN}" y="{MARGIN - 10}" font-family="{FONT}" font-size="17" '
        f'font-weight="600" fill="{PALETTE["text"]}">{escape(title)}</text>',
    ]

    for index, label in enumerate(labels):
        y = MARGIN + TITLE_H + index * GAP_Y
        is_decision = label.lower() in decision_set

        if index > 0:
            parts.append(
                f'  <path d="M {int(centre)} {int(y - 20)} L {int(centre)} {int(y - 2)}" '
                f'fill="none" stroke="{PALETTE["edge"]}" stroke-width="1.5" marker-end="url(#arrow)"/>'
            )

        if is_decision:
            fill, stroke = "#fdf0dc", "#9a6700"
            points = (
                f"{int(centre)},{int(y)} {int(centre + 110)},{int(y + 29)} "
                f"{int(centre)},{int(y + 58)} {int(centre - 110)},{int(y + 29)}"
            )
            parts.append(
                f'  <polygon points="{points}" fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>'
            )
        else:
            fill, stroke = "#e7f0fe", "#2f6fdb"
            parts.append(
                f'  <rect x="{int(centre - 110)}" y="{int(y)}" width="220" height="{BOX_H}" rx="9" '
                f'fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>'
            )

        parts.append(
            f'  <text x="{int(centre)}" y="{int(y + 33)}" font-family="{FONT}" font-size="13" '
            f'font-weight="500" fill="{PALETTE["text"]}" text-anchor="middle">{escape(label)}</text>'
        )

    parts.append("</svg>")
    return {
        "ok": True,
        "svg": "\n".join(parts),
        "steps": len(labels),
        "problems": [],
        "width": width,
        "height": int(height),
    }
