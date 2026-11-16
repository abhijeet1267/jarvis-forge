#!/bin/bash
# Build the native desktop executor.
#
# macOS authorizes input posting per binary identity, so this must be a
# compiled, stable artifact rather than a `swift run` harness: rebuilding to a
# different path is a different identity and can lose an existing grant.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${1:-$HERE/vantage-desktop}"

echo "building $OUT"
swiftc -O -o "$OUT" "$HERE/main.swift"
echo "built: $OUT"
echo
echo "Next, grant it permission (this cannot be done for you):"
echo "  $OUT request_access     # shows macOS's own prompt"
echo "or add it manually: System Settings → Privacy & Security → Accessibility"
echo
echo "Check the real state with:"
echo "  $OUT permissions"
