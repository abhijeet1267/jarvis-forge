#!/usr/bin/env python3
"""Re-measure the numbers this repository publishes, and write them as JSON.

Everything in README.md that looks like a score, a chart or a claim is generated
from the files this script writes -- no hand-typed figures, and nothing that
cannot be re-run on a laptop with no network, no hardware and no account.

    .venv/bin/python benchmarks/acceptance_run.py

Outputs, under benchmarks/:
  acceptance_latency.json  the plan's 18 acceptance commands, one real /command
                           round-trip each, with latency and terminal state
  hardware_status.json     what /health says about each peripheral, right now
  suite_history.json       the recorded suite size at the close of each phase,
                           transcribed from audit/PHASE*_REPORT.md

A scratch JARVIS_HOME is used, so the developer's real memory database and audit
chain are never touched by a benchmark run.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "benchmarks"
sys.path.insert(0, str(ROOT))

# The plan's own acceptance list (section 59), verbatim.
ACCEPTANCE_COMMANDS = [
    "Jarvis, open VS Code.",
    "Jarvis, find my latest project report.",
    "Jarvis, show me my laptop battery.",
    "Jarvis, transfer this file to my phone.",
    "Jarvis, what is my phone battery?",
    "Jarvis, plan my day.",
    "Jarvis, schedule two hours for project work tomorrow.",
    "Jarvis, draft an email to my teammate.",
    "Jarvis, send it.",
    "Jarvis, remember that this is my AMR project.",
    "Jarvis, what did I work on yesterday?",
    "Jarvis, design this system.",
    "Jarvis, research this idea.",
    "Jarvis, check my car fuel.",
    "Jarvis, check my car charge.",
    "Jarvis, give me today's location summary.",
    "Jarvis, turn on study mode.",
    "Jarvis, stop all physical actions.",
]

# Do not launch a GUI application on the user's machine just to draw a chart.
# This entry is measured as a route, not as a side effect.
SKIP_EXECUTION = {"Jarvis, open VS Code."}


def _pipeline_state(payload: dict) -> str:
    """Run the *shipped* dashboard mapping, so the chart shows what a user sees."""
    source = (ROOT / "dashboard" / "index.html").read_text()
    body = re.search(r"<script>(.*?)</script>", source, re.S).group(1)
    snippet = body[body.index("const STATE_TONE"):body.index("function renderState")]
    snippet = snippet.replace("const STATE_TONE", "var STATE_TONE", 1)
    script = "eval(" + json.dumps(snippet) + ");\n" \
             "console.log(pipelineState(JSON.parse(process.argv[2])));\n"
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
        handle.write(script)
        path = handle.name
    node = subprocess.run(["node", path, json.dumps(payload, default=str)],
                          capture_output=True, text=True, check=False)
    os.unlink(path)
    return node.stdout.strip() or "UNKNOWN"


def measure_acceptance() -> dict:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp(prefix="jarvis-bench-")
    os.environ.setdefault("JARVIS_LOG_LEVEL", "WARNING")
    # Fixed token, set before jarvis is imported so settings pick it up. A run
    # without it gets 401 on every command and would otherwise record fourteen
    # empty rows as if they were measurements.
    os.environ["JARVIS_API_TOKEN"] = "benchmark-token-not-for-production-use"
    from fastapi.testclient import TestClient
    from jarvis.api.app import app, get_core

    client = TestClient(app, base_url="http://127.0.0.1:8765",
                        headers={"X-JARVIS-Token": os.environ["JARVIS_API_TOKEN"]})
    rows = []
    for text in ACCEPTANCE_COMMANDS:
        if text in SKIP_EXECUTION:
            rows.append({"command": text, "skipped": "would launch a GUI application"})
            continue
        core = get_core()
        before = core.db.query("SELECT COALESCE(MAX(rowid), 0) AS m FROM action_logs")[0]["m"]
        started = time.perf_counter()
        response = client.post("/command", json={"text": text, "source": "benchmark"})
        elapsed = round((time.perf_counter() - started) * 1000)
        if response.status_code != 200:
            raise SystemExit(f"/command {text!r} -> HTTP {response.status_code}; "
                             "the benchmark measures real answers, not rejections")
        payload = response.json()
        if payload.get("needs_confirmation") and payload.get("confirmation_id"):
            approval = client.post(
                f"/confirm/{payload['confirmation_id']}?approve=true", json={})
            payload = approval.json()
            elapsed = round((time.perf_counter() - started) * 1000)
        added = core.db.query(
            "SELECT result FROM action_logs WHERE rowid > ? ORDER BY rowid", (before,))
        if not added:
            raise SystemExit(f"{text!r} produced no audit row -- command accountability "
                             "regressed (Phase 16): refusing to publish a blank number")
        rows.append({
            "command": text,
            "http_status": response.status_code,
            "ms": elapsed,
            "intent": payload.get("intent"),
            "tools": [a.get("tool") for a in payload.get("actions") or []],
            "verification": [a.get("verification") for a in payload.get("actions") or []],
            "audit_result": added[-1]["result"],
            "dashboard_state": _pipeline_state(payload),
            "reply": " ".join((payload.get("reply") or "").split())[:160],
        })
    return {
        "surface": "POST /command via the ASGI app (no network, no hardware, no account)",
        "commands": rows,
    }


def measure_hardware() -> dict:
    """What the API itself says about each peripheral, unmassaged."""
    os.environ.setdefault("JARVIS_HOME", tempfile.mkdtemp(prefix="jarvis-bench-"))
    os.environ.setdefault("JARVIS_LOG_LEVEL", "WARNING")
    os.environ.setdefault("JARVIS_API_TOKEN", "benchmark-token-not-for-production-use")
    from fastapi.testclient import TestClient
    from jarvis.api.app import app

    client = TestClient(app, base_url="http://127.0.0.1:8765",
                        headers={"X-JARVIS-Token": os.environ["JARVIS_API_TOKEN"]})
    response = client.get("/health")
    if response.status_code != 200:
        raise SystemExit(f"/health -> HTTP {response.status_code}")
    health = response.json()
    if "peripherals" not in health:
        raise SystemExit("/health returned no peripherals -- refusing to publish "
                         f"an empty table as a result: {str(health)[:120]}")
    security = dict(health.get("security", {}))
    if "token_path" in security:
        # The scratch home's random temp path is this machine's, not a fact about the
        # product; keeping it out of the committed artifact stops every run from
        # rewriting the file. The token itself was never in here -- only its path.
        security["token_path"] = "<JARVIS_HOME>/api_token"
    return {
        "surface": "GET /health, no hardware attached, no accounts configured",
        "tools": health.get("tools"),
        "agents": health.get("agents"),
        "llm_enabled": health.get("llm_enabled"),
        "peripherals": health.get("peripherals", {}),
        "integrations": health.get("integrations", {}),
        "memory_retrieval": (health.get("memory_retrieval") or {}).get("mode"),
        "paired_devices": (health.get("devices") or {}).get("paired_devices"),
        "security": security,
    }


def suite_history() -> dict:
    """Suite size at the close of each phase, as recorded in the audit reports."""
    points = [
        ("9 audit", 1002, 875, 126, "baseline as found"),
        ("10 truth/authority", 1002, 998, 3, "P0 verification authority fixed"),
        ("11 real world", 1132, 1128, 0, "first fully green run"),
        ("12 command path", 1147, 1143, 0, "typed sentence proven"),
        ("13 dashboard truth", 1160, 1156, 0, "state rendered from server fields"),
        ("14 workflows", 1185, 1181, 0, "multi-step plan reachable"),
        ("15 acceptance", 1207, 1203, 0, "18 commands run live; 6 false successes found"),
        ("16 accountability", 1222, 1218, 0, "route-level refusals audited"),
        ("17 coherence", 1251, 1247, 0, "response agrees with its ledger row"),
        ("18 simulation", 1263, 1259, 0, "simulated actuation no longer audited success"),
        ("19 refusals", 1280, 1276, 0, "dropped clauses named; VERIFIED IN PART"),
        ("20 dated recall", 1290, 1286, 0, "recalling by day, echoes labelled"),
        ("21 speed", 1304, 1300, 0, "bounded search, graceful source fallback"),
        ("22 safety matrix", 1321, 1317, 0, "ambiguous actuation refused; outages disclosed"),
        ("23 deletion completeness", 1331, 1330, 0, "three parked xfails fixed at their source"),
        ("24 refusal vs detail", 1343, 1342, 0, "a question stopped being labelled a refusal"),
        ("25 desktop truth", 1360, 1359, 0, "the self-test runs on a broken install"),
        ("26 recovery trust", 1417, 1417, 0, "retry keyed on re-running, frozen risk tiers"),
        ("27 job re-run", 1433, 1433, 0, "a failed step re-runs through the gate"),
        ("28 reading is not writing", 1466, 1466, 0, "a read request can no longer author a file"),
        ("29 routes fill themselves", 1487, 1487, 0, "a route asks instead of quoting a schema field"),
        ("30 capabilities reachable", 1555, 1555, 0, "a named capability is reached, or asked about"),
        ("31 the device you named", 1582, 1582, 0, "the plan's safety matrix holds 6 of 6 for the first time"),
        ("32 one vocabulary one approval", 1621, 1621, 0, "the router speaks the gateway's words; the approval names the device"),
        ("33 questions carry their answers", 1642, 1642, 0, "a question offers the ids it holds rather than demanding them"),
        ("34 one deadline per sentence", 1653, 1653, 0, "a network read that never stops being silent can no longer run forever"),
        ("35 reach the laptop", 1683, 1683, 0, "a phone drives the laptop over the LAN; the client page was tested in a browser and found three bugs"),
        ("36 two applications", 1710, 1710, 0, "the .app carries its own source; the phone gets a manifest, icons and a worker"),
        ("37 vantage", 1718, 1718, 0, "renamed where a person reads it; the wiring names counted identical before and after"),
    ]
    return {"phases": [{"phase": p, "collected": c, "passed": q, "failed": f, "note": n}
                       for p, c, q, f, n in points]}


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for name, data in (("acceptance_latency.json", measure_acceptance()),
                       ("hardware_status.json", measure_hardware()),
                       ("suite_history.json", suite_history())):
        (OUT / name).write_text(json.dumps(data, indent=1) + "\n")
        print("wrote", OUT / name)


if __name__ == "__main__":
    main()
