"""Phase 10 -- the master API token must never reach an unauthenticated caller.

Measured before the fix (audit PHASE9_API_SECURITY "API-0", P0), with no headers
of any kind:

    $ curl -s http://127.0.0.1:8765/auth/token
    {"auth_enabled":true,"token":"<the live token>"}     HTTP 200
    $ curl -s http://127.0.0.1:8765/dashboard | grep -c "<the live token>"
    1                                                   HTTP 200

Holding that token meant any process on the user's machine could drive all 126
tools via ``POST /command`` and approve its own confirmations via
``POST /confirm/{id}`` -- and it made the 0600 on ``$JARVIS_HOME/api_token``
moot, since the same secret was served over HTTP.

The fix under test: ``/dashboard`` is still a public shell, but what it carries
is a single-use, 60-second bootstrap code. The page spends it once at
``POST /auth/session``, which refuses a caller that is not a same-origin
browser, and gets back an in-memory session token. The master token is never
serialized into either response.

These tests run against the isolated ``JARVIS_HOME`` that ``tests/conftest.py``
installs before ``jarvis`` is first imported.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

# ``jarvis.api.__init__`` re-exports the FastAPI instance under the name ``app``,
# which shadows the submodule attribute. The module object is what holds the
# tunables these tests monkeypatch, so it is fetched by name.
app_module = importlib.import_module("jarvis.api.app")

# Set by tests/conftest.py before any jarvis import.
MASTER_TOKEN = os.environ["JARVIS_API_TOKEN"]
MASTER_HEADERS = {"X-JARVIS-Token": MASTER_TOKEN}

#: The origin the dashboard is actually served from -- see Settings.dashboard_origin.
ORIGIN = "http://127.0.0.1:8765"
EVIL_ORIGIN = "https://evil.example.com"

BOOTSTRAP_TAG = re.compile(
    r'<meta\s+name="jarvis-bootstrap"\s+content="([^"]*)"\s*/?>'
)


@pytest.fixture(scope="module")
def anon() -> TestClient:
    """A caller with no credentials: another process on the machine, or a page."""
    return TestClient(app_module.app, base_url=ORIGIN)


@pytest.fixture()
def authed() -> TestClient:
    """The legitimate operator, holding the token from ``$JARVIS_HOME/api_token``."""
    client = TestClient(app_module.app, base_url=ORIGIN)
    client.headers.update(MASTER_HEADERS)
    return client


def bootstrap_from(html: str) -> str:
    """The code the served page actually carries."""
    match = BOOTSTRAP_TAG.search(html)
    assert match, "the dashboard shell must carry a bootstrap meta tag"
    return match.group(1)


def establish_session(anon: TestClient) -> str:
    """Run exactly the sequence the dashboard performs, and return its token."""
    page = anon.get("/dashboard", headers={"Origin": ORIGIN})
    assert page.status_code == 200
    bootstrap = bootstrap_from(page.text)
    assert bootstrap and bootstrap != MASTER_TOKEN

    exchange = anon.post(
        "/auth/session",
        json={"bootstrap": bootstrap},
        headers={"Origin": ORIGIN, "Content-Type": "application/json"},
    )
    assert exchange.status_code == 200, exchange.text
    body = exchange.json()
    assert body["auth_enabled"] is True
    assert body["session_token"] and body["session_token"] != MASTER_TOKEN
    assert body["expires_in"] >= 0
    return body["session_token"]


# ---------------------------------------------------------------------------
# 1. The two disclosure routes are closed
# ---------------------------------------------------------------------------
def test_anonymous_token_route_without_origin_does_not_disclose_the_master_token(
    anon: TestClient,
) -> None:
    """The measured repro: no Origin, no credentials, nothing to be had."""
    response = anon.get("/auth/token")

    # Refused at the origin gate before the credential gate, so a foreign or
    # header-less caller cannot tell an invalid token from an absent one.
    assert response.status_code == 403
    assert MASTER_TOKEN not in response.text
    assert "token" not in response.json()


def test_anonymous_token_route_with_same_origin_still_needs_a_credential(
    anon: TestClient,
) -> None:
    """A browser is necessary but no longer sufficient."""
    response = anon.get("/auth/token", headers={"Origin": ORIGIN})

    assert response.status_code == 401
    assert MASTER_TOKEN not in response.text


def test_dashboard_html_does_not_contain_the_master_token(anon: TestClient) -> None:
    response = anon.get("/dashboard")

    assert response.status_code == 200, "the shell stays public, or the UI breaks"
    assert "Vantage" in response.text
    assert MASTER_TOKEN not in response.text
    assert "__JARVIS_BOOTSTRAP__" not in response.text, "placeholder should be replaced"
    assert '<meta name="jarvis-token"' not in response.text

    bootstrap = bootstrap_from(response.text)
    assert bootstrap != MASTER_TOKEN
    assert len(bootstrap) >= 32, "a short code would be guessable"


# ---------------------------------------------------------------------------
# 2. Origin is now mandatory, not conditional
# ---------------------------------------------------------------------------
def test_cross_origin_token_route_is_refused(anon: TestClient) -> None:
    assert anon.get("/auth/token", headers={"Origin": EVIL_ORIGIN}).status_code == 403
    assert anon.get(
        "/auth/token", headers={"Origin": EVIL_ORIGIN, **MASTER_HEADERS}
    ).status_code == 403, "a valid credential must not rescue a foreign origin here"


def test_exchange_refuses_a_headerless_and_a_foreign_caller(anon: TestClient) -> None:
    """Scraping the code out of the HTML is not enough to spend it."""
    bootstrap = bootstrap_from(anon.get("/dashboard").text)

    no_origin = anon.post("/auth/session", json={"bootstrap": bootstrap})
    assert no_origin.status_code == 403
    assert "session_token" not in no_origin.json()

    foreign = anon.post(
        "/auth/session", json={"bootstrap": bootstrap}, headers={"Origin": EVIL_ORIGIN}
    )
    assert foreign.status_code == 403

    # A caller that never clears the origin gate cannot burn the code either --
    # it is spent only by a sanctioned attempt, so the browser that was served
    # it still gets its session.
    spent = anon.post(
        "/auth/session", json={"bootstrap": bootstrap}, headers={"Origin": ORIGIN}
    )
    assert spent.status_code == 200
    assert spent.json()["session_token"] != MASTER_TOKEN

    # ...and once spent it is gone, for anyone.
    replay = anon.post(
        "/auth/session", json={"bootstrap": bootstrap}, headers={"Origin": ORIGIN}
    )
    assert replay.status_code == 403


def test_exchange_accepts_a_browser_that_omits_origin_but_says_same_origin(
    anon: TestClient,
) -> None:
    """Older WebKit may drop ``Origin``; ``Sec-Fetch-Site`` is the browser's own
    word that the request stayed on this origin, so it is honoured."""
    bootstrap = bootstrap_from(anon.get("/dashboard").text)

    response = anon.post(
        "/auth/session",
        json={"bootstrap": bootstrap},
        headers={"Sec-Fetch-Site": "same-origin"},
    )
    assert response.status_code == 200
    assert response.json()["session_token"] != MASTER_TOKEN


def test_a_fabricated_bootstrap_buys_nothing(anon: TestClient) -> None:
    response = anon.post(
        "/auth/session",
        json={"bootstrap": "x" * 43},
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 403
    assert MASTER_TOKEN not in response.text


# ---------------------------------------------------------------------------
# 3. /command: auth is still enforced, and still works
# ---------------------------------------------------------------------------
def test_command_without_credentials_is_still_refused(anon: TestClient) -> None:
    # A truly credential-less caller holds no session cookie. The module-scoped `anon`
    # client is shared with the login tests above it, so clear whatever session a prior
    # test left in its cookie jar: what is under test is "no credential presented", not
    # "the shared jar happens to still hold a token".
    anon.cookies.clear()
    response = anon.post("/command", json={"text": "what time is it"})
    assert response.status_code == 401


def test_command_with_the_master_token_still_works(authed: TestClient) -> None:
    """The legitimate path is untouched: the token on disk still authorises."""
    response = authed.post("/command", json={"text": "what time is it"})

    assert response.status_code == 200
    assert response.json()["intent"] == "time"
    assert response.json()["reply"]


# ---------------------------------------------------------------------------
# 4. The legitimate dashboard bootstrap flow, end to end
# ---------------------------------------------------------------------------
def test_dashboard_bootstraps_a_working_session(anon: TestClient) -> None:
    """GET /dashboard -> POST /auth/session -> POST /command, as the page does it."""
    page = anon.get("/dashboard", headers={"Origin": ORIGIN})
    assert page.status_code == 200
    exchange = anon.post(
        "/auth/session",
        json={"bootstrap": bootstrap_from(page.text)},
        headers={"Origin": ORIGIN, "Content-Type": "application/json"},
    )
    assert exchange.status_code == 200
    assert exchange.json()["expires_in"] > 0
    token = exchange.json()["session_token"]

    client = TestClient(app_module.app, base_url=ORIGIN)
    client.headers.update({"X-JARVIS-Token": token, "Origin": ORIGIN})

    command = client.post("/command", json={"text": "what time is it"})
    assert command.status_code == 200, command.text
    assert command.json()["intent"] == "time"
    assert command.json()["reply"]

    # The same credential drives the read cards the dashboard polls.
    assert client.get("/health").status_code == 200
    assert client.get("/tools").status_code == 200


# ---------------------------------------------------------------------------
# 4b. The same flow, driven by the dashboard's own JavaScript
# ---------------------------------------------------------------------------
FAKE_SESSION = "session-token-the-page-was-handed"

#: A DOM and network stub, so the real dashboard script can be executed outside a
#: browser. Nothing here reimplements the page: its <script> is pasted verbatim
#: between the head and the tail, and every request it *actually makes* is
#: recorded for replay against the live app.
JS_HEAD = """
const RECORD = [];
const FAKE_SESSION = "__FAKE_SESSION__";
const BOOTSTRAP_CODE = process.argv[2];
const flush = () => new Promise((resolve) => setImmediate(resolve));
const elements = new Map();
globalThis.document = {
  querySelector: (selector) => {
    const named = /name="([^"]+)"/.exec(selector);
    return named && named[1] === "jarvis-bootstrap"
      ? { content: BOOTSTRAP_CODE }
      : null;
  },
  getElementById: (id) => {
    if (!elements.has(id)) {
      elements.set(id, {
        value: "", textContent: "", innerHTML: "",
        addEventListener: () => {}, querySelectorAll: () => [],
      });
    }
    return elements.get(id);
  },
};
globalThis.setInterval = () => 0;
globalThis.fetch = async (url, options) => {
  const headers = (options && options.headers) || {};
  RECORD.push({
    url,
    method: (options && options.method) || "GET",
    headers: Object.assign({}, headers),
    body: (options && options.body) || null,
  });
  const payload =
    url === "/auth/session"
      ? { auth_enabled: true, session_token: FAKE_SESSION, expires_in: 3600 }
      : url === "/command"
        ? { reply: "It is noon.", intent: "time" }
        : {};
  return { ok: true, status: 200, json: async () => payload };
};
"""

JS_TAIL = """
(async () => {
  await flush(); await flush(); await flush();
  document.getElementById("cmd").value = "what time is it";
  await document.getElementById("send").onclick();
  await flush();
  process.stdout.write(JSON.stringify(RECORD));
})();
"""


def run_dashboard_script(bootstrap: str) -> List[Dict[str, Any]]:
    """Execute the page's own script and return the requests it chose to make."""
    node = shutil.which("node")
    if node is None:  # pragma: no cover - the test is skipped instead
        raise AssertionError("node is required for this test")

    served = TestClient(app_module.app, base_url=ORIGIN).get("/dashboard").text
    script = re.search(r"<script>(.*?)</script>", served, re.S).group(1)
    assert "authenticate" in script, "the served page must be the fixed one"

    js = (JS_HEAD.replace("__FAKE_SESSION__", FAKE_SESSION) + "\n" + script + "\n" + JS_TAIL)
    target = Path(tempfile.mkdtemp(prefix="jarvis-dash-")) / "page.js"
    target.write_text(js, encoding="utf-8")
    try:
        proc = subprocess.run(
            [node, str(target), bootstrap], capture_output=True, text=True, timeout=60
        )
    finally:
        target.unlink(missing_ok=True)
    assert proc.returncode == 0, proc.stderr[-2000:]
    return json.loads(proc.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_served_page_performs_the_bootstrap_and_authenticates(
    anon: TestClient,
) -> None:
    """Run the dashboard's real JS, then replay its recorded requests for real."""
    bootstrap = bootstrap_from(anon.get("/dashboard").text)
    calls = run_dashboard_script(bootstrap)

    exchange = next((c for c in calls if c["url"] == "/auth/session"), None)
    command = next((c for c in calls if c["url"] == "/command"), None)
    assert exchange, f"the page never exchanged its bootstrap: {calls}"
    assert command, f"the page never sent a command: {calls}"

    # What the page spends the bootstrap on, verbatim from its own code.
    assert exchange["method"] == "POST"
    assert json.loads(exchange["body"]) == {"bootstrap": bootstrap}
    assert "X-JARVIS-Token" not in exchange["headers"], "it has no credential yet"
    # ...and what it does with the answer.
    assert command["method"] == "POST"
    assert command["headers"]["X-JARVIS-Token"] == FAKE_SESSION
    assert json.loads(command["body"])["source"] == "dashboard"
    assert MASTER_TOKEN not in json.dumps(calls)

    # Replay the recorded pair against the live app. A browser adds ``Origin`` to
    # a same-origin POST on the page's behalf, so it is injected here rather than
    # expected from page code -- page script cannot set that header at all.
    spent = anon.post(
        exchange["url"],
        content=exchange["body"],
        headers={**exchange["headers"], "Origin": ORIGIN},
    )
    assert spent.status_code == 200, spent.text
    session_token = spent.json()["session_token"]
    assert session_token != MASTER_TOKEN

    replay = anon.post(
        command["url"],
        content=command["body"],
        headers={
          **{k: (session_token if v == FAKE_SESSION else v)
             for k, v in command["headers"].items()},
          "Origin": ORIGIN,
        },
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["intent"] == "time"
    assert replay.json()["reply"]


def test_session_token_is_not_the_master_token_and_leaves_no_trace(
    anon: TestClient,
) -> None:
    token = establish_session(anon)

    client = TestClient(app_module.app, base_url=ORIGIN)
    client.headers.update({"X-JARVIS-Token": token})
    body = client.get("/health").json()

    assert token != MASTER_TOKEN
    # Nothing on a protected route echoes a credential back to its holder.
    assert MASTER_TOKEN not in str(body)
    assert token not in str(body["security"])


def test_token_route_refreshes_a_session_without_touching_the_master_one(
    anon: TestClient,
) -> None:
    token = establish_session(anon)

    refreshed = anon.get(
        "/auth/token", headers={"Origin": ORIGIN, "X-JARVIS-Token": token}
    )
    assert refreshed.status_code == 200
    body = refreshed.json()
    assert body["auth_enabled"] is True
    assert body["token"] != MASTER_TOKEN
    assert body["token"] != token

    probe = TestClient(app_module.app, base_url=ORIGIN)
    probe.headers.update({"X-JARVIS-Token": body["token"]})
    assert probe.get("/tools").status_code == 200


# ---------------------------------------------------------------------------
# 5. The expiry / single-use properties themselves
# ---------------------------------------------------------------------------
def test_a_bootstrap_spent_twice_is_refused(anon: TestClient) -> None:
    bootstrap = bootstrap_from(anon.get("/dashboard").text)

    first = anon.post(
        "/auth/session", json={"bootstrap": bootstrap}, headers={"Origin": ORIGIN}
    )
    assert first.status_code == 200

    replay = anon.post(
        "/auth/session", json={"bootstrap": bootstrap}, headers={"Origin": ORIGIN}
    )
    assert replay.status_code == 403
    assert "session_token" not in replay.json()


def test_an_expired_bootstrap_is_refused(anon: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "BOOTSTRAP_TTL_SECONDS", 0.0)
    bootstrap = bootstrap_from(anon.get("/dashboard").text)

    response = anon.post(
        "/auth/session", json={"bootstrap": bootstrap}, headers={"Origin": ORIGIN}
    )
    assert response.status_code == 403


def test_an_expired_session_stops_authorising(anon: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "SESSION_TTL_SECONDS", 0.0)
    token = establish_session(anon)

    client = TestClient(app_module.app, base_url=ORIGIN)
    client.headers.update({"X-JARVIS-Token": token})
    assert client.get("/tools").status_code == 401
    assert client.post("/command", json={"text": "what time is it"}).status_code == 401


def test_revoking_sessions_kicks_the_dashboard_but_not_the_master_token(
    anon: TestClient,
) -> None:
    """The point of an in-memory session: it can be taken back, the file cannot."""
    token = establish_session(anon)
    client = TestClient(app_module.app, base_url=ORIGIN)
    client.headers.update({"X-JARVIS-Token": token})
    assert client.get("/tools").status_code == 200

    app_module.clear_dashboard_credentials()

    assert client.get("/tools").status_code == 401
    assert client.post("/command", json={"text": "what time is it"}).status_code == 401
    assert TestClient(app_module.app, base_url=ORIGIN, headers=MASTER_HEADERS).get(
        "/tools"
    ).status_code == 200


def test_the_bootstrap_store_is_bounded(monkeypatch) -> None:
    """Codes are issued on every page load, so an unbounded dict is a leak."""
    monkeypatch.setattr(app_module, "MAX_DASHBOARD_CREDENTIALS", 4)
    table: dict = {}
    for _ in range(10):
        app_module._issue_credential(table, 60)
    assert len(table) == 4


# ---------------------------------------------------------------------------
# 6. Nothing else on the surface sprays the secret
# ---------------------------------------------------------------------------
def test_no_public_route_returns_the_master_token(anon: TestClient) -> None:
    """Every route that answers without credentials, checked for the secret."""
    responses = [
        anon.get("/"),
        anon.get("/dashboard"),
        anon.get("/auth/token"),
        anon.get("/auth/token", headers={"Origin": ORIGIN}),
        anon.get("/openapi.json"),
        anon.post("/auth/session", json={"bootstrap": "nope"}, headers={"Origin": ORIGIN}),
    ]
    for response in responses:
        assert MASTER_TOKEN not in response.text, response.request.url.path
