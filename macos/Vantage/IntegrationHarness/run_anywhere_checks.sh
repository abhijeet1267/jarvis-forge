#!/usr/bin/env bash
# Vantage Anywhere + Flows verification against the REAL sources.
#
# Four phases, each compiled from Sources/ (the whole tree, so a new file cannot
# make a harness silently stop compiling):
#   1. flow engine logic        — ordering, failure, approval, cancel, persistence
#   2. authoring + palette      — the three intents, drafts never execute
#   3. native flow execution    — real NSWorkspace/CoreAudio actions
#   4. global shortcut          — register, fire once, re-register, shutdown
#
# XCTest cannot run in this environment (Command Line Tools only), so these
# stand in for it. Phase 3 is the only one that proves real Mac control.
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

echo "=== 1. flow engine ==="
mkdir -p "$TMP/engine" && cp "$DIR/flow_engine_check.swift" "$TMP/engine/main.swift"
if compile "$TMP/engine/main.swift" "$TMP/engine/run"; then
  "$TMP/engine/run" | tail -30 || FAIL=1
else FAIL=1; fi

echo "=== 2. authoring + palette ==="
mkdir -p "$TMP/auth" && cp "$DIR/flow_authoring_check.swift" "$TMP/auth/main.swift"
if compile "$TMP/auth/main.swift" "$TMP/auth/run"; then
  "$TMP/auth/run" | tail -30 || FAIL=1
else FAIL=1; fi

echo "=== 3. native flow execution ==="
mkdir -p "$TMP/native" && cp "$DIR/flow_native_check.swift" "$TMP/native/main.swift"
if compile "$TMP/native/main.swift" "$TMP/native/run"; then
  "$TMP/native/run" | tail -20 || FAIL=1
else FAIL=1; fi

echo "=== 4. global shortcut ==="
mkdir -p "$TMP/probe"
cat > "$TMP/probe/main.swift" <<'SWIFTGEN'
SWIFTGEN
cp "$DIR/hotkey_check.swift" "$TMP/probe/main.swift"
if compile "$TMP/probe/main.swift" "$TMP/probe/run"; then
  "$TMP/probe/run" | tail -10 || FAIL=1
else FAIL=1; fi

echo "=== 5. one execution path ==="
mkdir -p "$TMP/shared" && cp "$DIR/shared_path_check.swift" "$TMP/shared/main.swift"
if compile "$TMP/shared/main.swift" "$TMP/shared/run"; then
  "$TMP/shared/run" | tail -10 || FAIL=1
else FAIL=1; fi

[ "$FAIL" = "0" ] && echo "ALL ANYWHERE/FLOWS CHECKS PASSED" || echo "SOME CHECKS FAILED"
exit "$FAIL"
