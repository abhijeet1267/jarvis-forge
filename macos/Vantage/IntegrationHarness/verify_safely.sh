#!/bin/bash
# Run a command against a throwaway Vantage server on a scratch home.
#
# Written because verifying side-effecting features against the live backend on
# :8765 moved a real planner task and created a stray one. Anything that *acts* —
# schedules a block, moves work, places a call — must run here instead.
#
# Usage:  verify_safely.sh '<python expression using base + token>'
#
# The expression is evaluated with `base` (the scratch server URL) and `token`
# bound. It can use the `request(method, path, body)` helper, which returns the
# decoded JSON and raises on a non-2xx.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
EXPR="${1:?usage: verify_safely.sh '<expression>'}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

cd "$REPO"
.venv/bin/python - "$TMP" "$EXPR" <<'PYEOF'
import json, os, sys, urllib.error, urllib.request
from pathlib import Path

tmp = Path(sys.argv[1])
expression = sys.argv[2]
sys.path.insert(0, str(Path.cwd() / "tests"))
from isolated_server import session_token, start_server, stop_server

server = start_server(tmp / "home")
base = server["base"]
token = session_token(base)

def request(method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    headers = {"X-JARVIS-Token": token, "Content-Type": "application/json"}
    req = urllib.request.Request(base + path, method=method, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=180) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return {"status": exc.code, "body": exc.read().decode()[:400]}

try:
    exec(compile(expression, "<verify>", "exec"), {"request": request, "base": base,
                                                   "token": token, "json": json})
finally:
    stop_server(server)
PYEOF