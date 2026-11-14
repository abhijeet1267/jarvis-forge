#!/usr/bin/env bash
# Render the Control Mac screen in the designed light appearance.
#
# Hosts the REAL MacControlView (not a mock) so the capability states, the
# disabled-until-permitted window controls and the history row are what a
# person would actually see. A render cannot click, so the wiring itself is
# proven by MacControlPolicyTests + the native harness, not by this PNG.
#
#   ./run_mac_control_render.sh <out.png>
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
OUT="${1:-artifacts/mac_control.png}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
cp "$DIR/render_mac_control.swift" "$TMP/main.swift"
"$DIR/build_render.sh" "$TMP/main.swift" "$TMP/render"
[ -x "$TMP/render" ] || { echo "FAIL: renderer did not build"; exit 1; }
mkdir -p "$(dirname "$OUT")"
"$TMP/render" "$OUT"
[ -f "$OUT" ] && echo "rendered $OUT" || { echo "FAIL: no PNG"; exit 1; }
