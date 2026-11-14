#!/usr/bin/env bash
# Command Center end-to-end check: drives the REAL ConversationService + APIClient
# (what CommandCenterView uses) against a local stub backend on an ephemeral port
# (via the internal JARVIS_BACKEND_PORT seam; production default stays 8765).
# Proves a model answer is shown, the session is adopted, and provider/auth/
# malformed failures each become an honest visible error — never a fake success.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$(cd "$DIR/.." && pwd)"
OUT="$(mktemp -d)"
cp "$DIR/stub_backend.py" "$OUT/server.py"
sed -i.bak "s#^BASE = \"/tmp/cc_harness\"#BASE = \"$OUT\"#" "$OUT/server.py"
cp "$DIR/command_center_check.swift" "$OUT/main.swift"
sed -i.bak "s#^let BASE = \"/tmp/cc_harness\"#let BASE = \"$OUT\"#" "$OUT/main.swift"
PY="${PYTHON:-python3}"
"$PY" "$OUT/server.py" > "$OUT/server.log" 2>&1 &
SRV=$!
# Compile the whole of Sources/ rather than a hand-written list.
#
# This list used to name six files. When Mac control added
# `Sources/Services/MacControl/`, ConversationService gained a reference the
# list could not see and the harness stopped compiling -- quietly, because a
# failed compile here is not an app bug. build_render.sh already fixed this
# class of failure for the renderers; do the same here.
# The @main entry point is excluded because the driver supplies its own.
SOURCES=$(find "$APP/Sources" -name '*.swift' ! -name 'VantageApp.swift' | sort)
swiftc -O -o "$OUT/harness" $SOURCES "$OUT/main.swift"
( "$OUT/harness" > "$OUT/result.log" 2>&1 ) &
H=$!
ok=1
for _ in $(seq 1 60); do kill -0 "$H" 2>/dev/null || break; sleep 0.5; done
if kill -0 "$H" 2>/dev/null; then ok=0; kill -9 "$H" 2>/dev/null || true; fi
kill "$SRV" 2>/dev/null || true
echo "=== stub backend ==="; cat "$OUT/server.log" 2>/dev/null || true
echo "=== command center check ==="; cat "$OUT/result.log" 2>/dev/null || true
passed=0
[ "$ok" = "1" ] && grep -q "ALL CHECKS PASSED" "$OUT/result.log" 2>/dev/null && passed=1
rm -rf "$OUT"
[ "$passed" = "1" ] && exit 0 || exit 1
