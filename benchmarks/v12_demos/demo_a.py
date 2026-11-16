"""Demo A -- File Mission, end to end against the live backend.

Organize a sample project, update its documentation, show modifications before
applying them (the parked approval), then apply and independently verify.
"""
from __future__ import annotations

import json, os, re, shutil, sys, urllib.request

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


ROOT = "/tmp/demo_a_project"
shutil.rmtree(ROOT, ignore_errors=True)
os.makedirs(ROOT + "/src", exist_ok=True)
os.makedirs(ROOT + "/docs", exist_ok=True)
open(ROOT + "/src/app.py", "w").write("def main():\n    print('hello')\n")
open(ROOT + "/src/util.py", "w").write("def add(a, b):\n    return a + b\n")
open(ROOT + "/README.md", "w").write("# Sample\n\nOld docs.\n")
print("== project seeded ==")
for dirpath, _, files in os.walk(ROOT):
    for f in sorted(files):
        p = os.path.join(dirpath, f)
        print("  ", p)

NEW_README = (
    "# Sample Project\n\n"
    "A tiny sample with two modules.\n\n"
    "## Layout\n\n"
    "* `src/app.py` -- entry point\n"
    "* `src/util.py` -- helpers\n\n"
    "## Run\n\n```bash\npython src/app.py\n```\n"
)

print("\n== 1. mission contract ==")
st, mission = call("POST", "/missions", {
    "request": "Organize this sample project, update its documentation, "
               "and show me all modifications before applying them.",
    "outcome": "README documents the layout; no other file is touched.",
    "scope": {"root": ROOT},
    "conditions": [{
        "condition_id": "c1",
        "statement": "README.md names both modules and the run command",
        "verifier": {"kind": "file_contains", "path": ROOT + "/README.md",
                     "needle": "src/util.py"},
    }],
    "steps": [{
        "step_id": "s1",
        "title": "Update README documentation",
        "tool": "write_file",
        "params": {"path": ROOT + "/README.md", "content": NEW_README,
                   "mode": "overwrite"},
        "satisfies": "c1",
        "idempotency_key": "demo-a-readme",
    }],
})
print("status", st, "state", mission.get("state"), "id", mission.get("mission_id"))
MID = mission["mission_id"]
assert st == 200, mission

print("\n== 2. README BEFORE ==")
print(repr(open(ROOT + "/README.md").read()))

print("\n== 3. run -- must park at approval, applying nothing ==")
st, parked = call("POST", f"/missions/{MID}/run", {})
print("status", st, "state", parked.get("state"))
print("reason:", parked.get("reason"))
for s in parked.get("steps", []):
    print("  step", s["step_id"], s["status"], "|", s["detail"])
print("README AFTER PARKED RUN:", repr(open(ROOT + "/README.md").read()))
assert open(ROOT + "/README.md").read() == "# Sample\n\nOld docs.\n", \
    "the run applied a change before approval"

conf = None
for s in parked.get("steps", []):
    m = re.search(r"confirmation\s+([A-Za-z0-9\-_]+)", s.get("detail", ""))
    if m:
        conf = m.group(1)
print("\nrecorded confirmation id:", conf)
assert conf, "no confirmation id was recorded on the step"

print("\n== 4. preview -- mission is not touched by a shadow ==")
st, shadow = call("POST", f"/missions/{MID}/shadow", {})
print("status", st)
print(json.dumps(shadow, indent=2)[:1400])

print("\n== 5. approve with the recorded id ==")
st, done = call("POST", f"/missions/{MID}/run", {"approval": {"confirmation_id": conf}})
print("status", st, "state", done.get("state"), "verdict", done.get("verdict"))
print("reason:", done.get("reason"))
for s in done.get("steps", []):
    print("  step", s["step_id"], s["status"], "|", s["detail"][:160])
for c in done.get("conditions", []):
    print("  cond", c["condition_id"], c["status"], "|", c["observation"][:160])

print("\n== 6. file actually on disk ==")
on_disk = open(ROOT + "/README.md").read()
print(on_disk)
print("matches intended:", on_disk == NEW_README)
import hashlib
print("sha256:", hashlib.sha256(on_disk.encode()).hexdigest())
print("util.py untouched:", open(ROOT + "/src/util.py").read() == "def add(a, b):\n    return a + b\n")

print("\n== 7. proof (ProofLens evidence) ==")
st, proof = call("GET", f"/missions/{MID}/proof")
print("status", st)
print(json.dumps(proof, indent=2)[:1800])

print("\n== 8. undo map knows the change ==")
st, undo = call("GET", "/undo")
rows = undo.get("entries", undo.get("rows", []))
print("status", st, "rows", len(rows))
for r in rows[:5]:
    print("  ", r.get("kind"), r.get("status"), r.get("target") or r.get("path"),
          "|", str(r.get("note", ""))[:90])
print("\nDEMO_A_MISSION", MID)