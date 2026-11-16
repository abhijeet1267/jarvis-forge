"""Demo H -- Temporal Undo: reversible action, preview, safe undo, conflict refusal."""
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


def write_via_mission(path, content, tag):
    st, m = call("POST", "/missions", {
        "request": f"write {tag}",
        "scope": {"root": "/tmp"},
        "steps": [{"step_id": "s1", "title": f"write {tag}", "tool": "write_file",
                   "params": {"path": path, "content": content, "mode": "overwrite"},
                   "idempotency_key": tag}],
    })
    mid = m["mission_id"]
    st, parked = call("POST", f"/missions/{mid}/run", {})
    import re
    conf = None
    for s in parked.get("steps", []):
        mm = re.search(r"confirmation\s+(\S+)", s.get("detail", ""))
        if mm:
            conf = mm.group(1)
    st, done = call("POST", f"/missions/{mid}/run",
                    {"approval": {"confirmation_id": conf}})
    return done


D = "/tmp/demo_h"
shutil.rmtree(D, ignore_errors=True); os.makedirs(D, exist_ok=True)
F = D + "/notes.txt"
open(F, "w").write("original contents\n")
print("== 1. before:", repr(open(F).read()))

print("\n== 2. reversible action (real write through the gate) ==")
done = write_via_mission(F, "edited by the agent\n", "demo-h-first")
print("mission state:", done.get("state"), done.get("verdict"))
print("file now:", repr(open(F).read()))

st, undo = call("GET", "/undo")
entries = [e for e in undo["entries"] if e.get("path") == F and e.get("state") == "reversible"]
print("\n== 3. preview: the map offers an exact restore ==")
print("counts:", undo["counts"])
for e in entries[:2]:
    print("  ", e["entry_id"], e["state"], "|", e["why"])
    print("     undo_kind:", e["undo_kind"], "current_sha256:", e["current_sha256"][:16])
assert entries, "no reversible entry for the write"

target = entries[0]
print("\n== 4. undo it ==")
st, res = call("POST", f"/undo/{target['entry_id']}", {})
print("status", st, json.dumps(res, indent=2)[:900])
print("file after undo:", repr(open(F).read()))
print("restored exactly:", open(F).read() == "original contents\n")

print("\n== 5. conflict refusal: the file moved on after the action ==")
done2 = write_via_mission(F, "second edit\n", "demo-h-second")
print("mission:", done2.get("state"))
st, undo2 = call("GET", "/undo")
cand = [e for e in undo2["entries"]
        if e.get("path") == F and e.get("state") == "reversible"
        and e["entry_id"] != target["entry_id"]]
print("candidate entry:", cand[0]["entry_id"] if cand else "NONE")
assert cand, "no candidate for the conflict test"
victim = cand[0]
# A person edits the file by hand after the action: restoring the pre-image
# would silently discard their work.
open(F, "w").write("HUMAN EDIT, DO NOT LOSE\n")
st, re_read = call("GET", "/undo")
now = [e for e in re_read["entries"] if e["entry_id"] == victim["entry_id"]]
print("state after human edit:", now[0]["state"], "|", now[0]["why"])
print("counts:", re_read["counts"])
st, refusal = call("POST", f"/undo/{victim['entry_id']}", {})
print("undo status:", st)
print("refusal:", json.dumps(refusal, indent=2)[:600])
print("human edit still on disk:", repr(open(F).read()))
assert open(F).read() == "HUMAN EDIT, DO NOT LOSE\n", "undo overwrote newer user work"
print("\nDEMO_H_OK")