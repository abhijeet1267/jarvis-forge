#!/usr/bin/env bash
# Render the AI-voice controls offscreen (Settings header, Voice tab, Command
# Center composer) with no backend reachable, and write a PNG of what the real
# views draw. Use VOICE_ON=0 for the OFF state; VOICE_SPEAK=1 to capture the
# "Speaking…" row while the synthesizer is actually playing.
#
#   ./run_voice_controls_render.sh <view> [out.png]
#   view: settings | voice-tab | command-center
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$(cd "$DIR/.." && pwd)"
REPO="$(cd "$APP/../.." && pwd)"
VIEW="${1:?usage: run_voice_controls_render.sh <view> [out.png]}"
OUT="${2:-$REPO/artifacts/voice-$VIEW.png}"
TMP="$(mktemp -d)"
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT

cp "$DIR/render_voice_controls.swift" "$TMP/main.swift"
"$DIR/build_render.sh" "$TMP/main.swift" "$TMP/render"

# A port that is definitely closed.
CLOSED_PORT="$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')"
mkdir -p "$TMP/home"
printf "unused" > "$TMP/home/api_token"
mkdir -p "$(dirname "$OUT")"
# VOICE_ON=0 or VOICE_OFF=1 renders the OFF state without writing the preference:
# an NSArgumentDomain override outranks UserDefaults.standard for the process.
VOICE_ARGS=()
if [ "${VOICE_ON:-1}" = "0" ] || [ "${VOICE_OFF:-0}" = "1" ]; then
  VOICE_ARGS=(-VantageSpeakReplies NO)
fi
JARVIS_BACKEND_PORT="$CLOSED_PORT" JARVIS_HOME="$TMP/home" \
  VOICE_SPEAK="${VOICE_SPEAK:-0}" \
  VOICE_WIDTH="${VOICE_WIDTH:-}" VOICE_HEIGHT="${VOICE_HEIGHT:-}" \
  "$TMP/render" "$OUT" "$VIEW" ${VOICE_ARGS[@]+"${VOICE_ARGS[@]}"} </dev/null
[ -f "$OUT" ] && echo "rendered $OUT" || { echo "FAIL: no PNG produced"; exit 1; }