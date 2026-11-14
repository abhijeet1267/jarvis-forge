"""An isolated Vantage server for the tests that drive a real browser.

**The browser tests were writing into the developer's real calendar.**

``tests/test_dashboard_browser_flows.py`` drives the real dashboard page in
a real browser against ``http://127.0.0.1:8765`` -- the developer's *live*
backend, running against the real ``~/.jarvis-forge`` data directory. One of
those tests raises "schedule a meeting called Browser Check on 2026-12-05
09:00", waits for the approval card, and clicks **Approve**. The approval
runs a real ``create_event``, and the event lands in the real calendar.

Measured on this machine, after a few suite runs:

```
 10  called Browser Check
  5  Searchable sync
  5  Busy
  5  Tool test
  5  Renamed
  1  Design Review
  1  Standup
  1  Standup2
```

Ten "Browser Check" events, all on 2026-12-05 12:00, all with the same
title. They are not a fixture. They are the test suite's detritus in the
user's data.

The fix is the one the standing rule already asks for: *use isolated
environments and test fixtures to prevent repetition*. This module starts a
**real server** -- a real uvicorn, a real HTTP port, the real page -- on a
**scratch home**, so the browser drives a genuine backend that happens to
have an empty data directory. The page's JavaScript, the session bootstrap
and the approval round trip are all exercised for real; only the data
directory differs.

The server is started once per session and shared by the tests that need
it, because starting uvicorn is slow and the alternative -- the developer's
live server -- is what caused the problem.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

REPO = Path(__file__).resolve().parent.parent

#: How long the isolated server may take to answer its first request.
STARTUP_TIMEOUT = float(os.environ.get("VANTAGE_TEST_SERVER_TIMEOUT", "60"))


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _server_env(home: Path, port: int) -> Dict[str, str]:
    env = dict(os.environ)
    env["JARVIS_HOME"] = str(home)
    env["JARVIS_HOST"] = "127.0.0.1"
    env["JARVIS_PORT"] = str(port)
    env["JARVIS_LOG_LEVEL"] = "WARNING"
    # A fixed token so the test does not depend on a keychain read.
    env["JARVIS_API_TOKEN"] = "isolated-browser-test-token"
    # The provider selection in the developer's home must not leak in.
    env.pop("JARVIS_AI_PROVIDER", None)
    # Google must not leak in either.
    #
    # `Settings` reads `.env` from the working directory, and this server is
    # started from the repository root — so a developer who has connected their
    # Google account in the real `.env` had *every* isolated server routed to
    # their real, read-only Google calendar. Measured 2026-10-29: an approval
    # that should have written a local scratch event answered "that calendar is
    # connected read-only", and the scratch database stayed empty.
    #
    # Turning the integration off for the scratch server is the isolation the
    # module's own docstring promises: the test exercises the local provider,
    # and the developer's real calendar is neither read nor written.
    env["JARVIS_GOOGLE_CLIENT_ID"] = ""
    env["JARVIS_GOOGLE_CLIENT_SECRET"] = ""
    env["JARVIS_GOOGLE_CALENDAR_ENABLED"] = "false"
    env["JARVIS_GOOGLE_TASKS_ENABLED"] = "false"
    return env


def _wait_for(base: str, process: subprocess.Popen, timeout: float) -> None:
    """Wait until the server answers, or fail with what it printed."""
    import pytest

    deadline = time.time() + timeout
    last_error = ""
    while time.time() < deadline:
        if process.poll() is not None:
            out = process.stdout.read() if process.stdout else ""
            pytest.fail(
                f"the isolated server exited with {process.returncode} before it "
                f"answered:\n{out[-2000:]}")
        try:
            # `/` is the one unauthenticated route, so the readiness probe
            # does not need a token. `/health` is protected.
            with urllib.request.urlopen(f"{base}/", timeout=2) as response:
                if response.status == 200:
                    return
        except Exception as exc:  # not up yet
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(0.25)
    pytest.fail(f"the isolated server did not come up within {timeout:.0f} s "
                f"(last error: {last_error})")


def start_server(home: Path) -> Dict[str, Any]:
    """Start a real Vantage server on a scratch home and wait for it."""
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    process = subprocess.Popen(
        [sys.executable, "run.py", "serve", "--port", str(port),
         "--host", "127.0.0.1", "--home", str(home)],
        cwd=str(REPO),
        env=_server_env(home, port),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    _wait_for(base, process, STARTUP_TIMEOUT)
    return {"base": base, "home": home, "port": port, "process": process}


def stop_server(server: Dict[str, Any]) -> None:
    """Stop the server started by :func:`start_server`."""
    process = server.get("process")
    if process is None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
    if process.stdout:
        process.stdout.close()


def session_token(base: str) -> str:
    """A session token for the isolated server, the way the dashboard gets one.

    The dashboard's own page reads a bootstrap out of the served HTML and
    spends it at ``POST /auth/session``. This does the same, because a test
    that bypasses it would not be testing what the page does.
    """
    html = urllib.request.urlopen(f"{base}/dashboard").read().decode()
    match = re.search(r'jarvis-bootstrap" content="([^"]*)"', html)
    assert match, "the dashboard was served without a bootstrap code"
    request = urllib.request.Request(
        f"{base}/auth/session", method="POST",
        data=json.dumps({"bootstrap": match.group(1)}).encode(),
        headers={"Content-Type": "application/json",
                 "Origin": base, "Host": f"127.0.0.1:{base.rsplit(':', 1)[1]}"})
    return json.loads(urllib.request.urlopen(request).read())["session_token"]


def post_command(base: str, token: str, text: str,
                 session_id: str = "isolated-browser-test") -> Dict[str, Any]:
    """Raise a command against the isolated server and return the response."""
    request = urllib.request.Request(
        f"{base}/command", method="POST",
        data=json.dumps({"text": text}).encode(),
        headers={"Content-Type": "application/json",
                 "X-JARVIS-Token": token,
                 "Host": f"127.0.0.1:{base.rsplit(':', 1)[1]}"})
    return json.loads(urllib.request.urlopen(request).read())


def get_json(base: str, path: str, token: Optional[str] = None) -> Any:
    headers = {"Host": f"127.0.0.1:{base.rsplit(':', 1)[1]}"}
    if token:
        headers["X-JARVIS-Token"] = token
    request = urllib.request.Request(f"{base}{path}", headers=headers)
    try:
        return json.loads(urllib.request.urlopen(request).read())
    except urllib.error.HTTPError as exc:
        return {"status": exc.code, "body": exc.read().decode()[:400]}
