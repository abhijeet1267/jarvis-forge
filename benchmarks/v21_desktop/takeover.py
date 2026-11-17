#!/usr/bin/env python3
"""Take over during real input: every desktop action must be refused.

Takeover was demonstrated before at the HTTP layer for a move alone. This checks
all four kinds, because a pause that stops clicks but still types would be worse
than none -- the person has the mouse but the machine is still writing.

    python3 benchmarks/v21_desktop/takeover.py
"""
import json, subprocess, time, urllib.request, urllib.error
from pathlib import Path
BASE = "http://127.0.0.1:8798"
H = {"X-JARVIS-Token": Path("/tmp/vantage_probe2/api_token").read_text().strip(),
     "Host": "127.0.0.1", "Content-Type": "application/json"}
def call(m, p, b=None):
    d = json.dumps(b).encode() if b is not None else None
    r = urllib.request.Request(BASE + p, data=d, headers=H, method=m)
    try:
        with urllib.request.urlopen(r, timeout=20) as x: return x.status, json.loads(x.read() or b"{}")
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read() or b"{}")

# Take over, then try a desktop action of each kind.
st, taken = call("POST", "/reality/takeover", {})
print(f"  takeover: paused {len(taken.get('paused', []))} worker(s)")
held = taken.get("released_input") or {}
print(f"  held-input release reported: {held.get('status')}")

codes = {}
for name, body in (("move", {"x": 400, "y": 300}),
                   ("click", {"x": 400, "y": 300}),
                   ("type", {"text": "should not appear"}),
                   ("scroll", {"x": 400, "y": 300, "lines": -3})):
    code, _ = call("POST", f"/desktop/{name}", body)
    codes[name] = code
print(f"  during takeover: {codes}")

call("POST", "/reality/release", {})
ok = all(c == 409 for c in codes.values())
print(f"  {'PASS' if ok else 'FAIL'}: every desktop action refused during takeover")
raise SystemExit(0 if ok else 1)
