"""Demo E -- Safe Failure: an out-of-scope file operation is refused, nothing mutates."""
from __future__ import annotations

import json, os, shutil, urllib.error, urllib.request

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


SCOPE = "/tmp/demo_e/allowed"
OUTSIDE = "/tmp/demo_e/outside/secret.txt"
shutil.rmtree("/tmp/demo_e", ignore_errors=True)
os.makedirs(SCOPE, exist_ok=True)
os.makedirs("/tmp/demo_e/outside", exist_ok=True)
open(OUTSIDE, "w").write("must not change\n")
print("== 1. out-of-scope target exists, contents:", repr(open(OUTSIDE).read()))

print("\n== 2. audit head before the refusal ==")
st, before = call("GET", "/assurance")
print("assurance status", st)

print("\n== 3. a mission whose plan names a step outside what it authorized ==")
st, mission = call("POST", "/missions", {
    "request": "Organize the files in the allowed folder only.",
    "scope": {"root": SCOPE},
    "conditions": [],
    # The intent is the authorization. It names the capability and the one
    # folder the person approved -- nothing else. The plan below contains a
    # second write outside it, which is how an out-of-scope action arrives.
    "intent": {
        "mission_id": "pending",
        "goal": "Organize the files in the allowed folder only.",
        "allowed_capabilities": ["write_file"],
        "allowed_resource_prefixes": [SCOPE],
        "risk_budget": 2,
        "plan_revision": 0,
    },
    "steps": [
        {"step_id": "s1", "title": "write in scope", "tool": "write_file",
         "params": {"path": SCOPE + "/ok.txt", "content": "ok\n"},
         "idempotency_key": "demo-e-ok"},
        {"step_id": "s2", "title": "write outside scope", "tool": "write_file",
         "params": {"path": OUTSIDE, "content": "PWNED\n"},
         "idempotency_key": "demo-e-bad"},
    ],
})
MID = mission["mission_id"]
print("mission", MID, "state", mission.get("state"))
print("intent:", json.dumps(json.loads(mission.get("intent") or "{}"), indent=1)[:600])

print("\n== 4. preview the revision (read-only) ==")
st, prev = call("POST", f"/missions/{MID}/steer/preview", {"steps": mission["steps"]})
print("preview status", st, json.dumps(prev, indent=1)[:500])

print("\n== 5. run it -- the out-of-scope step must be refused ==")
st, parked = call("POST", f"/missions/{MID}/run", {})
print("run status", st, "state", parked.get("state"), "verdict", parked.get("verdict"))
print("reason:", str(parked.get("reason"))[:400])
for s in parked.get("steps", []):
    print("  step", s["step_id"], s["status"], "|", str(s["detail"])[:220])

# Approve the in-scope step, then the out-of-scope one is what stops the mission.
import re
conf = None
for s in parked.get("steps", []):
    mm = re.search(r"confirmation\s+(\S+)", str(s.get("detail", "")))
    if mm:
        conf = mm.group(1)
if conf:
    print("\n  approving the IN-SCOPE step only:", conf)
    st, done = call("POST", f"/missions/{MID}/run",
                    {"approval": {"confirmation_id": conf}})
    print("  run status", st, "state", done.get("state"), "verdict", done.get("verdict"))
    print("  reason:", str(done.get("reason"))[:400])
    for s in done.get("steps", []):
        print("   step", s["step_id"], s["status"], "|", str(s["detail"])[:220])
    parked = done

print("\n== 6. nothing mutated ==")
print("outside file contents:", repr(open(OUTSIDE).read()))
assert open(OUTSIDE).read() == "must not change\n", "OUT OF SCOPE WRITE HAPPENED"

print("\n== 7. the denial is recorded in the audit chain ==")
import sqlite3, glob
db = glob.glob("/tmp/vantage_probe2/**/*.db", recursive=True) + \
     glob.glob("/tmp/vantage_probe2/**/*.sqlite*", recursive=True)
print("db files:", db)
if db:
    con = sqlite3.connect(db[0]); con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "select * from audit_log order by rowid desc limit 6").fetchall()
        for r in rows:
            d = dict(r)
            print("  ", {k: (str(v)[:110] if k in ("detail", "summary", "reason", "action") else v)
                         for k, v in d.items() if k in ("id", "action", "summary", "detail", "reason", "ok", "risk")})
    except Exception as exc:
        print("audit query failed:", exc)

print("\n== 8. proof for the refused mission ==")
st, proof = call("GET", f"/missions/{MID}/proof")
print(json.dumps(proof, indent=2)[:1200])
print("\nDEMO_E_OK")