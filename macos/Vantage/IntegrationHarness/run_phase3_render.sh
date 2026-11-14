#!/usr/bin/env bash
# Render one Phase 3 screen offscreen against a real, isolated backend.
#
#   run_phase3_render.sh <capture|documents|briefing|preferences> [output.png]
#
# The backend is started on a scratch home, seeded through the real routes (a
# due task, an overdue task, a capture, a failed flow in the local store) so the
# screens draw populated states rather than three empty cards. Nothing in
# ~/.jarvis-forge is read or written.
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$(cd "$DIR/.." && pwd)"
REPO="$(cd "$APP/../.." && pwd)"
SCREEN="${1:-capture}"
OUT="${2:-$REPO/artifacts/phase3-$SCREEN.png}"
TMP="$(mktemp -d)"
cleanup() {
  PORT_GUARD="$(cat "$TMP/port" 2>/dev/null || true)"
  if [ -n "${PORT_GUARD:-}" ]; then
    lsof -ti ":$PORT_GUARD" 2>/dev/null | xargs -r kill -9 2>/dev/null || true
  fi
  rm -rf "$TMP"
}
trap cleanup EXIT
PY="${PYTHON:-$REPO/.venv/bin/python}"
if [ ! -x "$PY" ]; then echo "SKIP  no python at $PY"; exit 2; fi

# 1. A real backend on a scratch home, seeded through its own routes.
"$PY" - "$TMP" "$REPO" "$SCREEN" <<'PYEOF' &
import json, os, sys, time, urllib.request
from pathlib import Path
os.chdir(sys.argv[2])
sys.path.insert(0, str(Path(sys.argv[2]) / "tests"))
from isolated_server import start_server, stop_server, session_token
home = Path(sys.argv[1])
srv = start_server(home)
base = srv["base"]
Path(sys.argv[1], "port").write_text(str(srv["port"]))
Path(sys.argv[1], "token").write_text(session_token(base))
Path(sys.argv[1], "srv_pid").write_text(str(srv["process"].pid))
tok = Path(sys.argv[1], "token").read_text()
def post(path, body):
    req = urllib.request.Request(base + path, method="POST",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-JARVIS-Token": tok})
    urllib.request.urlopen(req).read()

import datetime
today = datetime.datetime.now().astimezone().replace(hour=23, minute=30).isoformat()
post("/planner/tasks", {"title": "Finish the acceptance report", "priority": "urgent",
                        "due_at": today})
post("/planner/tasks", {"title": "Review the PR from Priya", "priority": "high",
                        "due_at": today})
post("/planner/tasks", {"title": "Book the AMR demo", "priority": "normal"})
post("/planner/tasks", {"title": "Late thing", "priority": "high",
                        "due_at": "2020-01-01T09:00:00"})
Path(sys.argv[1], "ready").write_text("1")
stop = Path(sys.argv[1], "stop")
deadline = time.time() + 240
while not stop.exists() and time.time() < deadline:
    time.sleep(0.2)
stop_server(srv)
PYEOF
SEEDER=$!

for _ in $(seq 1 120); do [ -f "$TMP/ready" ] && break; sleep 0.25; done
if [ ! -f "$TMP/ready" ]; then echo "SKIP  backend did not start"; exit 2; fi
PORT="$(cat "$TMP/port")"; TOKEN="$(cat "$TMP/token")"

# 2. Compile the real app sources + the renderer.
cp "$DIR/render_phase3_view.swift" "$TMP/main.swift"
if ! "$DIR/build_render.sh" "$TMP/main.swift" "$TMP/render" 2>&1 | grep "error:"; then :; else
  echo "FAIL  the renderer did not compile"; exit 1
fi

# 3. Seed the local stores the screens read (capture inbox, flow history,
#    preferences) in the scratch Application Support the app will use.
SUPPORT="$TMP/support"
mkdir -p "$SUPPORT" "$TMP/home"
printf "%s" "$TOKEN" > "$TMP/home/api_token"
if [ "$SCREEN" = "capture" ]; then
  cat > "$SUPPORT/capture.json" <<'JSON'
{"items":[{"id":"11111111-1111-1111-1111-111111111111","content":"Make Vantage open files faster","kind":"idea","status":"inbox","tags":["performance"],"createdAt":"2026-10-28T09:00:00Z","modifiedAt":"2026-10-28T09:00:00Z"},{"id":"22222222-2222-2222-2222-222222222222","content":"Review the project proposal","kind":"taskCandidate","status":"inbox","tags":[],"createdAt":"2026-10-28T10:00:00Z","modifiedAt":"2026-10-28T10:00:00Z","suggestedDue":"2026-10-29T13:00:00Z"},{"id":"33333333-3333-3333-3333-333333333333","content":"Turn the onboarding checklist into a flow","kind":"workflowIdea","status":"reviewed","tags":[],"createdAt":"2026-10-27T14:00:00Z","modifiedAt":"2026-10-28T11:00:00Z"}],"schema":1}
JSON
fi
if [ "$SCREEN" = "preferences" ]; then
  cat > "$SUPPORT/preferences.json" <<'JSON'
{"preferences":[{"id":"44444444-4444-4444-4444-444444444444","kind":"preferredApplication","key":"browser","value":"Safari","enabled":true,"note":"I use Safari for research.","createdAt":"2026-10-27T09:00:00Z","modifiedAt":"2026-10-27T09:00:00Z"},{"id":"55555555-5555-5555-5555-555555555555","kind":"preferredFolder","key":"summary","value":"Downloads","enabled":true,"note":"","createdAt":"2026-10-27T10:00:00Z","modifiedAt":"2026-10-27T10:00:00Z"},{"id":"66666666-6666-6666-6666-666666666666","kind":"summaryFormat","key":"format","value":"brief","enabled":false,"note":"","createdAt":"2026-10-27T11:00:00Z","modifiedAt":"2026-10-27T11:00:00Z"}],"schema":1}
JSON
fi

mkdir -p "$(dirname "$OUT")"
VANTAGE_RENDER_DOC_DIR="$TMP" \
VANTAGE_SUPPORT_DIR="$SUPPORT" \
JARVIS_BACKEND_PORT="$PORT" JARVIS_HOME="$TMP/home" "$TMP/render" "$SCREEN" "$OUT"
STATUS=$?
touch "$TMP/stop" 2>/dev/null || true
kill "$SEEDER" 2>/dev/null || true
wait "$SEEDER" 2>/dev/null || true
PORT_GUARD="$(cat "$TMP/port" 2>/dev/null)"
if [ -n "$PORT_GUARD" ]; then
  lsof -ti ":$PORT_GUARD" 2>/dev/null | xargs -r kill -9 2>/dev/null || true
fi
if [ -f "$OUT" ]; then echo "rendered $OUT"; else echo "FAIL  no PNG produced"; exit 1; fi
exit $STATUS