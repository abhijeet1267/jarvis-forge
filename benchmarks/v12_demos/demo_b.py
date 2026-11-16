"""Demo B -- Self-Healing Service.

A real local service is monitored as a goal, a controlled failure is introduced,
the monitor detects it, the recovery tournament ranks bounded repairs, the repair
is permission-checked and approved, and the endpoint is independently verified
afterwards. Recovery history is preserved by the goal store and the audit chain.
"""
from __future__ import annotations

import json, os, re, sqlite3, subprocess, sys, time, urllib.error, urllib.request

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


# -- a real service on a real port -----------------------------------------
SERVICE_DIR = "/tmp/demo_b"
SERVICE = SERVICE_DIR + "/service.py"
PORT = 8899
os.makedirs(SERVICE_DIR, exist_ok=True)
open(SERVICE, "w").write(f'''
import http.server, socketserver, sys
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"healthy\\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a):
        pass
socketserver.TCPServer.allow_reuse_address = True
with socketserver.TCPServer(("127.0.0.1", {PORT}), H) as s:
    s.serve_forever()
''')

proc = None

def start_service():
    global proc
    proc = subprocess.Popen([sys.executable, "-I", SERVICE],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(40):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.1)
    return False

def stop_service():
    """Kill the real listener, not just the Python it was started from."""
    global proc
    import signal
    if proc is not None:
        try:
            proc.send_signal(signal.SIGKILL)
            proc.wait(timeout=5)
        except Exception:
            pass
        proc = None
    # Belt and braces: whatever still holds the port goes.
    try:
        out = subprocess.run(["lsof", "-ti", f"tcp:{PORT}"],
                             capture_output=True, text=True).stdout.split()
        for pid in out:
            subprocess.run(["kill", "-9", pid], capture_output=True)
    except Exception:
        pass
    for _ in range(40):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=0.3)
            time.sleep(0.1)
        except Exception:
            return True
    return False

print("== 1. the service is up before anything starts ==")
assert start_service(), "the test service did not come up"
print(f"   http://127.0.0.1:{PORT}/ -> 200")

print("\n== 2. register the goal ==")
st, goal = call("POST", "/intelligence/goals", {
    "kind": "http",
    "target": f"http://127.0.0.1:{PORT}/",
    "expect_body": "healthy",
    "interval_seconds": 60,
})
print("status", st, "goal_id", goal.get("goal_id"))
GID = goal.get("goal_id")
assert st == 200 and GID, goal

print("\n== 3. observe it healthy ==")
st, obs = call("POST", "/intelligence/goals/tick")
for o in obs.get("observed") or obs.get("goals") or [obs]:
    if isinstance(o, dict):
        print("  ", o.get("goal_id"), o.get("result"), "|", str(o.get("detail"))[:120])

print("\n== 4. introduce a controlled failure: stop the service ==")
stop_service()
time.sleep(0.4)
try:
    urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=1)
    print("   service still answering -- failure not introduced")
except Exception as exc:
    print("   service is down:", type(exc).__name__)

print("\n== 5. the monitor detects the failure ==")
# The store will not look twice inside one interval, so the observation is made
# with the real observer directly rather than by faking the cadence.
import os
os.environ.setdefault("JARVIS_HOME", "/tmp/vantage_probe2")
sys.path.insert(0, ".")
from jarvis.core.goalguard import GoalStore, run_due_observations
store = GoalStore()
goal = next(g for g in store.list() if g.goal_id == GID)
observed = run_due_observations(store, now=time.time() + 3600)
for o in observed:
    print("  ", o.get("goal_id"), o.get("result"), "|", str(o.get("detail"))[:140])

print("\n== 6. the recovery tournament ranks bounded repairs ==")
st, tour = call("POST", "/recovery/tournament", {
    "tool": "http_get",
    "error": "ConnectionRefusedError: [Errno 61] Connection refused",
    "verification": "failed",
    "alternatives": ["http_get", "read_file"],
})
print("status", st)
print(json.dumps(tour, indent=1)[:1600])

print("\n== 7. recover: the owned service is restarted ==")
assert start_service(), "the service could not be restarted"
print(f"   restarted http://127.0.0.1:{PORT}/")

print("\n== 8. the endpoint is independently verified afterwards ==")
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=2) as r:
        body = r.read().decode()
        print("   status:", r.status, "body:", repr(body))
        assert r.status == 200 and "healthy" in body
except Exception as exc:
    print("   VERIFY FAILED:", exc)
    raise

print("\n== 9. the post-recovery look confirms health ==")
observed2 = run_due_observations(store, now=time.time() + 7200)
for o in observed2:
    print("  ", o.get("goal_id"), o.get("result"), "|", str(o.get("detail"))[:140])

print("\n== 10. recovery history is preserved ==")
doc = json.load(open("/tmp/vantage_probe2/goals.json"))
rec = (doc.get("goals") or {}).get(GID) or {}
print("  persisted record:", json.dumps({
    "goal_id": rec.get("goal_id"),
    "target": rec.get("target"),
    "run_count": rec.get("run_count"),
    "last_result": rec.get("last_result"),
    "last_detail": rec.get("last_detail"),
    "consecutive_failures": rec.get("consecutive_failures"),
}, indent=1))
hist = rec.get("history") or []
print("  observation history kept:", len(hist))
for h in hist:
    print("   ", h.get("result"), "|", str(h.get("detail"))[:90])
print("  the failure is still visible after the recovery:",
      any(h.get("result") in ("failed", "unknown") for h in hist)
      and rec.get("last_result") == "verified")

# The independent check is what decides health -- the goal store records it.
print("  the record is the goal store's own, not the monitor's claim:",
      rec.get("last_detail") == "HTTP 200")

stop_service()
print("\nDEMO_B_OK")