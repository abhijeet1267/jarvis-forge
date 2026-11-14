#!/usr/bin/env bash
# Prove the Swift client path for `PUT /mcp/config` against a REAL backend.
#
# Compiles IntegrationHarness/mcp_config_check.swift (the app's own APIClient
# and APIModels, not a copy) and runs it. The tool:
#   * saves an endpoint + allowlist and checks the parsed allowlist the server
#     echoes (it de-duplicates "send_email, make_task, send_email");
#   * re-reads /mcp/status and checks it reports the saved config;
#   * sends ftp://x and checks for 422 with the server's own detail sentence;
#   * restores url/allowed_tools/name to empty.
#
# The live backend must be running (default 127.0.0.1:8765) and the invoking
# user's ~/.jarvis-forge/api_token must be valid for it.
#
# Usage: run_mcp_config_check.sh [port] [--keep]
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
PORT="${1:-8765}"
KEEP="${2:-}"
CASE_ARGS=("$PORT")
[ "$KEEP" = "--keep" ] && CASE_ARGS+=("--keep")

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

cp "$DIR/mcp_config_check.swift" "$TMP/main.swift"
"$DIR/build_render.sh" "$TMP/main.swift" "$TMP/mcp_config_check"
JARVIS_BACKEND_PORT="$PORT" JARVIS_HOME="${JARVIS_HOME:-$HOME/.jarvis-forge}" \
  "$TMP/mcp_config_check" "${CASE_ARGS[@]}"