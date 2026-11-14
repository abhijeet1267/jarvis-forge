#!/usr/bin/env bash
# Integration check for BackendManager's connection lifecycle (connect ->
# down -> recovery -> auth-distinction -> no duplicate poll loops -> clean
# shutdown). XCTest cannot run in this environment (Command Line Tools only),
# so this compiles the REAL BackendManager/APIClient/APIModels source files
# alongside a small driver and runs it against a REAL local HTTP backend on an
# ephemeral port (via the internal JARVIS_BACKEND_PORT seam; production default
# stays 8765, untouched). No mock fetcher, no stub client.
#
# A second phase drives BackendLifecycle's parser against the REAL
# scripts/backend_lifecycle.py, run by the real Python, against fixture
# listeners on free ephemeral ports -- never the live :8765. Each fixture
# produces one verdict (ours / legacy / foreign / other-checkout / free), the
# script's --json is captured verbatim, and the Swift types decode those bytes.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$(cd "$DIR/.." && pwd)"
REPO="$(cd "$APP/../.." && pwd)"
OUT="$(mktemp -d)"
SERVER_LOG="$OUT/server.log"; HARN_LOG="$OUT/result.log"
PORTF="$OUT/port"; MODEF="$OUT/mode"; HITSF="$OUT/hits"
OVERALL=0
kill_fixtures() {
  # The fixture writes its own pid beside its port file; `$!` here is a
  # subshell, so killing it would leave the server running.
  for pidf in "$OUT"/port-*.pid; do
    [ -f "$pidf" ] || continue
    kill "$(cat "$pidf")" 2>/dev/null || true
  done
}
cleanup() {
  kill_fixtures
  rm -rf "$OUT"
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Phase 1: connection lifecycle against fake_backend.py
# ---------------------------------------------------------------------------
# The fake backend publishes its port to $OUT/port; the Swift driver reads it.
# Patch the driver's BASE to the temp dir so the two share it.
cp "$DIR/fake_backend.py" "$OUT/server.py"
sed -i.bak "s#^BASE = \"/tmp/bm_harness\"#BASE = \"$OUT\"#" "$OUT/server.py"
cp "$DIR/lifecycle_check.swift" "$OUT/main.swift"
sed -i.bak "s#^let BASE = \"/tmp/bm_harness\"#let BASE = \"$OUT\"#" "$OUT/main.swift"
PY="${PYTHON:-python3}"
"$PY" "$OUT/server.py" > "$SERVER_LOG" 2>&1 &
SRV=$!
# build
swiftc -O -o "$OUT/harness" \
  "$APP/Sources/Services/BackendManager.swift" \
  "$APP/Sources/Services/BackendLifecycle.swift" \
  "$APP/Sources/APIClient/APIClient.swift" \
  "$APP/Sources/APIClient/APIModels.swift" \
  "$OUT/main.swift"
# run with a guard
( "$OUT/harness" > "$HARN_LOG" 2>&1 ) &
H=$!
ok=1
for _ in $(seq 1 60); do kill -0 "$H" 2>/dev/null || break; sleep 0.5; done
if kill -0 "$H" 2>/dev/null; then ok=0; kill -9 "$H" 2>/dev/null || true; fi
kill "$SRV" 2>/dev/null || true
echo "=== fake backend ==="; cat "$SERVER_LOG" 2>/dev/null || true
echo "=== lifecycle check ==="; cat "$HARN_LOG" 2>/dev/null || true
passed=0
[ "$ok" = "1" ] && grep -q "ALL CHECKS PASSED" "$HARN_LOG" 2>/dev/null && passed=1
[ "$passed" = "1" ] || OVERALL=1

# ---------------------------------------------------------------------------
# Phase 2: BackendLifecycle parser vs real script output
# ---------------------------------------------------------------------------
# Pick a batch of free ephemeral ports from the OS rather than hard-coding any.
free_port() {
  "$PY" - <<'PYEOF'
import socket
s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()
PYEOF
}
wait_for_file() { for _ in $(seq 1 50); do [ -s "$1" ] && return 0; sleep 0.1; done; return 1; }
start_fixture() { # <port-file> <cwd> <extra args...>
  local port_file="$1"; local cwd="$2"; shift 2
  ( cd "$cwd" && exec "$PY" "$OUT/fixture_server.py" --port-file "$port_file" "$@" ) \
    > "$OUT/fixture.log" 2>&1 &
  wait_for_file "$port_file" || { echo "FAIL: fixture did not start ($*)" >&2; exit 1; }
}

# A legacy/foreign listener must look like *this project's entry point* to the
# script's OS-evidence check. The fixture's own command line carries its file
# name; point it at a copy named run.py and pass `serve` so the check is the
# same one a real pre-identity `run.py serve` would pass. It still serves the
# fixture's controlled 404 for /health/identity.
FIXDIR="$OUT/fixture-entry"
mkdir -p "$FIXDIR"
cp "$DIR/lifecycle_fixture_server.py" "$FIXDIR/run.py"
# Also run the plain fixture from the temp dir. Running the tracked source file
# in place makes macOS rewrite its mtime, and the backend reads that as "the
# code changed after I started" -- so a read-only harness produced a false
# stale warning for the live server.
cp "$DIR/lifecycle_fixture_server.py" "$OUT/fixture_server.py"

start_fixture_real() { # <port-file> <cwd> <extra...>
  local port_file="$1"; local cwd="$2"; shift 2
  ( cd "$cwd" && exec "$PY" "$FIXDIR/run.py" serve --port-file "$port_file" "$@" ) \
    > "$OUT/fixture.log" 2>&1 &
  wait_for_file "$port_file" || { echo "FAIL: fixture did not start ($*)" >&2; exit 1; }
}

python_bin() {
  if [ -x "$REPO/.venv/bin/python" ]; then printf '%s' "$REPO/.venv/bin/python"; else printf '%s' "$PY"; fi
}
PYBIN="$(python_bin)"

capture_status() { # <port> <out-file>
  set +e
  "$PYBIN" "$REPO/scripts/backend_lifecycle.py" status --port "$1" --json > "$2" 2> "$OUT/status.err"
  set -e
}

# ours: answers identity, source_root == this checkout
PF="$OUT/port-ours"; start_fixture "$PF" "$REPO" --identity-root "$REPO" &
wait_for_file() { for _ in $(seq 1 50); do [ -s "$1" ] && return 0; sleep 0.1; done; return 1; }
wait_for_file "$PF" || { echo "FAIL: ours fixture did not start"; exit 1; }
OURS_PORT="$(cat "$PF")"; capture_status "$OURS_PORT" "$OUT/ours.json"

# legacy: no identity, cwd inside checkout, command line is run.py serve
PF="$OUT/port-legacy"; start_fixture_real "$PF" "$REPO" &
wait_for_file "$PF" || { echo "FAIL: legacy fixture did not start"; exit 1; }
LEGACY_PORT="$(cat "$PF")"; capture_status "$LEGACY_PORT" "$OUT/legacy.json"

# foreign: no identity, cwd OUTSIDE the checkout
PF="$OUT/port-foreign"; start_fixture_real "$PF" "$OUT" &
wait_for_file "$PF" || { echo "FAIL: foreign fixture did not start"; exit 1; }
FOREIGN_PORT="$(cat "$PF")"; capture_status "$FOREIGN_PORT" "$OUT/foreign.json"

# other checkout: answers identity, source_root is somewhere else
PF="$OUT/port-other"; start_fixture "$PF" "$REPO" --identity-root "$OUT/some-other-checkout/jarvis-forge" &
wait_for_file "$PF" || { echo "FAIL: other-checkout fixture did not start"; exit 1; }
OTHER_PORT="$(cat "$PF")"; capture_status "$OTHER_PORT" "$OUT/other.json"

# free: a port nothing listens on
FREE_PORT="$(free_port)"
capture_status "$FREE_PORT" "$OUT/free.json"

echo "=== parser fixtures: ours=$OURS_PORT legacy=$LEGACY_PORT foreign=$FOREIGN_PORT other=$OTHER_PORT free=$FREE_PORT ==="
for f in ours legacy foreign other free; do
  echo "--- $f ---"; cat "$OUT/$f.json"; echo
done

# Swift only permits top-level statements in a file named main.swift.
cp "$DIR/lifecycle_parser_check.swift" "$OUT/parser_main.swift"
mkdir -p "$OUT/parser_src" && cp "$OUT/parser_main.swift" "$OUT/parser_src/main.swift"
swiftc -O -o "$OUT/parser_check" \
  "$APP/Sources/Services/BackendLifecycle.swift" \
  "$APP/Sources/Services/BackendManager.swift" \
  "$APP/Sources/APIClient/APIClient.swift" \
  "$APP/Sources/APIClient/APIModels.swift" \
  "$OUT/parser_src/main.swift"
FIXTURE_PORT="$OURS_PORT" "$OUT/parser_check" \
  "$OUT/ours.json" "$OUT/legacy.json" "$OUT/foreign.json" "$OUT/other.json" "$OUT/free.json" \
  > "$OUT/parser.log" 2>&1 || true
cat "$OUT/parser.log"
grep -q "PARSER CHECKS PASSED" "$OUT/parser.log" || OVERALL=1

kill_fixtures

[ "$OVERALL" = "0" ] && exit 0 || exit 1