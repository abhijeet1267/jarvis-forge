#!/usr/bin/env bash
# Render the native macOS PlannerView offscreen against a real backend with
# seeded data, and write a PNG. Verifies the native screen actually renders
# (screencapture / Accessibility are refused in this environment).
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$(cd "$DIR/.." && pwd)"
REPO="$(cd "$APP/../.." && pwd)"
OUT="${1:-$REPO/artifacts/home-native.png}"
TMP="$(mktemp -d)"
# A `trap` rather than a trailing rm: an interrupted run (Ctrl-C, a failed
# compile, set -e bailing out) otherwise leaves a scratch dir behind. This is the
# same class as the server leak -- cleanup that only works on the happy path.
cleanup() {
  PORT_GUARD="$(cat "$TMP/port" 2>/dev/null || true)"
  if [ -n "${PORT_GUARD:-}" ]; then
    lsof -ti ":$PORT_GUARD" 2>/dev/null | xargs -r kill -9 2>/dev/null || true
  fi
  rm -rf "$TMP"
}
trap cleanup EXIT
PY="${PYTHON:-$REPO/.venv/bin/python}"

# 1. A real backend on a scratch home.
"$PY" - "$TMP" "$REPO" <<'PYEOF' &
import json, os, sys, time, tempfile, urllib.request
from pathlib import Path
os.chdir(sys.argv[2])
sys.path.insert(0, str(Path(sys.argv[2]) / "tests"))
from isolated_server import start_server, stop_server, session_token
home = Path(sys.argv[1])
srv = start_server(home)
base = srv["base"]
Path(sys.argv[1], "port").write_text(str(srv["port"]))
Path(sys.argv[1], "token").write_text(session_token(base))
# Seed tasks + a schedule through the real routes.
tok = Path(sys.argv[1], "token").read_text()
def post(path, body):
    req = urllib.request.Request(base + path, method="POST",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-JARVIS-Token": tok})
    urllib.request.urlopen(req).read()
for title, prio in (("Finish the acceptance report", "urgent"),
                    ("Review the PR from Priya", "high"),
                    ("Book the AMR demo", "normal")):
    post("/planner/tasks", {"title": title, "priority": prio})
post("/planner/tasks", {"title": "Late thing", "due_at": "2020-01-01T09:00:00"})

post("/schedules", {"action": "briefing", "schedule": "every morning at 8"})
Path(sys.argv[1], "ready").write_text("1")
Path(sys.argv[1], "pid").write_text(str(__import__("os").getpid()))
# The *server's* pid too. Killing only the seeder left start_server()'s child
# running -- stale backends accumulated across renders before this.
Path(sys.argv[1], "srv_pid").write_text(str(srv["process"].pid))
# Stay alive until the runner says it is done. bash feeds this heredoc through
# a pipe, so Python sees EOF the moment the script text ends -- and the old
# atexit handler then stopped the server *before* the renderer ever connected.
# Every seeded render was silently drawing the dead-server screen while still
# reporting success.
stop = Path(sys.argv[1], "stop")
deadline = time.time() + 300
while not stop.exists() and time.time() < deadline:
    time.sleep(0.2)
stop_server(srv)
PYEOF
SEEDER=$!

for _ in $(seq 1 80); do [ -f "$TMP/ready" ] && break; sleep 0.25; done
if [ ! -f "$TMP/ready" ]; then echo "FAIL: backend did not start"; exit 1; fi
PORT="$(cat "$TMP/port")"; TOKEN="$(cat "$TMP/token")"

# 2. Compile the real app sources + the renderer.
cp "$DIR/render_home_view.swift" "$TMP/main.swift"
"$DIR/build_render.sh" "$TMP/main.swift" "$TMP/render"

# 3. Run it pointed at the backend.
mkdir -p "$TMP/home"
printf "%s" "$TOKEN" > "$TMP/home/api_token"
JARVIS_BACKEND_PORT="$PORT" JARVIS_HOME="$TMP/home" "$TMP/render" "$OUT" || true
touch "$TMP/stop" 2>/dev/null || true
kill "$SEEDER" 2>/dev/null || true
wait "$SEEDER" 2>/dev/null || true          # let its atexit run stop_server
kill "$(cat "$TMP/pid" 2>/dev/null)" 2>/dev/null || true
kill "$(cat "$TMP/srv_pid" 2>/dev/null)" 2>/dev/null || true
PORT_GUARD="$(cat "$TMP/port" 2>/dev/null)"
if [ -n "$PORT_GUARD" ]; then
  lsof -ti ":$PORT_GUARD" 2>/dev/null | xargs -r kill -9 2>/dev/null || true
fi
rm -rf "$TMP"
[ -f "$OUT" ] && echo "rendered $OUT" || { echo "FAIL: no PNG produced"; exit 1; }