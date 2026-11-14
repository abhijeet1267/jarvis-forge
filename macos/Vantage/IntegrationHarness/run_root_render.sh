#!/usr/bin/env bash
# Render the whole app root — sidebar plus the selected detail pane — in the
# designed light appearance. Every other renderer hosts one view; this one is
# what proves the navigation chrome and the page surface read as one product.
#
#   ./run_root_render.sh <out.png> [navItem]
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$(cd "$DIR/.." && pwd)"
OUT="${1:-artifacts/root.png}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

cp "$DIR/render_root.swift" "$TMP/main.swift"
"$DIR/build_render.sh" "$TMP/main.swift" "$TMP/render"

[ -x "$TMP/render" ] || { echo "FAIL: renderer did not build"; exit 1; }
mkdir -p "$(dirname "$OUT")"
"$TMP/render" "$OUT" "${2:-home}"
[ -f "$OUT" ] && echo "rendered $OUT" || { echo "FAIL: no PNG"; exit 1; }