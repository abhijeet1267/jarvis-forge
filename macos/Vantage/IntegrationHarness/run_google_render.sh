#!/usr/bin/env bash
# Render the native macOS GoogleView offscreen against a real backend on a
# scratch home (so the honest "not configured" setup branch is what shows) and
# write a PNG. Same pattern as run_connections_render.sh.
#
# Usage: run_google_render.sh [out.png] [-d]
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$(cd "$DIR/.." && pwd)"
REPO="$(cd "$APP/../.." && pwd)"
OUT="${1:-$REPO/artifacts/google-native.png}"
MODE="${2:-}"
case "$OUT" in
  /*) ;;
  *) OUT="$(pwd)/$OUT" ;;
esac
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

"$PY" - "$TMP" "$REPO" <<'PYEOF' &
import sys, time
from pathlib import Path
import os
os.chdir(sys.argv[2])
sys.path.insert(0, str(Path(sys.argv[2]) / "tests"))
from isolated_server import start_server, stop_server, session_token
home = Path(sys.argv[1])
srv = start_server(home)
base = srv["base"]
Path(sys.argv[1], "port").write_text(str(srv["port"]))
Path(sys.argv[1], "token").write_text(session_token(base))
Path(sys.argv[1], "ready").write_text("1")
Path(sys.argv[1], "pid").write_text(str(os.getpid()))
Path(sys.argv[1], "srv_pid").write_text(str(srv["process"].pid))
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

cp "$DIR/render_google.swift" "$TMP/main.swift"
"$DIR/build_render.sh" "$TMP/main.swift" "$TMP/render"

mkdir -p "$TMP/home" "$(dirname "$OUT")"
printf "%s" "$TOKEN" > "$TMP/home/api_token"
JARVIS_BACKEND_PORT="$PORT" JARVIS_HOME="$TMP/home" "$TMP/render" "$OUT" $MODE || true
touch "$TMP/stop" 2>/dev/null || true
kill "$SEEDER" 2>/dev/null || true
wait "$SEEDER" 2>/dev/null || true
kill "$(cat "$TMP/pid" 2>/dev/null)" 2>/dev/null || true
kill "$(cat "$TMP/srv_pid" 2>/dev/null)" 2>/dev/null || true
PORT_GUARD="$(cat "$TMP/port" 2>/dev/null || true)"
if [ -n "$PORT_GUARD" ]; then
  lsof -ti ":$PORT_GUARD" 2>/dev/null | xargs -r kill -9 2>/dev/null || true
fi
rm -rf "$TMP"
[ -f "$OUT" ] && echo "rendered $OUT" || { echo "FAIL: no PNG produced"; exit 1; }