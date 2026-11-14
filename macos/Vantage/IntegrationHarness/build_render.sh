#!/usr/bin/env bash
# Compile a renderer against the REAL app sources.
#
#   build_render.sh <main.swift> <output-binary>
#
# Why this exists: every script here used to enumerate the app's source files by
# hand. When a new directory appeared (`Sources/DesignSystem/`), the list went
# stale and each harness stopped compiling -- with an error that looks like a
# bug in the app ("cannot find 'V' in scope") rather than in the list. A harness
# that fails to compile silently stops verifying anything, so the failure is
# quiet in exactly the way that matters.
#
# Compiling the whole of `Sources/` removes that class of failure. The only file
# excluded is `VantageApp.swift`: it is the `@main` entry point, and a renderer
# supplies its own top-level code.
set -euo pipefail

MAIN="${1:?usage: build_render.sh <main.swift> <output-binary>}"
OUT="${2:?usage: build_render.sh <main.swift> <output-binary>}"
APP="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# macOS ships bash 3.2, which has no `mapfile`.
SOURCES=$(find "$APP/Sources" -name '*.swift' ! -name 'VantageApp.swift' | sort)
if [ -z "$SOURCES" ]; then
  echo "FAIL: no app sources found under $APP/Sources" >&2
  exit 1
fi

# Word-splitting is intended: these are individual paths, one per word after
# the newline split, and none contains a space in this tree.
# shellcheck disable=SC2086
swiftc -O -o "$OUT" $SOURCES "$MAIN"
