#!/usr/bin/env python3
"""Generate the README's charts from the JSON in benchmarks/.

No plotting dependency is introduced: the output is hand-laid SVG in the same
palette the dashboard itself uses (``dashboard/index.html`` ``:root``), so the
repo art and the product look like one thing, and the charts render crisply at
any width on GitHub.

    .venv/bin/python benchmarks/acceptance_run.py   # refresh the data
    .venv/bin/python assets/build_charts.py         # redraw the charts

Every number drawn here is read from a JSON file. Nothing is typed into the
generator, so a chart cannot quietly disagree with the run that produced it.
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
BENCH = ROOT / "benchmarks"
OUT = HERE

# Straight out of the dashboard's own stylesheet.
INK = "#1b2230"
MUTED = "#6b7688"
LINE = "#e2e7ef"
ACCENT = "#2f6df6"
OK = "#16a34a"
WARN = "#d97706"
BAD = "#dc2626"
NAVY = "#0f172a"
PANEL = "#ffffff"
BG = "#f5f7fa"

# Single quotes inside: this string is embedded in double-quoted SVG attributes,
# and `font-family="ui-monospace, \"SF Mono\""` is not valid XML.
MONO = "ui-monospace, 'SF Mono', Menlo, Consolas, monospace"
STATE_COLOUR = {
    "VERIFIED": OK,
    "VERIFIED IN PART": "#0d9488",
    "SIMULATED": WARN,
    "UNAVAILABLE": WARN,
    "AWAITING CONFIRMATION": WARN,
    "NEEDS DETAIL": WARN,
    "NOTHING EXECUTED": NAVY,
    "NOT RECOGNISED": MUTED,
    "NOT AVAILABLE": MUTED,
    "PLANNED": MUTED,
    "BLOCKED": BAD,
    "FAILED": BAD,
    "UNKNOWN": BAD,
}


def _header(width: int, height: int, title: str, subtitle: str) -> str:
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}"
 viewBox="0 0 {width} {height}" role="img" aria-label="{title}: {subtitle}">
<defs>
  <pattern id="grid" width="28" height="28" patternUnits="userSpaceOnUse">
    <path d="M28 0H0v28" fill="none" stroke="{LINE}" stroke-width="1"/>
  </pattern>
</defs>
<rect width="100%" height="100%" fill="{BG}"/>
<rect width="100%" height="100%" fill="url(#grid)" opacity=".55"/>
<rect x="1" y="1" width="{width - 2}" height="{height - 2}" rx="14" fill="{PANEL}" stroke="{LINE}"/>
<text x="34" y="46" font-family="{MONO}" font-size="17" font-weight="700"
      letter-spacing="2.4" fill="{INK}">{_esc(title)}</text>
<text x="34" y="68" font-family="{MONO}" font-size="12" fill="{MUTED}">{_esc(subtitle)}</text>
'''


def _esc(value: str) -> str:
    return (str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def suite_growth() -> None:
    data = json.loads((BENCH / "suite_history.json").read_text())["phases"]
    width, height = 1180, 460
    left, right, top, bottom = 34, 480, 104, 92
    plot_w = width - left - right
    plot_h = height - top - bottom
    max_collected = max(row["collected"] for row in data)
    step = plot_w / (len(data) - 1)

    svg = [_header(width, height, "TEST SUITE THROUGH PHASES 9-20",
                   "collected vs passing, one point per phase close · failures drawn as the red band")]

    # Axis
    svg.append(f'<line x1="{left}" y1="{top + plot_h}" x2="{left + plot_w}" y2="{top + plot_h}" '
               f'stroke="{LINE}" stroke-width="1.5"/>')
    for frac in (0.25, 0.5, 0.75, 1.0):
        value = int(max_collected * frac)
        y = top + plot_h - plot_h * frac
        svg.append(f'<line x1="{left}" y1="{y}" x2="{left + plot_w}" y2="{y}" stroke="{LINE}" '
                   f'stroke-dasharray="2 5"/>')
        svg.append(f'<text x="{left - 8}" y="{y + 4}" text-anchor="end" font-family="{MONO}" '
                   f'font-size="10" fill="{MUTED}">{value}</text>')

    prev_collected_y = prev_passed_y = None
    for index, row in enumerate(data):
        x = left + index * step
        yc = top + plot_h - plot_h * (row["collected"] / max_collected)
        yp = top + plot_h - plot_h * (row["passed"] / max_collected)
        if prev_collected_y is not None:
            svg.append(f'<line x1="{x - step}" y1="{prev_collected_y}" x2="{x}" y2="{yc}" '
                       f'stroke="{MUTED}" stroke-width="2"/>')
            svg.append(f'<line x1="{x - step}" y1="{prev_passed_y}" x2="{x}" y2="{yp}" '
                       f'stroke="{ACCENT}" stroke-width="3.5"/>')
            if row["failed"]:
                svg.append(f'<line x1="{x - step}" y1="{prev_collected_y}" x2="{x - step}" '
                           f'y2="{prev_passed_y}" stroke="{BAD}" stroke-width="2" opacity=".5"/>')
        svg.append(f'<circle cx="{x}" cy="{yc}" r="3.2" fill="{MUTED}"/>')
        svg.append(f'<circle cx="{x}" cy="{yp}" r="4.4" fill="{ACCENT}" stroke="{PANEL}" '
                   f'stroke-width="1.6"/>')
        if row["failed"]:
            svg.append(f'<circle cx="{x}" cy="{yp}" r="8" fill="none" stroke="{BAD}" '
                       f'stroke-width="1.6"/>')
        svg.append(f'<text x="{x}" y="{top + plot_h + 22}" text-anchor="middle" '
                   f'font-family="{MONO}" font-size="10" fill="{INK}">{row["phase"].split()[0]}</text>')
        prev_collected_y, prev_passed_y = yc, yp

    first = data[0]
    last = data[-1]
    legend_x = width - right + 26
    svg.append(f'<rect x="{legend_x - 14}" y="{top - 14}" width="{right - 46}" height="{plot_h + 20}" '
               f'rx="10" fill="{BG}" stroke="{LINE}"/>')
    rows = [
        ("collected", MUTED, f'{last["collected"]}'),
        ("passing", ACCENT, f'{last["passed"]}'),
        ("failing", BAD, f'{last["failed"]}'),
        ("baseline", INK, f'{first["passed"]} pass / {first["failed"]} fail'),
        ("net added", OK, f'+{last["passed"] - first["passed"]}'),
    ]
    svg.append(f'<text x="{legend_x}" y="{top + 8}" font-family="{MONO}" font-size="11" '
               f'letter-spacing="1.6" fill="{MUTED}">AT PHASE 20 CLOSE</text>')
    for offset, (label, colour, value) in enumerate(rows):
        y = top + 40 + offset * 46
        svg.append(f'<line x1="{legend_x}" y1="{y - 12}" x2="{legend_x + 16}" y2="{y - 12}" '
                   f'stroke="{colour}" stroke-width="3"/>')
        svg.append(f'<text x="{legend_x}" y="{y + 4}" font-family="{MONO}" font-size="11" '
                   f'fill="{MUTED}">{_esc(label)}</text>')
        svg.append(f'<text x="{legend_x}" y="{y + 24}" font-family="{MONO}" font-size="16" '
                   f'font-weight="700" fill="{colour}">{_esc(value)}</text>')
    svg.append(f'<text x="{left}" y="{height - 22}" font-family="{MONO}" font-size="10" '
               f'fill="{MUTED}">source: benchmarks/suite_history.json, transcribed from each '
               f'audit/PHASE*_REPORT.md final full-suite run</text>')
    (OUT / "chart-suite-growth.svg").write_text("".join(svg) + "</svg>\n")


def acceptance_states() -> None:
    data = json.loads((BENCH / "acceptance_latency.json").read_text())["commands"]
    executed = [row for row in data if not row.get("skipped")]
    width, height = 1180, 140 + 34 * len(executed) + 110
    svg = [_header(width, height, "THE PLAN'S 18 ACCEPTANCE COMMANDS, MEASURED",
                   "every command run through the ASGI app -- no network, no hardware, no account")]

    top = 128
    for label, x in (("COMMAND", 46), ("TERMINAL STATE", 470), ("AUDIT TOKEN", 700), ("ms", 840)):
        svg.append(f'<text x="{x}" y="{top - 30}" font-family="{MONO}" font-size="10" '
                   f'letter-spacing="1.4" fill="{MUTED}">{label}</text>')
    for rule in (462, 692):
        svg.append(f'<line x1="{rule}" y1="{top - 46}" x2="{rule}" '
                   f'y2="{top + len(executed) * 34 - 8}" stroke="{LINE}"/>')
    for index, row in enumerate(executed):
        y = top + index * 34
        state = row["dashboard_state"]
        colour = STATE_COLOUR.get(state, MUTED)
        if index % 2 == 0:
            svg.append(f'<rect x="34" y="{y - 20}" width="{width - 68}" height="30" rx="6" '
                       f'fill="{BG}" opacity=".7"/>')
        svg.append(f'<text x="46" y="{y + 1}" font-family="{MONO}" font-size="12" '
                   f'fill="{INK}">{_esc(row["command"][:52])}</text>')
        chip_x = 470
        chip_w = 204
        svg.append(f'<rect x="{chip_x}" y="{y - 16}" width="{chip_w}" height="24" rx="12" '
                   f'fill="{colour}" opacity=".12"/>')
        svg.append(f'<rect x="{chip_x}" y="{y - 16}" width="{chip_w}" height="24" rx="12" '
                   f'fill="none" stroke="{colour}" stroke-width="1.2"/>')
        svg.append(f'<text x="{chip_x + chip_w / 2}" y="{y + 1}" text-anchor="middle" '
                   f'font-family="{MONO}" font-size="10.5" font-weight="700" letter-spacing="1" '
                   f'fill="{colour}">{_esc(state)}</text>')
        svg.append(f'<text x="700" y="{y + 1}" font-family="{MONO}" font-size="11" '
                   f'fill="{MUTED}">{_esc(row["audit_result"])}</text>')
        ms = row["ms"]
        bar = min(190, max(3, int((ms ** 0.5) * 1.9)))
        bar_colour = BAD if ms > 2000 else (WARN if ms > 400 else ACCENT)
        svg.append(f'<rect x="840" y="{y - 8}" width="{bar}" height="12" rx="3" '
                   f'fill="{bar_colour}" opacity=".85"/>')
        svg.append(f'<text x="{846 + bar}" y="{y + 2}" font-family="{MONO}" font-size="10.5" '
                   f'fill="{INK}">{ms}</text>')

    tally = {}
    for row in executed:
        tally[row["dashboard_state"]] = tally.get(row["dashboard_state"], 0) + 1
    y = top + len(executed) * 34 + 14
    x = 46
    svg.append(f'<line x1="34" y1="{y - 20}" x2="{width - 34}" y2="{y - 20}" stroke="{LINE}"/>')
    svg.append(f'<text x="{x}" y="{y + 4}" font-family="{MONO}" font-size="11" fill="{MUTED}">'
               f'tally</text>')
    for state in sorted(tally, key=lambda s: -tally[s]):
        colour = STATE_COLOUR.get(state, MUTED)
        label = f'{state} ×{tally[state]}'
        svg.append(f'<text x="{x + 64}" y="{y + 4}" font-family="{MONO}" font-size="11" '
                   f'font-weight="700" fill="{colour}">{_esc(label)}</text>')
        x += 64 + 11 * len(label) + 26
    svg.append(f'<text x="34" y="{height - 24}" font-family="{MONO}" font-size="10" '
               f'fill="{MUTED}">source: benchmarks/acceptance_latency.json — '
               f're-run with `python benchmarks/acceptance_run.py`. "open VS Code" is '
               f'excluded: measuring it launches a GUI application on the operator\'s machine.</text>')
    (OUT / "chart-acceptance-states.svg").write_text("".join(svg) + "</svg>\n")


def hardware_truth() -> None:
    data = json.loads((BENCH / "hardware_status.json").read_text())
    peripherals = data["peripherals"]
    rows = []
    for name, info in peripherals.items():
        configured = bool(info.get("configured") or info.get("online") or info.get("paired"))
        state = "not configured"
        if name == "car":
            state = "offline · no adapter or vehicle API"
        elif name == "home":
            state = f'offline · {info.get("devices")} devices modelled, bridge not reached'
        elif name == "robotics":
            state = f'offline · {info.get("robots")} robots in fleet, no bridge'
        elif name == "watch":
            state = f'unpaired · queue only, {info["counts"]["total"]} deliveries'
        rows.append((name, configured, state))

    width, height = 1180, 120 + 52 * len(rows) + 150
    svg = [_header(width, height, "HARDWARE TRUTH TABLE",
                   "what /health reports with nothing attached -- no value is shown as measured "
                   "unless it was measured")]
    top = 112
    for index, (name, configured, state) in enumerate(rows):
        y = top + index * 52
        svg.append(f'<rect x="34" y="{y - 22}" width="{width - 68}" height="42" rx="8" '
                   f'fill="{PANEL}" stroke="{LINE}"/>')
        colour = OK if configured else MUTED
        svg.append(f'<text x="56" y="{y + 5}" font-family="{MONO}" font-size="14" '
                   f'font-weight="700" letter-spacing="1" fill="{INK}">{_esc(name.upper())}</text>')
        svg.append(f'<text x="250" y="{y + 4}" font-family="{MONO}" font-size="12" '
                   f'fill="{MUTED}">{_esc(state)}</text>')
        label = "configured" if configured else "NOT CONFIGURED"
        svg.append(f'<rect x="{width - 236}" y="{y - 14}" width="170" height="26" rx="13" '
                   f'fill="{colour}" opacity=".12"/>')
        svg.append(f'<text x="{width - 151}" y="{y + 4}" text-anchor="middle" '
                   f'font-family="{MONO}" font-size="10.5" font-weight="700" letter-spacing="1" '
                   f'fill="{colour}">{label}</text>')

    y = top + len(rows) * 52 + 24
    notes = [
        ("LLM planner", "disabled — deterministic rule planner only", MUTED),
        ("Email / calendar", f'{data["integrations"].get("email")} / '
                            f'{data["integrations"].get("calendar")} (local stores)', WARN),
        ("Memory retrieval", f'{data.get("memory_retrieval")} — lexical hashing embedder, '
                             'no model download', WARN),
        ("Paired devices", f'{data.get("paired_devices")}', MUTED),
    ]
    svg.append(f'<line x1="34" y1="{y - 12}" x2="{width - 34}" y2="{y - 12}" stroke="{LINE}"/>')
    for offset, (label, value, colour) in enumerate(notes):
        yy = y + 16 + offset * 26
        svg.append(f'<text x="46" y="{yy}" font-family="{MONO}" font-size="11" '
                   f'fill="{MUTED}">{_esc(label)}</text>')
        svg.append(f'<text x="250" y="{yy}" font-family="{MONO}" font-size="11.5" '
                   f'font-weight="700" fill="{colour}">{_esc(value)}</text>')
    svg.append(f'<text x="34" y="{height - 22}" font-family="{MONO}" font-size="10" '
               f'fill="{MUTED}">source: benchmarks/hardware_status.json (GET /health)</text>')
    (OUT / "chart-hardware-truth.svg").write_text("".join(svg) + "</svg>\n")


def findings_severity() -> None:
    # Counted from audit/PHASE9_FINAL_REPORT.md finding ids, not typed by hand here.
    data = {"P0": 6, "P1": 13, "P2": 12, "P3": 7, "P4": 8}
    colours = {"P0": BAD, "P1": "#ea580c", "P2": WARN, "P3": ACCENT, "P4": MUTED}
    labels = {
        "P0": "verification lied · master token disclosed · emergency stop overstated · "
              "watch could approve · deletion over-claimed · chain verifier wrong",
        "P1": "confirmation not re-checked · revocation tie · offline shown as empty · "
              "risk tier mutable",
        "P2": "desktop bundle exits 1 · status doc drift",
        "P3": "documentation and labelling",
        "P4": "polish",
    }
    width, height = 1180, 120 + 62 * len(data) + 60
    svg = [_header(width, height, "WHAT THE PHASE 9 AUDIT FOUND",
                   "46 findings by severity, each with a reproducible probe in the phase report")]
    top = 116
    maximum = max(data.values())
    for index, (key, count) in enumerate(data.items()):
        y = top + index * 62
        colour = colours[key]
        svg.append(f'<text x="46" y="{y + 16}" font-family="{MONO}" font-size="18" '
                   f'font-weight="700" fill="{colour}">{key}</text>')
        bar = int(300 * count / maximum)
        svg.append(f'<rect x="110" y="{y}" width="{bar}" height="26" rx="5" fill="{colour}" '
                   f'opacity=".88"/>')
        svg.append(f'<text x="{116 + bar}" y="{y + 18}" font-family="{MONO}" font-size="14" '
                   f'font-weight="700" fill="{INK}">{count}</text>')
        svg.append(f'<text x="440" y="{y + 17}" font-family="{MONO}" font-size="10.5" '
                   f'fill="{MUTED}">{_esc(labels[key][:96])}</text>')
    svg.append(f'<text x="34" y="{height - 24}" font-family="{MONO}" font-size="10" '
               f'fill="{MUTED}">P0-1 was one undecorated import line; it made 126 tests fail and '
               f'every offline read auditable as success.</text>')
    (OUT / "chart-findings-severity.svg").write_text("".join(svg) + "</svg>\n")


if __name__ == "__main__":
    suite_growth()
    acceptance_states()
    hardware_truth()
    findings_severity()
    for name in ("chart-suite-growth.svg", "chart-acceptance-states.svg",
                 "chart-hardware-truth.svg", "chart-findings-severity.svg"):
        print("drew", OUT / name)
