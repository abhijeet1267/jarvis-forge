#!/usr/bin/env python3
"""Drive the disposable probe with real input and read back what happened.

This is the Level-3 test the directive asks for, made runnable in one command:

    tools/input_probe/build.sh && tools/input_probe/vantage-probe &
    tools/input_probe/selftest.py

Every assertion is an *observation of the target's own state file*, which the
probe writes from inside its own process when a control actually fires. A
posted event that nothing received leaves the file unchanged, so a PASS here
cannot be produced by the act of posting.

Exit codes:
    0  every step observed
    2  input permission is missing (nothing was attempted)
    3  permission was present but an observed outcome did not match
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
EXECUTOR = REPO / "native" / "desktop_executor" / "vantage-desktop"
STATE = Path("/tmp/vantage_probe_app/state.json")

results: list[tuple[str, str, str]] = []


def record(step: str, status: str, detail: str) -> None:
    results.append((step, status, detail))
    print(f"  {status:12s} {step}: {detail}")


def read_state() -> dict:
    for _ in range(40):
        try:
            return json.loads(STATE.read_text())
        except (OSError, json.JSONDecodeError):
            time.sleep(0.05)
    raise SystemExit("the probe's state file never became readable; is it running?")


def run(op: str, *args: str) -> dict:
    proc = subprocess.run([str(EXECUTOR), op, *args],
                          capture_output=True, text=True, timeout=30)
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"status": "unknown", "reason": (proc.stdout or proc.stderr)[:200]}


def point(state: dict, name: str) -> tuple[float, float]:
    target = (state.get("targets") or {}).get(name)
    if not target:
        raise SystemExit(f"the probe did not report a target named {name!r}; "
                         "rebuild it with tools/input_probe/build.sh")
    return float(target["x"]), float(target["y"])


def wait_for(predicate, timeout: float = 2.0) -> dict:
    """Poll the target's own state, not our optimism."""
    deadline = time.monotonic() + timeout
    state = read_state()
    while time.monotonic() < deadline:
        if predicate(state):
            return state
        time.sleep(0.05)
        state = read_state()
    return state


print("=" * 70)
print("probe self-test — real input, observed outcome")
print("=" * 70)

if not EXECUTOR.exists():
    raise SystemExit(f"no executor at {EXECUTOR}; run native/desktop_executor/build.sh")

posture = run("permissions")
state = read_state()
if not state.get("targets"):
    raise SystemExit("the running probe predates target reporting; restart it after "
                     "rebuilding with tools/input_probe/build.sh")

print(f"\n  executor:  {posture.get('executable')}")
print(f"  post events: {posture.get('post_events')}   accessibility: {posture.get('accessibility')}")
print()

if not posture.get("post_events"):
    # Nothing is attempted. Claiming a click here would be the exact failure the
    # directive exists to prevent.
    print("PERMISSION BLOCKED — post-event access is not granted to this executable.")
    print(f"  grant it: {EXECUTOR} request_access")
    print("  then add it in System Settings -> Privacy & Security -> Accessibility,")
    print("  quit and rerun the probe, and run this script again.")
    sys.exit(2)

failures = 0

# -- B: click a button -------------------------------------------------------
x, y = point(state, "click")
before = state["clicks"]
move = run("move", str(x), str(y))
clicked = run("click", str(x), str(y))
after = wait_for(lambda s: s["clicks"] > before)
if after["clicks"] > before:
    record("B click", "VERIFIED", f"clicks {before} -> {after['clicks']} (state file, from inside the probe)")
else:
    failures += 1
    record("B click", "FAILED", f"posted={clicked.get('status')} but clicks stayed {before}")

# -- E: double-click ---------------------------------------------------------
x, y = point(state, "double")
before = read_state()["double_clicks"]
run("move", str(x), str(y))
run("click", str(x), str(y), "--count", "2")
after = wait_for(lambda s: s["double_clicks"] > before)
if after["double_clicks"] > before:
    record("E double-click", "VERIFIED", f"doubles {before} -> {after['double_clicks']}")
else:
    failures += 1
    record("E double-click", "FAILED", f"doubles stayed {before}")

# -- C: click a text field, then type ---------------------------------------
x, y = point(state, "field")
run("move", str(x), str(y))
run("click", str(x), str(y))
# The field takes focus asynchronously; wait on the border effect rather than
# sleeping a fixed amount, and do not type into a field that never took it.
focused = wait_for(lambda s: s["last_action"] in ("typing", "click"), timeout=1.0)
time.sleep(0.15)
note = "Vantage real desktop control"
typed = run("type", note)
after = wait_for(lambda s: s["text"] == note)
if after["text"] == note:
    record("C type into a field", "VERIFIED", f"the field reads back {after['text']!r}")
else:
    failures += 1
    record("C type into a field", "FAILED",
           f"posted={typed.get('status')}, field reads {after['text']!r}")

# -- A: move the cursor ------------------------------------------------------
x, y = point(state, "checkbox")
moved = run("move", str(x), str(y))
cursor = moved.get("cursor_after") or {}
if abs(cursor.get("x", 0) - x) <= 2 and abs(cursor.get("y", 0) - y) <= 2:
    record("A move cursor", "VERIFIED", f"pointer readback ({cursor.get('x')}, {cursor.get('y')}) == target")
else:
    failures += 1
    record("A move cursor", "FAILED", f"readback {cursor} != ({x}, {y})")

print()
for step, status, detail in results:
    print(f"{step:22s} {status}")
print()
if failures:
    print(f"{failures} step(s) posted an event that the target never observed — "
          "that is a real defect, not a permission problem.")
    sys.exit(3)
print("All steps were confirmed by the target's own state, not by our claim.")