#!/usr/bin/env python3
"""Record a desktop workflow in Automation Studio, then replay it for real.

The recorder already knew desktop action kinds; what was never established is
that a *saved* workflow performs actual input on replay. This records a click,
derives a draft, and runs that draft through the mission runner -- then checks
the target's own counter, because a step reporting `done` only means the executor
accepted the request.

    python3 benchmarks/v21_desktop/studio_replay.py
"""
import json, subprocess, time, urllib.request, urllib.error
from pathlib import Path

BASE = "http://127.0.0.1:8798"
H = {"X-JARVIS-Token": Path("/tmp/vantage_probe2/api_token").read_text().strip(),
     "Host": "127.0.0.1", "Content-Type": "application/json"}
STATE = Path("/tmp/vantage_probe_app/state.json")
EXE = Path("native/desktop_executor/VantageDesktopDev.app/Contents/MacOS/vantage-desktop")

def call(m, p, b=None):
    data = json.dumps(b).encode() if b is not None else None
    req = urllib.request.Request(BASE + p, data=data, headers=H, method=m)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")

def fx():
    for _ in range(40):
        try: return json.loads(STATE.read_text())
        except Exception: time.sleep(0.05)
    return {}

def focus():
    for _ in range(6):
        o = subprocess.run([str(EXE), "windows"], capture_output=True, text=True).stdout
        try:
            if "vantage-probe" in json.loads(o).get("frontmost_application", "").lower():
                return True
        except Exception: pass
        subprocess.run(["osascript","-e",'tell application "System Events" to set frontmost of (first process whose name contains "vantage-probe") to true'],capture_output=True)
        time.sleep(0.4)
    return False

t = fx()["targets"]["click"]
before = fx()["clicks"]
focus()

# 1. record
call("POST", "/intelligence/recordings", {"name": "v21 click workflow"})
call("POST", "/intelligence/recordings/steps", {
    "kind": "desktop_click", "params": {"x": t["x"], "y": t["y"]},
    "label": "click the fixture button"})
call("POST", "/intelligence/recordings/stop", {})

# 2. derive a draft
st, draft = call("POST", "/intelligence/recordings/draft", {
    "reviewed": [{"index": 0, "params": {"x": t["x"], "y": t["y"]}}],
    "name": "v21 click workflow"})
steps = draft.get("steps") or []
print("  recorded steps:", [(s["tool"], s["params"]) for s in steps])
if not steps:
    print("  FAIL: no draft produced", draft.get("detail")); raise SystemExit(1)

# 3. replay through the mission runner
focus()
st, mis = call("POST", "/missions", {"request": "replay the recorded workflow",
                                     "conditions": [], "steps": steps})
if st != 200:
    print("  FAIL: mission:", mis.get("detail")); raise SystemExit(1)
mid = mis["mission_id"]
_, out = call("POST", f"/missions/{mid}/run", {})
import re
for _ in range(4):
    if out.get("state") != "awaiting_approval": break
    conf = next((m.group(1) for s in out.get("steps", [])
                 for m in [re.search(r"confirmation\s+(\S+)", str(s.get("detail","")))] if m), None)
    if not conf: break
    _, out = call("POST", f"/missions/{mid}/run", {"approval": {"confirmation_id": conf}})
time.sleep(0.8)
after = fx()["clicks"]
print(f"  replay state: {out.get('state')} | step: {[s['status'] for s in out.get('steps',[])]}")
print(f"  counter: {before} -> {after}")
ok = after == before + 1
print(f"  {'PASS' if ok else 'FAIL'}: recorded workflow performed a real click")
raise SystemExit(0 if ok else 1)
