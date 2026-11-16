#!/bin/bash
# Build the disposable input probe.
#
# Separate binary from the executor on purpose: the probe is the *target* of
# input, the executor is the *source*. They must not share an identity, or a
# grant intended for one would silently authorize the other.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${1:-$HERE/vantage-probe}"

echo "building $OUT"
swiftc -O -o "$OUT" "$HERE/main.swift"
echo "built: $OUT"
echo
echo "Run it, leave it on screen, then drive it with:"
echo "  tools/input_probe/selftest.py"