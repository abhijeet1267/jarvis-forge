#!/usr/bin/env bash
# Render a native list view offscreen with NO backend reachable, so the only
# thing on screen is the failed-read path. Checks that a failed read never draws
# an "empty list" claim.
#
#   ./run_view_offscreen_render.sh <view> [out.png]
#   view: devices | memory | projects | tasks | audit | providers
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$(cd "$DIR/.." && pwd)"
REPO="$(cd "$APP/../.." && pwd)"
VIEW="${1:?usage: run_view_offscreen_render.sh <view> [out.png]}"
OUT="${2:-$REPO/artifacts/offscreen-$VIEW.png}"
TMP="$(mktemp -d)"
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT

cp "$DIR/render_view_offscreen.swift" "$TMP/main.swift"
"$DIR/build_render.sh" "$TMP/main.swift" "$TMP/render"

# A port that is definitely closed: bind one, learn its number, release it.
CLOSED_PORT="$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')"
mkdir -p "$TMP/home"
printf "unused" > "$TMP/home/api_token"
mkdir -p "$(dirname "$OUT")"
JARVIS_BACKEND_PORT="$CLOSED_PORT" JARVIS_HOME="$TMP/home" \
  "$TMP/render" "$OUT" "$VIEW" </dev/null
[ -f "$OUT" ] && echo "rendered $OUT" || { echo "FAIL: no PNG produced"; exit 1; }