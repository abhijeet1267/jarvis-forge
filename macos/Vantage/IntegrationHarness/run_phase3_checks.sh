#!/usr/bin/env bash
# Phase 3 verification against the REAL app sources.
#
#   1. logic checks   — capture, dates, briefing, preferences, documents, palette
#   2. capture store on a real filesystem across a simulated restart
#   3. renders        — Quick Capture, Documents, Daily Briefing, Preferences
#
# Same shape as run_anywhere_checks.sh: the whole of Sources/ is compiled, so a
# new file cannot make a harness silently stop compiling. XCTest is unavailable
# under Command Line Tools, which is why these exist.
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$(cd "$DIR/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
SOURCES=$(find "$APP/Sources" -name '*.swift' ! -name 'VantageApp.swift' | sort)
FAIL=0

compile() { # <main.swift> <out>
  swiftc -O -o "$2" $(printf '%s\n' "$SOURCES") "$1" 2>&1 | grep "error:" && return 1
  return 0
}

echo "=== 1. Phase 3 logic ==="
mkdir -p "$TMP/logic" && cp "$DIR/phase3_logic_check.swift" "$TMP/logic/main.swift"
if compile "$TMP/logic/main.swift" "$TMP/logic/run"; then
  "$TMP/logic/run" | tail -20 || FAIL=1
else FAIL=1; fi

echo "=== 2. Briefing loads from a real backend ==="
mkdir -p "$TMP/live" && cp "$DIR/phase3_live_check.swift" "$TMP/live/main.swift"
if compile "$TMP/live/main.swift" "$TMP/live/run"; then
  if "$DIR/run_phase3_live.sh" "$TMP/live/run"; then :; else FAIL=1; fi
else FAIL=1; fi

echo "=== 3. Renders ==="
for view in capture documents briefing preferences; do
  echo "--- $view ---"
  if "$DIR/run_phase3_render.sh" "$view"; then :; else FAIL=1; fi
done

[ "$FAIL" = "0" ] && echo "ALL PHASE 3 CHECKS PASSED" || echo "SOME PHASE 3 CHECKS FAILED"
exit "$FAIL"