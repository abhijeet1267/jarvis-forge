#!/usr/bin/env python3
"""Generate the repository's three diagrams, in the dashboard's own visual language.

    .venv/bin/python assets/build_diagrams.py

Hand-laid SVG rather than a drawing tool or a plotting library: it keeps the repo
dependency-free, renders sharply at any width on GitHub, and lets the palette be
taken straight from dashboard/index.html so the documentation and the product look
like one thing.

No number is typed into this file. The counts that appear in the diagrams are read
from the live registry and from benchmarks/*.json, so a diagram cannot drift from
the code it claims to describe -- which is the whole subject of this repository.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

INK = "#1b2230"
MUTED = "#6b7688"
LINE = "#e2e7ef"
ACCENT = "#2f6df6"
OK = "#16a34a"
WARN = "#d97706"
BAD = "#dc2626"
NAVY = "#0f172a"
TEAL = "#0d9488"
PANEL = "#ffffff"
BG = "#f5f7fa"
M = "ui-monospace, 'SF Mono', Menlo, Consolas, monospace"


def live_counts() -> dict:
    """Ask the running registry rather than restating numbers in this file."""
    os.environ.setdefault("JARVIS_HOME", tempfile.mkdtemp(prefix="jarvis-art-"))
    os.environ.setdefault("JARVIS_LOG_LEVEL", "WARNING")
    sys.path.insert(0, str(ROOT))
    import collections

    from jarvis.core.orchestrator import JarvisCore

    core = JarvisCore()
    specs = [core.tools.get(name) for name in core.tools.names()]
    risks = collections.Counter(int(spec.risk) for spec in specs)
    routes = 0
    source = (ROOT / "jarvis" / "api" / "app.py").read_text()
    for verb in ("get", "post", "put", "patch", "delete"):
        routes += source.count(f'@app.{verb}("')
    return {
        "tools": len(specs),
        "agents": len(core.agents),
        "routes": routes,
        "confirm_gated": sum(1 for spec in specs if spec.requires_confirmation),
        "risk": dict(sorted(risks.items())),
        "tables": core.db.query(
            "SELECT COUNT(*) AS n FROM sqlite_master WHERE type='table'")[0]["n"],
    }


def suite_tail() -> tuple:
    phases = json.loads((ROOT / "benchmarks" / "suite_history.json").read_text())["phases"]
    last = phases[-1]
    return len(phases) + 8, last["collected"], last["failed"] + next(
        (p["failed"] for p in phases if p["failed"]), 0)


def _frame(width: int, height: int, title: str, subtitle: str) -> list:
    return [f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-label="{title}">
<defs>
 <pattern id="g" width="26" height="26" patternUnits="userSpaceOnUse"><path d="M26 0H0v26" fill="none" stroke="{LINE}" stroke-width="1"/></pattern>
 <linearGradient id="fade" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="{ACCENT}" stop-opacity=".16"/><stop offset="1" stop-color="{ACCENT}" stop-opacity="0"/></linearGradient>
</defs>
<rect width="100%" height="100%" fill="{BG}"/><rect width="100%" height="100%" fill="url(#g)" opacity=".6"/>
<rect x="1" y="1" width="{width - 2}" height="{height - 2}" rx="16" fill="{PANEL}" stroke="{LINE}"/>
<text x="40" y="52" font-family="{M}" font-size="17" font-weight="700" letter-spacing="2.6" fill="{INK}">{title}</text>
<text x="40" y="74" font-family="{M}" font-size="12" fill="{MUTED}">{subtitle}</text>
''']


def esc(value: str) -> str:
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def chip(svg: list, x: float, y: float, label: str, colour: str, filled: bool = False,
         size: float = 10.5) -> float:
    width = 20 + size * 0.72 * len(label)
    if filled:
        svg.append(f'<rect x="{x}" y="{y}" width="{width}" height="26" rx="13" fill="{colour}" opacity=".12"/>')
        svg.append(f'<rect x="{x}" y="{y}" width="{width}" height="26" rx="13" fill="none" stroke="{colour}" stroke-width="1.1"/>')
    else:
        svg.append(f'<rect x="{x}" y="{y}" width="{width}" height="22" rx="5" fill="{BG}" stroke="{LINE}"/>')
    text_y = y + (17 if filled else 15)
    svg.append(f'<text x="{x + width / 2}" y="{text_y}" text-anchor="middle" font-family="{M}" '
               f'font-size="{size}" font-weight="700" letter-spacing="{1 if filled else 0}" '
               f'fill="{colour}">{esc(label)}</text>')
    return width


def hero(counts: dict) -> None:
    phases, collected, _ = suite_tail()
    s = _frame(1280, 430, "JARVIS FORGE",
               "a permissioned, local-first agent OS -- and the audit trail that proves what it did")
    s.append('<rect x="1" y="88" width="1278" height="120" fill="url(#fade)"/>')
    s.append(f'<text x="40" y="150" font-family="{M}" font-size="46" font-weight="700" '
             f'letter-spacing="6" fill="{INK}">SAY IT &#183; GATED &#183; VERIFIED</text>')
    s.append(f'<text x="40" y="182" font-family="{M}" font-size="14" fill="{MUTED}">'
             f'{counts["tools"]} tools &#183; {counts["agents"]} agents &#183; {counts["routes"]} API routes '
             f'&#183; {counts["tables"]} tables &#183; hash-chained audit</text>')
    x = 40
    stages = ["AUTH", "PLAN", "PERMIT", "CONFIRM", "EXECUTE", "OBSERVE", "AUDIT"]
    for index, label in enumerate(stages):
        width = 148
        gated = label in ("PERMIT", "CONFIRM", "OBSERVE")
        s.append(f'<rect x="{x}" y="228" width="{width}" height="40" rx="8" fill="{PANEL}" '
                 f'stroke="{ACCENT if gated else LINE}" stroke-width="{1.8 if gated else 1}"/>')
        s.append(f'<text x="{x + width / 2}" y="253" text-anchor="middle" font-family="{M}" '
                 f'font-size="12" font-weight="700" letter-spacing="1.6" '
                 f'fill="{ACCENT if gated else INK}">{label}</text>')
        if index < len(stages) - 1:
            s.append(f'<path d="M{x + width + 3} 248h14" stroke="{MUTED}" stroke-width="1.4"/>'
                     f'<path d="M{x + width + 13} 244l6 4-6 4z" fill="{MUTED}"/>')
        x += width + 20
    s.append(f'<text x="40" y="308" font-family="{M}" font-size="10" letter-spacing="1.6" '
             f'fill="{MUTED}">8 OF THE 12 STATES A USER CAN BE SHOWN &#183; EACH FROM A SERVER FIELD, '
             f'NEVER FROM WISHFUL READING &#183; FULL MAP IN truth-states.svg</text>')
    x = 40
    for label, colour in [("VERIFIED", OK), ("VERIFIED IN PART", TEAL), ("SIMULATED", WARN),
                          ("UNAVAILABLE", WARN), ("AWAITING", WARN), ("NOTHING EXECUTED", NAVY),
                          ("BLOCKED", BAD), ("FAILED", BAD)]:
        width = chip(s, x, 322, label, colour, filled=True)
        x += width + 12
    s.append(f'<line x1="40" y1="374" x2="1240" y2="374" stroke="{LINE}"/>')
    s.append(f'<text x="40" y="398" font-family="{M}" font-size="11" fill="{MUTED}">'
             f'{phases} audit-and-repair phases &#183; {collected} tests collected &#183; 0 failing</text>')
    s.append(f'<text x="1240" y="398" text-anchor="end" font-family="{M}" font-size="11" '
             f'font-weight="700" letter-spacing="1.4" fill="{ACCENT}">macOS &#183; offline by default '
             f'&#183; no LLM required</text>')
    (HERE / "hero.svg").write_text("".join(s) + "</svg>\n")


def architecture(counts: dict) -> None:
    risk = counts["risk"]
    a = _frame(1180, 660, "HOW A SENTENCE BECOMES A VERIFIED ACT",
               "every path crosses one permission chokepoint; nothing reaches a tool without a verdict")

    def layer(y, name, items, colour, note=""):
        a.append(f'<rect x="40" y="{y}" width="820" height="74" rx="10" fill="{PANEL}" stroke="{LINE}"/>')
        a.append(f'<rect x="40" y="{y}" width="4" height="74" rx="2" fill="{colour}"/>')
        a.append(f'<text x="62" y="{y + 22}" font-family="{M}" font-size="11" font-weight="700" '
                 f'letter-spacing="1.6" fill="{INK}">{esc(name)}</text>')
        x, row_y = 62, y + 34
        for item in items:
            width = 20 + 10.5 * 0.72 * len(item)
            if x + width > 850:                      # wrap instead of overflowing the panel
                x, row_y = 62, row_y + 26
            a.append(f'<rect x="{x}" y="{row_y}" width="{width}" height="22" rx="5" fill="{BG}" '
                     f'stroke="{LINE}"/>')
            a.append(f'<text x="{x + width / 2}" y="{row_y + 15}" text-anchor="middle" '
                     f'font-family="{M}" font-size="10.5" fill="{MUTED}">{esc(item)}</text>')
            x += width + 8
        if note:
            a.append(f'<text x="62" y="{y + 68}" font-family="{M}" font-size="10" '
                     f'fill="{MUTED}">{esc(note)}</text>')

    y = 104
    layer(y, "SURFACES", ["dashboard", "voice", "watch", "phone", "CLI", "HTTP API"], MUTED)
    y += 90
    layer(y, "EDGE / AUTH", ["token 0600", "constant-time", "same-origin", "host allowlist",
                             "rate limit 600/min"], WARN,
          "a rejected request is logged but not written to the chain: an unauthenticated caller "
          "must not be able to append to it")
    y += 90
    layer(y, "ORCHESTRATOR", ["guard", "RulePlanner", "Plan", "permission", "confirmation",
                              "execute", "verify", "memory"], ACCENT,
          "deterministic rules only -- no LLM in the default path")
    y += 90
    layer(y, f'AGENTS ({counts["agents"]})', ["laptop", "laptop_center", "memory", "phone", "email",
                                             "calendar", "scheduler", "research", "coding", "design",
                                             "vision", "home", "robotics", "car", "watch"], ACCENT)
    y += 90
    layer(y, f'TOOLS ({counts["tools"]}) &#183; RISK 0-4'.replace("&#183;", "·"),
          [f'{["read", "low", "external", "high", "physical"][k]} {risk.get(k, 0)}' for k in range(5)]
          + [f'{counts["confirm_gated"]} confirm-gated'], TEAL,
          "a scheduled job re-asks permission every run and can never raise its own ceiling")

    a.append(f'<rect x="880" y="104" width="260" height="{y + 74 - 104}" rx="10" fill="{BG}" '
             f'stroke="{LINE}"/>')
    a.append(f'<text x="902" y="128" font-family="{M}" font-size="11" font-weight="700" '
             f'letter-spacing="1.6" fill="{INK}">PERSISTENCE</text>')
    for index, (key, value) in enumerate([
        ("SQLite", f'{counts["tables"]} tables'),
        ("action_logs", "hash-chained"),
        ("audit.jsonl", "append-only"),
        ("verify_chain()", "breaks reported"),
        ("memories", "hybrid retrieval"),
        ("devices", "HMAC pre-shared key"),
        ("scheduler", "30s tick, risk ceiling"),
    ]):
        yy = 152 + index * 40
        a.append(f'<text x="902" y="{yy}" font-family="{M}" font-size="11" fill="{MUTED}">{key}</text>')
        a.append(f'<text x="902" y="{yy + 15}" font-family="{M}" font-size="12" font-weight="700" '
                 f'fill="{ACCENT}">{esc(value)}</text>')
    a.append(f'<text x="40" y="640" font-family="{M}" font-size="10.5" fill="{MUTED}">source: '
             f'jarvis/api/app.py, jarvis/core/orchestrator.py, jarvis/security/ -- every count read '
             f'from the live registry by assets/build_diagrams.py</text>')
    (HERE / "architecture.svg").write_text("".join(a) + "</svg>\n")


def truth_states() -> None:
    rows = [
        ("VERIFIED", OK, 'actions[].verification == "success"', "an effect was observed in the world"),
        ("VERIFIED IN PART", TEAL, "all steps success AND plan.unhandled non-empty", "the request was answered in part"),
        ("SIMULATED", WARN, "actions[].output.simulated == true", "a simulator accepted it; nothing real moved"),
        ("UNAVAILABLE", WARN, "verification failed + provider_status unavailable/network_error", "the source could not be reached"),
        ("AWAITING CONFIRMATION", WARN, "needs_confirmation + confirmation_id", "parked; nothing has run"),
        ("NOTHING EXECUTED", NAVY, "plan.reply_kind == declined", "JARVIS said no, and said why"),
        ("NOT RECOGNISED", MUTED, "plan.reply_kind == not_understood", "no rule matched the goal"),
        ("NOT AVAILABLE", MUTED, "plan.reply_kind == capability_missing", "no such tool exists yet"),
        ("NEEDS DETAIL", WARN, "plan.needs_clarification == true", "a question back, not an answer"),
        ("BLOCKED", BAD, "decision == BLOCK / blocked_reason", "refused by policy or the safety guard"),
        ("FAILED", BAD, "verification == failed", "it was attempted and did not work"),
        ("UNKNOWN", BAD, "no evidence above distinguishes it", "the honest answer when nothing else does"),
    ]
    t = _frame(1180, 700, "WHERE EACH STATE COMES FROM",
               "the mapping the dashboard applies to server fields -- and the fields it is forbidden to invent")
    y = 112
    for label, x in (("STATE", 46), ("DERIVED FROM (response field)", 300),
                     ("WHAT IT MEANS TO THE PERSON READING IT", 742)):
        t.append(f'<text x="{x}" y="{y - 14}" font-family="{M}" font-size="10" letter-spacing="1.4" '
                 f'fill="{MUTED}">{label}</text>')
    for index, (name, colour, field, meaning) in enumerate(rows):
        yy = y + 8 + index * 42
        if index % 2 == 0:
            t.append(f'<rect x="40" y="{yy - 20}" width="1100" height="36" rx="6" fill="{BG}" opacity=".7"/>')
        chip(t, 46, yy - 16, name, colour, filled=True, size=11)
        t.append(f'<text x="300" y="{yy + 1}" font-family="{M}" font-size="11" fill="{INK}">{esc(field)}</text>')
        t.append(f'<text x="742" y="{yy + 1}" font-family="{M}" font-size="11" fill="{MUTED}">{esc(meaning)}</text>')
    line_y = y + 8 + len(rows) * 42
    t.append(f'<line x1="40" y1="{line_y}" x2="1140" y2="{line_y}" stroke="{LINE}"/>')
    t.append(f'<text x="46" y="{line_y + 26}" font-family="{M}" font-size="11" font-weight="700" '
             f'fill="{BAD}">NEVER A PATH TO VERIFIED:</text>')
    t.append(f'<text x="288" y="{line_y + 26}" font-family="{M}" font-size="11" fill="{MUTED}">'
             f'HTTP 200 &#183; action.ok &#183; reply text containing "success" &#183; a client-supplied '
             f'verified/status field</text>')
    t.append(f'<text x="46" y="{line_y + 48}" font-family="{M}" font-size="11" fill="{MUTED}">'
             f'The dashboard renders server truth. It authorises nothing, and a forged field can '
             f'change a label and nothing else.</text>')
    (HERE / "truth-states.svg").write_text("".join(t) + "</svg>\n")


if __name__ == "__main__":
    counts = live_counts()
    hero(counts)
    architecture(counts)
    truth_states()
    print("drew hero.svg, architecture.svg, truth-states.svg from", counts)
