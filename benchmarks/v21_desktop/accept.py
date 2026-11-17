#!/usr/bin/env python3
"""Physical desktop acceptance against the disposable fixture.

Every assertion is an observation of the fixture's own state file, which it
writes from inside its own process. A posted event that nothing received leaves
the file unchanged, so a PASS here cannot be produced by the act of posting.

    tools/input_probe/build.sh && tools/input_probe/vantage-probe &
    python3 benchmarks/v21_desktop/accept.py

Exit codes: 0 every step observed, 1 a step did not, 2 no fixture running.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
EXE = REPO / "native" / "desktop_executor" / "VantageDesktopDev.app" / "Contents" / "MacOS" / "vantage-desktop"
STATE = Path("/tmp/vantage_probe_app/state.json")

if not EXE.is_file():
    EXE = REPO / "native" / "desktop_executor" / "vantage-desktop"


def state() -> dict:
    """The fixture's own record, retried briefly because it writes on a runloop."""
    for _ in range(40):
        try:
            return json.loads(STATE.read_text())
        except (OSError, json.JSONDecodeError):
            time.sleep(0.05)
    return {}


def run(*args: str) -> dict:
    out = subprocess.run([str(EXE), *args], capture_output=True, text=True).stdout
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"status": "unknown", "reason": out[:160]}


def target(name: str) -> dict:
    """Fresh coordinates, read immediately before acting.

    Never cached: the fixture re-reports its frames when the window moves, and a
    cached point is exactly how a click lands on the wrong control.
    """
    return state()["targets"][name]


results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str) -> None:
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL':5s} {name:22s} {detail}")


def frontmost() -> str:
    return run("windows").get("frontmost_application", "")


def ensure_focus(tries: int = 6) -> bool:
    """Bring the fixture forward and confirm it, before every input step.

    A single activation at the start is not enough: the desktop is in use while
    this runs, and measured, focus moved to Safari between steps -- every action
    then went to Safari and the fixture never saw an event. Activation is
    idempotent and harmless to retry; sending input blindly is not.
    """
    for _ in range(tries):
        if "vantage-probe" in frontmost().lower():
            return True
        subprocess.run(["osascript", "-e",
                        'tell application "System Events" to set frontmost of '
                        '(first process whose name contains "vantage-probe") to true'],
                       capture_output=True)
        time.sleep(0.4)
    return "vantage-probe" in frontmost().lower()


if not STATE.exists():
    print("the fixture is not running; start tools/input_probe/vantage-probe first")
    sys.exit(2)

if not ensure_focus():
    print("the fixture will not come to the front; not sending input to another app")
    sys.exit(2)
baseline = state()
print(f"\nfixture baseline: clicks={baseline.get('clicks')} "
      f"doubles={baseline.get('double_clicks')} text={baseline.get('text')!r}\n")

# -- T2: one left click -------------------------------------------------------
ensure_focus()
p = target("click")
before = state()["clicks"]
run("click", f"x={p['x']}", f"y={p['y']}")
time.sleep(0.7)
after = state()["clicks"]
record("T2 left click", after == before + 1,
       f"counter {before} -> {after} at ({p['x']:.0f},{p['y']:.0f})")

# -- T3: a real double-click --------------------------------------------------
ensure_focus()
p = target("double")
before = state()["double_clicks"]
run("click", f"x={p['x']}", f"y={p['y']}", "count=2")
time.sleep(0.8)
after = state()["double_clicks"]
record("T3 double click", after > before, f"doubles {before} -> {after}")

# -- T4: right click opens the context menu -----------------------------------
ensure_focus()
p = target("context_menu")
before = state()["context_menu_openings"]
run("click", f"x={p['x']}", f"y={p['y']}", "button=right")
time.sleep(0.9)
after = state()["context_menu_openings"]
record("T4 right click", after > before, f"menu openings {before} -> {after}")
run("key", "key=escape")
time.sleep(0.3)

# -- T5: typing reaches the field ---------------------------------------------
ensure_focus()
p = target("field")
run("click", f"x={p['x']}", f"y={p['y']}")
time.sleep(0.4)
note = "Vantage real input acceptance"
run("type", f"text={note}")
time.sleep(0.9)
after = state()["text"]
record("T5 typing", after == note, f"field reads {after!r}")

# -- T6: cmd+a selects the whole field ----------------------------------------
ensure_focus()
p = target("shortcut_field")
run("click", f"x={p['x']}", f"y={p['y']}")
time.sleep(0.4)
run("key", "key=a", "modifiers=cmd")
time.sleep(0.6)
s = state()
sel, length = s.get("selection_length", 0), s.get("field_text_length", 0)
record("T6 shortcut", bool(length) and sel == length,
       f"selection {sel} of {length} characters")

# -- T7: scroll ---------------------------------------------------------------
ensure_focus()
p = target("scroller")
before = state()["scroll_offset"]
run("scroll", f"x={p['x']}", f"y={p['y']}", "lines=-5")
time.sleep(0.8)
after = state()["scroll_offset"]
if after == before:
    # The AX path is the alternate route; it reports its own real result.
    ax = run("scroll_element", "application=vantage-probe", "direction=down")
    time.sleep(0.6)
    after = state()["scroll_offset"]
    note = f"wheel posted, offset {before} -> {after}; AX said {ax.get('status')}"
else:
    note = f"offset {before} -> {after}"
record("T7 scroll", after != before, note)

# -- T8: drag an item onto the drop target ------------------------------------
ensure_focus()
src, dst = target("drag_source"), target("drop_target")
before = state()["drop_count"]
run("drag", f"from_x={src['x']}", f"from_y={src['y']}",
    f"to_x={dst['x']}", f"to_y={dst['y']}")
time.sleep(1.0)
s = state()
record("T8 drag", s["drop_count"] > before,
       f"drops {before} -> {s['drop_count']}, item={s.get('dropped_item')!r}")

# -- summary ------------------------------------------------------------------
print()
passed = sum(1 for _, ok, _ in results if ok)
print(f"  {passed}/{len(results)} passed")
json.dump([{"test": n, "ok": ok, "detail": d} for n, ok, d in results],
          open("/tmp/v21_acceptance.json", "w"), indent=1)
sys.exit(0 if passed == len(results) else 1)
