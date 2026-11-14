#!/usr/bin/env bash
# Render transcript rows (bubbles, captions, error turns) offscreen with a
# hand-built conversation. No server needed: this exercises the presentation of
# a populated transcript, which the seeded renders do not reach because nothing
# has been asked yet.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$(cd "$DIR/.." && pwd)"
REPO="$(cd "$APP/../.." && pwd)"
OUT="${1:-$REPO/artifacts/transcript.png}"
W="${2:-820}"; H="${3:-1000}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

cp "$DIR/render_transcript.swift" "$TMP/main.swift"
"$DIR/build_render.sh" "$TMP/main.swift" "$TMP/render"

mkdir -p "$(dirname "$OUT")"
"$TMP/render" "$OUT" "$W" "$H" </dev/null
[ -f "$OUT" ] && echo "rendered $OUT" || { echo "FAIL: no PNG produced"; exit 1; }