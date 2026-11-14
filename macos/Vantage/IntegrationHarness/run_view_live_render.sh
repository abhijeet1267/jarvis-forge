#!/usr/bin/env bash
# Render a list view offscreen against a REAL backend with a scratch home, so
# the "read succeeded and really is empty" path can be looked at. The companion
# of run_view_offscreen_render.sh, which exercises the failed-read path.
#
#   ./run_view_live_render.sh <view> [out.png]
#   view: devices | memory | projects | tasks | audit | providers
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$(cd "$DIR/.." && pwd)"
REPO="$(cd "$APP/../.." && pwd)"
VIEW="${1:?usage: run_view_live_render.sh <view> [out.png]}"
OUT="${2:-$REPO/artifacts/live-$VIEW.png}"
TMP="$(mktemp -d)"
cleanup() {
  touch "$TMP/stop" 2>/dev/null || true
  if [ -f "$TMP/seeder" ]; then
    kill "$(cat "$TMP/seeder")" 2>/dev/null || true
    wait "$(cat "$TMP/seeder")" 2>/dev/null || true
  fi
  if [ -f "$TMP/port" ]; then
    lsof -ti ":$(cat "$TMP/port")" 2>/dev/null | xargs -r kill -9 2>/dev/null || true
  fi
  rm -rf "$TMP"
}
trap cleanup EXIT
PY="${PYTHON:-$REPO/.venv/bin/python}"

"$PY" - "$TMP" "$REPO" <<'PYEOF' &
import os, sys, time
from pathlib import Path
os.chdir(sys.argv[2])
sys.path.insert(0, str(Path(sys.argv[2]) / "tests"))
from isolated_server import start_server, stop_server, session_token
home = Path(sys.argv[1])
srv = start_server(home)
Path(home, "port").write_text(str(srv["port"]))
Path(home, "token").write_text(session_token(srv["base"]))
Path(home, "seeder").write_text(str(os.getpid()))
Path(home, "srv_pid").write_text(str(srv["process"].pid))
Path(home, "ready").write_text("1")
# Stay alive until the runner says it is done. Exiting here would run the
# atexit handler and stop the server before the renderer ever connected --
# which made the first "live" render silently draw the dead-server path
# while still looking like a successful run.
stop = home / "stop"
deadline = time.time() + 300
while not stop.exists() and time.time() < deadline:
    time.sleep(0.2)
stop_server(srv)
PYEOF
SEEDER=$!
for _ in $(seq 1 80); do [ -f "$TMP/ready" ] && break; sleep 0.25; done
if [ ! -f "$TMP/ready" ]; then echo "FAIL: backend did not start"; exit 1; fi
PORT="$(cat "$TMP/port")"; TOKEN="$(cat "$TMP/token")"

cp "$DIR/render_view_offscreen.swift" "$TMP/main.swift"
"$DIR/build_render.sh" "$TMP/main.swift" "$TMP/render"

mkdir -p "$TMP/home" "$(dirname "$OUT")"
printf "%s" "$TOKEN" > "$TMP/home/api_token"
JARVIS_BACKEND_PORT="$PORT" JARVIS_HOME="$TMP/home" \
  "$TMP/render" "$OUT" "$VIEW" </dev/null
[ -f "$OUT" ] && echo "rendered $OUT" || { echo "FAIL: no PNG produced"; exit 1; }