"""The test suite must not write into the developer's own data.

**The browser tests were creating real calendar events in the user's data.**

``tests/test_dashboard_browser_flows.py`` drove the real dashboard page in a
real browser against ``http://127.0.0.1:8765`` -- the developer's *live*
backend, running against the real ``~/.jarvis-forge`` data directory. One of
those tests raises *"schedule a meeting called Browser Check on 2026-12-05
09:00"*, waits for the approval card, and clicks **Approve**. The approval
runs a real ``create_event``, and the event lands in the real calendar.

Measured on this machine after a few suite runs:

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

Ten "Browser Check" events, all on 2026-12-05, all with the same title. They
are not a fixture. They are the test suite's detritus in the user's data --
and they grew every time the suite ran.

The rule this file pins is the one the standing requirements already state:
*protect the user's data; use isolated environments and test fixtures to
prevent repetition*.

The rules asserted here:

* the browser tests point at a **real server on a scratch home**, never at a
  developer's live backend;
* the scratch server is a genuine uvicorn on a genuine port, so the page, the
  session bootstrap and the approval round trip are exercised for real;
* the approval the test makes lands in the scratch calendar, and the
  developer's own calendar is untouched by it;
* no test module hard-codes a URL that points at a running server.
"""

from __future__ import annotations

import ast
import re
import urllib.request
from pathlib import Path
from typing import List

import pytest

REPO = Path(__file__).resolve().parent.parent
TESTS = REPO / "tests"


# ---------------------------------------------------------------------------
# 1. the browser tests use the isolated server
# ---------------------------------------------------------------------------

def test_the_browser_flow_tests_take_the_isolated_server():
    """Every browser test that talks to a server takes the fixture.

    A test that reaches a server without the fixture would be reaching
    whatever happens to be listening on that port -- which on a developer's
    machine is the one with the real data directory.
    """
    source = (TESTS / "test_dashboard_browser_flows.py").read_text()
    tree = ast.parse(source)
    browser_tests = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if not node.name.startswith("test_"):
            continue
        # A browser test is one that imports playwright.
        body = ast.get_source_segment(source, node) or ""
        if "sync_playwright" in body:
            browser_tests.append(node)

    assert browser_tests, "no browser test was found -- the file changed shape?"
    for node in browser_tests:
        args = [a.arg for a in node.args.args]
        assert "isolated_server" in args, (
            f"{node.name} drives a browser but does not take the isolated "
            f"server; it would reach whatever is listening on the URL")


def test_the_browser_flow_tests_no_longer_hard_code_the_live_port():
    source = (TESTS / "test_dashboard_browser_flows.py").read_text()
    # The `TestClient` base_url is a label, not a connection -- the client is
    # in-process and reads the isolated home. What must not survive is a
    # *browser* being pointed at a running server.
    assert "page.goto(f\"http://127.0.0.1:8765" not in source, (
        "a browser is still pointed at the developer's live port")
    assert "urlopen(f\"http://127.0.0.1:8765" not in source, (
        "a request is still made against the developer's live port")


def test_the_isolated_server_starts_a_real_server_on_a_scratch_home():
    from tests.isolated_server import _server_env

    env = _server_env(Path("/tmp/scratch-home"), 8123)
    assert env["JARVIS_HOME"] == "/tmp/scratch-home", env
    assert env["JARVIS_PORT"] == "8123", env
    assert env["JARVIS_HOST"] == "127.0.0.1", env


# ---------------------------------------------------------------------------
# 2. no test module points a browser at a running server
# ---------------------------------------------------------------------------

def _modules() -> List[Path]:
    return sorted(TESTS.glob("test_*.py"))


#: The ways a test can reach a running server. A `TestClient(base_url=...)`
#: is not one of them -- the client is in-process and reads the isolated
#: home, so its base_url is a label.
_LIVE_CALLS = (
    "page.goto(f\"http://127.0.0.1:8765",
    "page.goto(f\"http://localhost:8765",
    "urlopen(f\"http://127.0.0.1:8765",
    "urlopen(f\"http://localhost:8765",
    "urlopen(\"http://127.0.0.1:8765",
    "urlopen(\"http://localhost:8765",
)


def test_no_test_module_drives_a_browser_at_a_hard_coded_live_origin():
    """The check is on *browser* tests, because only those need a live server.

    A `TestClient` is in-process and reads the isolated home, so a
    ``base_url="http://127.0.0.1:8765"`` there is a label, not a connection.
    A Playwright page, though, really does open that port.
    """
    offenders = []
    for path in _modules():
        if path.name == Path(__file__).name:
            continue  # this file names the port in order to forbid it
        source = path.read_text()
        if "sync_playwright" not in source:
            continue
        for call in _LIVE_CALLS:
            if call in source:
                offenders.append((path.name, call))
    assert not offenders, (
        "these browser tests point at a developer's live backend: "
        f"{offenders}")


# ---------------------------------------------------------------------------
# 3. the approval really lands in the scratch calendar, not the real one
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_the_isolated_servers_calendar_takes_the_event_and_the_real_one_does_not(
        isolated_server):
    """The end-to-end claim, measured on the isolated server.

    The event the approval creates is in the scratch calendar, and the
    developer's own calendar does not gain it. The second half is the one
    that matters: the scratch server is *a* server, and the developer's
    machine has one running too -- the claim is that the test's server is
    not it.
    """
    import sqlite3
    import time
    import urllib.request

    from tests.isolated_server import post_command, session_token

    base = isolated_server["base"]
    home = isolated_server["home"]

    def titles_in(directory: Path) -> set:
        path = directory / "jarvis.db"
        if not path.exists():
            return set()
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return {row[0] for row in conn.execute("SELECT title FROM calendar_events")}
        except sqlite3.Error:
            return set()
        finally:
            conn.close()

    scratch_before = titles_in(home)
    real_home = Path.home() / ".jarvis-forge"
    real_before = titles_in(real_home)

    token = session_token(base)
    raised = post_command(base, token,
                          "schedule a meeting called Browser Check on 2026-12-05 09:00")
    assert raised.get("confirmation_id"), raised

    # Answer it, the way the browser test's Approve button does.
    request = urllib.request.Request(
        f"{base}/confirm/{raised['confirmation_id']}?approve=true&session_id=laptop_main",
        method="POST",
        headers={"X-JARVIS-Token": token, "Host": f"127.0.0.1:{base.rsplit(':', 1)[1]}"})
    urllib.request.urlopen(request).read()

    # It is in the scratch calendar.
    # NB: the title is "Browser Check", not "called Browser Check". The planner
    # used to keep the connective in the title (a defect fixed in iteration 82);
    # this test was written around that bug, so the expected string is the
    # corrected one. The point of this test is *isolation* (scratch vs real),
    # not the exact title — the title is just how the event is found.
    for _ in range(40):
        if "Browser Check" in titles_in(home):
            break
        time.sleep(0.25)
    assert "Browser Check" in titles_in(home), (
        f"the approved event did not reach the scratch calendar: "
        f"{titles_in(home)}")

    # And the developer's own data directory did not gain it.
    assert real_home != home, "the isolated server was pointed at the real home"
    real_after = titles_in(real_home)
    assert real_after == real_before, (
        "the approval wrote into the developer's own calendar: "
        f"{sorted(real_after - real_before)}")
    assert "Browser Check" not in real_after, (
        "the test's event is in the developer's own calendar")
