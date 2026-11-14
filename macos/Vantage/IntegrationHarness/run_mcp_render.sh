#!/usr/bin/env bash
# Render the native macOS MCPView offscreen against a real backend and write a
# PNG. Same pattern as run_connections_render.sh: a scratch-home isolated
# server, a renderer compiled from the app's own sources, and a trap that kills
# the server on every exit path.
#
# Usage: run_mcp_render.sh [out.png] [--seed] [-d]
#   --seed  PUT a (reachable-but-empty) MCP endpoint + allowlist first, so the
#           rendered screen shows the *configured* branch — the editor filled
#           in, and the pill reporting the honest "configured but not
#           connected" state for an endpoint that is not listening.
#   -d      dark appearance (default is light).
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$(cd "$DIR/.." && pwd)"
REPO="$(cd "$APP/../.." && pwd)"
OUT="$REPO/artifacts/mcp-native.png"
SEED=""
MODE=""
for arg in "$@"; do
  case "$arg" in
    --seed) SEED=1 ;;
    -d|--dark) MODE="-d" ;;
    *) OUT="$arg" ;;
  esac
done
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

# Resolve a relative output path against the caller's directory now: the
# renderer runs after this script has cd-ed nowhere, but a bare relative path
# checked here and written there must name the same file.
case "$OUT" in
  /*) ;;
  *) OUT="$(pwd)/$OUT" ;;
esac
mkdir -p "$(dirname "$OUT")"

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
# A seeded render must not outlive this script: keep the server up until the
# runner writes the stop file, and stop it explicitly rather than via atexit.
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

if [ -n "$SEED" ]; then
  # Port 9 (discard) is not listening: the saved config is real, and the probe
  # honestly reports "configured but not connected" — never a fake handshake.
  HTTP=$(curl -s -o "$TMP/seed.json" -w '%{http_code}' -m 15 \
    -X PUT "http://127.0.0.1:$PORT/mcp/config" \
    -H "X-JARVIS-Token: $TOKEN" -H "Origin: http://127.0.0.1:$PORT" \
    -H "Content-Type: application/json" \
    -d '{"url":"http://127.0.0.1:9/mcp","allowed_tools":"send_email, create_task","name":"rendered"}' || true)
  echo "seed: HTTP $HTTP $(cat "$TMP/seed.json")"
  [ "$HTTP" = "200" ] || { echo "FAIL: could not seed the MCP config"; exit 1; }
fi

cp "$DIR/render_mcp.swift" "$TMP/main.swift"
"$DIR/build_render.sh" "$TMP/main.swift" "$TMP/render"

mkdir -p "$TMP/home"
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