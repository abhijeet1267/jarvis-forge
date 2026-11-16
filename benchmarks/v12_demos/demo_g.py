"""Demo G -- Autonomy Dial: one mission in several modes, real permissions differ."""
from __future__ import annotations

import json, os, re, shutil, urllib.error, urllib.request

BASE = "http://127.0.0.1:8798"
TOK = open("/tmp/vantage_probe2/api_token").read().strip()
HDR = {"X-JARVIS-Token": TOK, "Host": "127.0.0.1", "Content-Type": "application/json"}


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=HDR, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


print("== 1. the dial as it stands ==")
st, posture = call("GET", "/autonomy")
print(json.dumps(posture, indent=1)[:1400])

print("\n== 2. the same tool through the real engine at each level ==")
results = {}
for level in (0, 1, 2, 3):
    rows = {}
    for tool in ("read_file", "write_file", "delete_file"):
        st, prev = call("POST", "/autonomy/preview", {
            "level": level, "tool": tool,
            "params": {"path": "/tmp/demo_g/x.txt", "content": "y"},
        })
        if st == 200:
            final = prev.get("final") or {}
            rows[tool] = (str(final.get("decision")), str(final.get("reason"))[:70])
        else:
            rows[tool] = ("ERROR", str(prev.get("detail"))[:70])
    results[level] = rows
    print(f"  level {level}:")
    for tool, (dec, why) in rows.items():
        print(f"    {tool:14s} -> {dec:8s} {why}")

print("\n== 3. the dial actually moves ==")
for level in (1, 3, 1):
    st, setting = call("POST", "/autonomy", {"level": level, "note": "demo G"})
    print(f"  set {level} -> status {st} level {setting.get('level')}")
    assert st == 200, setting

print("\n== 4. a real run's permission changes with the dial ==")
D = "/tmp/demo_g"
shutil.rmtree(D, ignore_errors=True); os.makedirs(D, exist_ok=True)
TARGET = D + "/source.txt"
open(TARGET, "w").write("the contents a read returns\n")

def run_read_at(level):
    call("POST", "/autonomy", {"level": level, "note": "demo G"})
    st, m = call("POST", "/missions", {
        "request": "read source.txt",
        "scope": {"root": D},
        "conditions": [{"condition_id": "c1",
                        "statement": "the file is unchanged",
                        "verifier": {"kind": "file_contains",
                                     "path": TARGET,
                                     "needle": "contents"}}],
        "steps": [{"step_id": "s1", "title": "read it", "tool": "read_file",
                   "params": {"path": TARGET}, "satisfies": "c1",
                   "idempotency_key": f"demo-g-read-{level}"}],
    })
    mid = m["mission_id"]
    st, out = call("POST", f"/missions/{mid}/run", {})
    return out

# The tool is a read, so its own risk says "execute". Only the dial can change
# that. Level 0 must park it; level 1 must run it through.
for level in (0, 1):
    out = run_read_at(level)
    steps = out.get("steps", [])
    print(f"  level {level}: state={out.get('state')} verdict={out.get('verdict')}")
    for s in steps:
        print(f"    step {s['step_id']} {s['status']} | {str(s['detail'])[:140]}")

print("  -> the same read is refused at level 0 and runs at level 1:",
      run_read_at(0).get("state") != run_read_at(1).get("state"))

print("\n== 5. no level crosses the hard ceiling ==")
st, high = call("POST", "/autonomy", {"level": 99})
print("set 99 -> status", st, str(high.get("detail"))[:160])

print("\n== 6. leave the dial where it was ==")
st, restored = call("POST", "/autonomy", {"level": 1, "note": "restored after demo G"})
print("level now:", restored.get("level"))
print("\nDEMO_G_OK")