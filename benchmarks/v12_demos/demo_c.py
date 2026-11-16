"""Demo C -- Interrupted Mission: a mission is stopped mid-plan, the backend is
killed, and on relaunch the saved mission is recovered without replaying work
whose side effect already landed."""
from __future__ import annotations

import json, os, re, shutil, signal, subprocess, sys, time, urllib.error, urllib.request

BASE = "http://127.0.0.1:8798"
TOKEN_PATH = "/tmp/vantage_probe2/api_token"


def token():
    return open(TOKEN_PATH).read().strip()


def call(method, path, body=None):
    hdr = {"X-JARVIS-Token": token(), "Host": "127.0.0.1",
           "Content-Type": "application/json"}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=hdr, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


D = "/tmp/demo_c"
shutil.rmtree(D, ignore_errors=True); os.makedirs(D, exist_ok=True)
STEP1 = D + "/first.txt"
STEP2 = D + "/second.txt"

print("== 1. open a mission with an idempotent first step and a later one ==")
st, mission = call("POST", "/missions", {
    "request": "write two files, one after the other",
    "scope": {"root": D},
    "conditions": [{"condition_id": "c1", "statement": "both files exist",
                    "verifier": {"kind": "file_contains", "path": STEP1,
                                 "needle": "first"}}],
    "steps": [
        {"step_id": "s1", "title": "write the first file", "tool": "write_file",
         "params": {"path": STEP1, "content": "first\n", "mode": "overwrite"},
         "satisfies": "c1", "idempotency_key": "demo-c-first"},
        {"step_id": "s2", "title": "write the second file", "tool": "write_file",
         "params": {"path": STEP2, "content": "second\n", "mode": "overwrite"},
         "idempotency_key": "demo-c-second", "depends_on": ["s1"]},
    ],
})
MID = mission["mission_id"]
print("mission", MID, "state", mission.get("state"))

print("\n== 2. run it: it parks at the first step's approval ==")
st, parked = call("POST", f"/missions/{MID}/run", {})
print("state:", parked.get("state"))
for s in parked.get("steps", []):
    print("  ", s["step_id"], s["status"], "|", str(s["detail"])[:120])

conf = None
for s in parked.get("steps", []):
    m = re.search(r"confirmation\s+(\S+)", str(s.get("detail", "")))
    if m:
        conf = m.group(1)
print("confirmation:", conf)

print("\n== 3. approve: the first step is an idempotent side effect that lands ==")
st, done1 = call("POST", f"/missions/{MID}/run",
                 {"approval": {"confirmation_id": conf}})
print("state:", done1.get("state"), "verdict:", done1.get("verdict"))
for s in done1.get("steps", []):
    print("  ", s["step_id"], s["status"], "|", str(s["detail"])[:120])
print("first file on disk:", os.path.exists(STEP1), "| second:",
      os.path.exists(STEP2))
assert os.path.exists(STEP1)
assert not os.path.exists(STEP2), "the second step should still be pending"

print("\n== 4. kill the backend while the mission is mid-flight ==")
pid = subprocess.run(["lsof", "-ti", "tcp:8798"], capture_output=True,
                     text=True).stdout.split()
for p in pid:
    os.kill(int(p), signal.SIGKILL)
time.sleep(1.5)
try:
    urllib.request.urlopen(BASE + "/health", timeout=1)
    print("   backend still up -- kill failed")
except Exception as exc:
    print("   backend is down:", type(exc).__name__)

print("\n== 5. relaunch it ==")
env = dict(os.environ)
env.update({"JARVIS_HOME": "/tmp/vantage_probe2", "JARVIS_BACKEND_PORT": "8798"})
proc = subprocess.Popen(
    [".venv/bin/python", "-m", "uvicorn", "jarvis.api.app:app",
     "--host", "127.0.0.1", "--port", "8798"],
    cwd="/Users/abhijeetmiskin/AppData/Vantage",
    env=env, stdout=open("/tmp/probe_restart.log", "a"),
    stderr=subprocess.STDOUT, start_new_session=True)
for _ in range(60):
    try:
        with urllib.request.urlopen(BASE + "/health", timeout=1) as r:
            if r.status == 200:
                break
    except Exception:
        time.sleep(0.25)
print("   backend is back")

print("\n== 6. the saved mission is recovered, not lost ==")
st, interrupted = call("GET", "/missions/interrupted")
ids = [m.get("mission_id") for m in interrupted.get("missions", [])]
print("interrupted missions:", len(ids))
print("our mission is listed:", MID in ids)
st, recovered = call("GET", f"/missions/{MID}")
print("state:", recovered.get("state"))
for s in recovered.get("steps", []):
    print("  ", s["step_id"], s["status"], "|", str(s["detail"])[:120])

print("\n== 7. resume: the completed side effect is not replayed ==")
mtime_before = os.stat(STEP1).st_mtime_ns
st, resumed = call("POST", f"/missions/{MID}/run", {})
print("run ->", st, resumed.get("state"))
conf2 = None
for s in resumed.get("steps", []):
    m = re.search(r"confirmation\s+(\S+)", str(s.get("detail", "")))
    if m:
        conf2 = m.group(1)
print("confirmation for the remaining step:", conf2)
if conf2:
    st, finished = call("POST", f"/missions/{MID}/run",
                        {"approval": {"confirmation_id": conf2}})
    print("after approval: state", finished.get("state"),
          "verdict", finished.get("verdict"))

mtime_after = os.stat(STEP1).st_mtime_ns
print("first file was not rewritten:", mtime_before == mtime_after)
print("second file now exists:", os.path.exists(STEP2))

print("\n== 8. accurate completion evidence ==")
st, final = call("GET", f"/missions/{MID}")
print("state:", final.get("state"), "verdict:", final.get("verdict"))
for s in final.get("steps", []):
    print("  ", s["step_id"], s["status"], "|", str(s["detail"])[:140])
for c in final.get("conditions", []):
    print("  cond", c["condition_id"], c["status"], "|",
          str(c["observation"])[:140])
st, proof = call("GET", f"/missions/{MID}/proof")
print("proof unsupported_claims:", proof.get("unsupported_claims"))
print("evidence_integrity:", str(proof.get("evidence_integrity"))[:180])
print("\nDEMO_C_OK")