#!/usr/bin/env python3
"""A desktop action through the real mission path, verified at the target.

The mission runner reports a step `done` when the executor accepted the request.
That is not the same as the target responding -- measured: a click reported `done`
while the fixture's counter did not move, because focus had moved to another
application during the approval exchanges.

So this script does what the product must: assert focus immediately before the
mission runs, and then confirm the *target* changed rather than trusting the
step status.

    python3 benchmarks/v21_desktop/mission_desktop.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8798"
TOKEN_FILE = Path("/tmp/vantage_probe2/api_token")
STATE = Path("/tmp/vantage_probe_app/state.json")
EXE = (Path(__file__).resolve().parents[2] / "native" / "desktop_executor"
       / "VantageDesktopDev.app" / "Contents" / "MacOS" / "vantage-desktop")

if not TOKEN_FILE.exists():
    print("the probe backend is not running; start it first")
    sys.exit(2)
HEADERS = {"X-JARVIS-Token": TOKEN_FILE.read_text().strip(),
           "Host": "127.0.0.1", "Content-Type": "application/json"}


def call(method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=HEADERS, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def fixture() -> dict:
    for _ in range(40):
        try:
            return json.loads(STATE.read_text())
        except (OSError, json.JSONDecodeError):
            time.sleep(0.05)
    return {}


def focus_fixture() -> bool:
    for _ in range(6):
        out = subprocess.run([str(EXE), "windows"], capture_output=True, text=True).stdout
        try:
            if "vantage-probe" in json.loads(out).get("frontmost_application", "").lower():
                return True
        except json.JSONDecodeError:
            pass
        subprocess.run(["osascript", "-e",
                        'tell application "System Events" to set frontmost of '
                        '(first process whose name contains "vantage-probe") to true'],
                       capture_output=True)
        time.sleep(0.4)
    return False


if not STATE.exists():
    print("the fixture is not running; start tools/input_probe/vantage-probe first")
    sys.exit(2)

target = fixture()["targets"]["click"]
before = fixture()["clicks"]

if not focus_fixture():
    print("the fixture will not come to the front; not dispatching input")
    sys.exit(2)

status, mission = call("POST", "/missions", {
    "request": "click the fixture button",
    "conditions": [],
    "steps": [{"step_id": "S1", "title": "click", "tool": "desktop_click",
               "params": {"x": target["x"], "y": target["y"]}}],
})
if status != 200:
    print(f"mission could not be created: {mission.get('detail')}")
    sys.exit(1)

mid = mission["mission_id"]
_, out = call("POST", f"/missions/{mid}/run", {})
import re
for _ in range(4):
    if out.get("state") != "awaiting_approval":
        break
    conf = None
    for step in out.get("steps", []):
        m = re.search(r"confirmation\s+(\S+)", str(step.get("detail", "")))
        if m:
            conf = m.group(1)
    if not conf:
        break
    _, out = call("POST", f"/missions/{mid}/run", {"approval": {"confirmation_id": conf}})

time.sleep(0.7)
after = fixture()["clicks"]
step_status = (out.get("steps") or [{}])[0].get("status")

print(f"  mission state   : {out.get('state')}")
print(f"  step status     : {step_status}")
print(f"  fixture counter : {before} -> {after}")

# The step being `done` is not the result. The counter moving is.
ok = after == before + 1
print(f"  {'PASS' if ok else 'FAIL'}: the target "
      f"{'changed' if ok else 'did NOT change'} after the mission ran")
sys.exit(0 if ok else 1)
