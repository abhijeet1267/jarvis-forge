#!/usr/bin/env bash
# Run the Phase 3 live check against a real, isolated backend.
#
#   run_phase3_live.sh <compiled-check-binary>
#
# Starts the repo's isolated server on a scratch home (the same helper the
# render harnesses use), seeds nothing, and points the check at it through the
# same env seams the app itself uses (JARVIS_BACKEND_PORT, JARVIS_HOME). The
# scratch home is deleted afterwards; the person's ~/.jarvis-forge is never read
# or written.
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$(cd "$DIR/.." && pwd)"
REPO="$(cd "$APP/../.." && pwd)"
BIN="${1:?usage: run_phase3_live.sh <check-binary>}"
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
if [ ! -x "$PY" ]; then
  echo "SKIP  no python at $PY — live check was NOT RUN"
  exit 2
fi

"$PY" - "$TMP" "$REPO" <<'PYEOF' &
import os, sys, time
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
Path(sys.argv[1], "ready").write_text("1")
stop = Path(sys.argv[1], "stop")
deadline = time.time() + 180
while not stop.exists() and time.time() < deadline:
    time.sleep(0.2)
stop_server(srv)
PYEOF
SEEDER=$!

for _ in $(seq 1 120); do [ -f "$TMP/ready" ] && break; sleep 0.25; done
if [ ! -f "$TMP/ready" ]; then
  echo "SKIP  the isolated backend did not start — live check was NOT RUN"
  touch "$TMP/stop" 2>/dev/null || true
  kill "$SEEDER" 2>/dev/null || true
  exit 2
fi
PORT="$(cat "$TMP/port")"; TOKEN="$(cat "$TMP/token")"

mkdir -p "$TMP/home"
printf "%s" "$TOKEN" > "$TMP/home/api_token"
JARVIS_BACKEND_PORT="$PORT" JARVIS_HOME="$TMP/home" "$BIN"
STATUS=$?

touch "$TMP/stop" 2>/dev/null || true
kill "$SEEDER" 2>/dev/null || true
wait "$SEEDER" 2>/dev/null || true
kill "$(cat "$TMP/srv_pid" 2>/dev/null)" 2>/dev/null || true
PORT_GUARD="$(cat "$TMP/port" 2>/dev/null)"
if [ -n "$PORT_GUARD" ]; then
  lsof -ti ":$PORT_GUARD" 2>/dev/null | xargs -r kill -9 2>/dev/null || true
fi
exit $STATUS