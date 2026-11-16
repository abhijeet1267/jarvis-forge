"""Demo J -- Human Takeover: pause the machine, a real agent call is refused,
the human does the work, the machine is handed back, and work resumes."""
from __future__ import annotations

import json, os, re, shutil, urllib.error, urllib.request

BASE = "http://127.0.0.1:8798"
TOK = open("/tmp/vantage_probe2/api_token").read().strip()
HDR = {"X-JARVIS-Token": TOK, "Host": "127.0.0.1", "Content-Type": "application/json"}


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=HDR, method=method)
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


D = "/tmp/demo_j"
shutil.rmtree(D, ignore_errors=True); os.makedirs(D, exist_ok=True)
TARGET = D + "/handover.txt"

def attempt_write(content):
    st, m = call("POST", "/missions", {
        "request": "write the handover file",
        "scope": {"root": D},
        "steps": [{"step_id": "s1", "title": "write", "tool": "write_file",
                   "params": {"path": TARGET, "content": content,
                              "mode": "overwrite"},
                   "idempotency_key": "demo-j"}],
    })
    mid = m["mission_id"]
    st, parked = call("POST", f"/missions/{mid}/run", {})
    conf = None
    for s in parked.get("steps", []):
        mm = re.search(r"confirmation\s+(\S+)", str(s.get("detail", "")))
        if mm:
            conf = mm.group(1)
    if conf:
        st, out = call("POST", f"/missions/{mid}/run",
                       {"approval": {"confirmation_id": conf}})
        return out
    return parked


print("== 1. the machine before a takeover ==")
st, hud = call("GET", "/reality")
print("takeover held:", hud["takeover"]["held"])
print("enforced at:", hud["takeover"]["enforced_at"])
print("cannot:", hud["takeover"]["cannot"])

print("\n== 2. take over -- every worker is paused ==")
st, taken = call("POST", "/reality/takeover", {"reason": ""})
print("paused:", len(taken["paused"]), "workers")
print("effect:", taken["effect"])
print("cannot:", taken["cannot"])
print("left alone:", taken["already_paused_by_someone_else"])

print("\n== 3. an unsupported step is attempted while the human holds it ==")
before = TARGET if os.path.exists(TARGET) else None
refused = attempt_write("written by the agent while paused\n")
print("mission state:", refused.get("state"))
print("reason:", str(refused.get("reason"))[:300])
for s in refused.get("steps", []):
    print("  step", s["step_id"], s["status"], "|", str(s["detail"])[:220])
print("file exists after the refused attempt:", os.path.exists(TARGET))
assert not os.path.exists(TARGET), "the agent wrote while taken over"

print("\n== 4. the human handles it, under their own control ==")
open(TARGET, "w").write("written by the human during the takeover\n")
print("file now:", repr(open(TARGET).read()))

print("\n== 5. the HUD reports the refused call ==")
st, hud2 = call("GET", "/reality")
print("takeover held:", hud2["takeover"]["held"])
print("held workers:", len(hud2["takeover"]["held_workers"]))
print("refused total:", hud2["workers"]["refused_total"])
print("paused workers:", hud2["workers"]["paused"])

print("\n== 6. hand control back ==")
st, released = call("POST", "/reality/release")
print("resumed:", len(released["resumed"]))
print("left paused:", released["left_paused"])
print("note:", released["note"])

print("\n== 7. work resumes: the same step now runs ==")
ok = attempt_write("resumed after the takeover\n")
print("mission state:", ok.get("state"), "verdict", ok.get("verdict"))
for s in ok.get("steps", []):
    print("  step", s["step_id"], s["status"], "|", str(s["detail"])[:120])
print("file now:", repr(open(TARGET).read()))
print("\nDEMO_J_OK")