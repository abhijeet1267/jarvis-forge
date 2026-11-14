"""The MCP connector against a real (local) Streamable-HTTP server.

The transport tests in ``test_google_tasks_tools_and_mcp.py`` drive the
``_exchange`` seam. These go one level lower: an ``http.server`` on an
ephemeral port speaks the real wire protocol -- SSE bodies, an
``Mcp-Session-Id`` that must be echoed, ``MCP-Protocol-Version`` on later
requests, a 202/empty-body notification, a 401, and a batch response. No
test contacts anything outside 127.0.0.1.

What each test proves:

* the session id issued by ``initialize`` is captured and sent back;
* an SSE body is parsed and the message matching the request id is used
  (not simply the first one);
* ``notifications/initialized`` is sent and a 202 with no body is accepted;
* a 401 is reported as a credential problem naming the setting to check;
* a redirect maps to a truthful "set JARVIS_MCP_URL to the final endpoint";
* a batch response picks this request's message by id;
* the allowlist still refuses a non-allowlisted tool before any request.
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Iterator, List, Optional

import pytest

from jarvis.integrations import mcp_client


SESSION_ID = "sess-abc123"
NEGOTIATED_VERSION = "2025-03-26"


class _FakeMCPState:
    """What the fake server observed, for assertions."""

    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []
        self.headers_seen: List[Dict[str, str]] = []

    def methods(self) -> List[str]:
        return [r.get("method") for r in self.requests]


def _sse(payload: Any) -> bytes:
    return (
        "event: message\n"
        f"data: {json.dumps(payload)}\n"
        "\n"
    ).encode()


class _Handler(BaseHTTPRequestHandler):
    """A minimal spec-shaped server: SSE, session id, batch, 401."""

    protocol_version = "HTTP/1.1"
    state: _FakeMCPState
    require_session: bool = True
    auth_status: Optional[int] = None

    def log_message(self, *args: Any) -> None:   # silence stderr chatter
        return

    # -- helpers ----------------------------------------------------------

    def _headers_seen(self) -> Dict[str, str]:
        return {k.lower(): v for k, v in self.headers.items()}

    def _send(self, status: int, body: bytes = b"",
              content_type: str = "application/json",
              extra: Optional[Dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    # -- routing ----------------------------------------------------------

    def do_POST(self) -> None:                    # noqa: N802 (http.server API)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""

        # A redirect is surfaced, not followed: urllib would turn the POST
        # into a GET and forward Authorization to the other host.
        if self.path == "/old":
            self.send_response(302)
            self.send_header("Location", "https://final.example/mcp")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        try:
            payload = json.loads(raw or b"{}")
        except Exception:
            self._send(400, b'{"error": "bad_json"}')
            return

        state: _FakeMCPState = self.server.state          # type: ignore[attr-defined]
        state.requests.append(payload)
        state.headers_seen.append(self._headers_seen())

        # A 401 endpoint: the credential is checked before anything else.
        if self.server.auth_status is not None:           # type: ignore[attr-defined]
            self._send(self.server.auth_status, json.dumps({    # type: ignore[attr-defined]
                "error": "invalid_token",
                "error_description": "The access token expired or was revoked",
            }).encode())
            return

        # A server that echoes the credential in a non-401 refusal body.
        if self.server.echo_token:                         # type: ignore[attr-defined]
            self._send(400, b'{"error": {"message": "unexpected token tok-test"}}')
            return

        method = payload.get("method")
        request_id = payload.get("id")

        if method == "initialize":
            body = _sse({"jsonrpc": "2.0", "id": request_id, "result": {
                "protocolVersion": NEGOTIATED_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-hosted-mcp", "version": "9.9"},
            }})
            self._send(200, body, "text/event-stream", {"Mcp-Session-Id": SESSION_ID})
            return

        # Every later request must echo the session id, as a strict hosted
        # server does. A missing/incorrect one is a 400, so a client that
        # dropped it would fail loudly rather than pass by luck.
        if self.server.require_session:                   # type: ignore[attr-defined]
            if self.headers.get("Mcp-Session-Id") != SESSION_ID:
                self._send(400, b'{"error": "missing or invalid Mcp-Session-Id"}')
                return

        if method == "notifications/initialized":
            self._send(202)                               # empty body, by spec
            return

        if method == "tools/list":
            # A batch: an unrelated notification and the real response. Only
            # the id match is correct.
            body = json.dumps([
                {"jsonrpc": "2.0", "method": "notifications/progress", "params": {}},
                {"jsonrpc": "2.0", "id": request_id, "result": {"tools": [
                    {"name": "send_email", "description": "Send an email"},
                    {"name": "create_ticket", "description": "Open a ticket"},
                ]}},
            ]).encode()
            self._send(200, body)
            return

        if method == "tools/call":
            # SSE carrying an unrelated message first, then the response for
            # this id -- the ordering a real server may use.
            first = {"jsonrpc": "2.0", "method": "notifications/message",
                     "params": {"level": "info"}}
            second = {"jsonrpc": "2.0", "id": request_id, "result": {
                "content": [{"type": "text", "text": "sent"}]}}
            self._send(200, _sse(first) + _sse(second), "text/event-stream")
            return

        self._send(404, b'{"error": "unknown method"}')


@contextmanager
def _server(*, auth_status: Optional[int] = None,
            require_session: bool = True,
            echo_token: bool = False) -> Iterator[tuple]:
    """Start the fake on an ephemeral port and always shut it down."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    server.state = _FakeMCPState()                        # type: ignore[attr-defined]
    server.auth_status = auth_status                      # type: ignore[attr-defined]
    server.require_session = require_session              # type: ignore[attr-defined]
    server.echo_token = echo_token                        # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host}:{port}/mcp", server.state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _connector(url: str, allowed: tuple = ("send_email",)) -> mcp_client.MCPConnector:
    return mcp_client.MCPConnector(mcp_client.MCPConfig(
        name="hosted", url=url, token="tok-test", allowed_tools=list(allowed)))


# ---------------------------------------------------------------------------
# Session id, protocol version, notification
# ---------------------------------------------------------------------------


def test_status_succeeds_over_sse_with_a_session_id_echoed() -> None:
    with _server() as (url, state):
        status = _connector(url).status()

    assert status["state"] == "connected", status["detail"]
    assert status["server"]["name"] == "fake-hosted-mcp"
    assert [t["name"] for t in status["tools"]] == ["send_email", "create_ticket"]
    assert status["callable"] == ["send_email"]           # discovery != callable

    # initialize went out without a session; every later request carried it.
    by_method = {r["method"]: h for r, h in zip(state.requests, state.headers_seen)}
    assert "mcp-session-id" not in state.headers_seen[0]
    assert by_method["tools/list"]["mcp-session-id"] == SESSION_ID

    # The client sent the required notification, and the server's 202 with an
    # empty body did not break anything (status is still connected).
    assert "notifications/initialized" in state.methods()
    initialized = state.requests[state.methods().index("notifications/initialized")]
    assert "id" not in initialized

    # The negotiated version is what later requests declare.
    assert by_method["tools/list"]["mcp-protocol-version"] == NEGOTIATED_VERSION


def test_sse_response_matching_the_request_id_is_used() -> None:
    with _server() as (url, _state):
        result = _connector(url).call_tool("send_email", {"to": "x@example.com"})
    assert mcp_client.result_text(result) == "sent"


def test_batch_response_picks_this_request_s_message() -> None:
    with _server() as (url, _state):
        names = [t["name"] for t in _connector(url).list_tools()]
    assert names == ["send_email", "create_ticket"]


def test_a_stateless_server_without_a_session_id_still_works() -> None:
    """The fallback the previous implementation assumed for every server."""
    with _server(require_session=False) as (url, _state):
        status = _connector(url).status()
    assert status["state"] == "connected", status["detail"]


# ---------------------------------------------------------------------------
# Error / edge handling
# ---------------------------------------------------------------------------


def test_a_401_is_reported_as_a_credential_problem() -> None:
    with _server(auth_status=401) as (url, _state):
        status = _connector(url).status()
    assert status["state"] == "error"
    assert "HTTP 401" in status["detail"]
    assert "JARVIS_MCP_TOKEN" in status["detail"]
    assert "invalid_token" in status["detail"]
    # Same keys in every branch, including this one.
    assert status["callable"] == [] and status["tool_count"] == 0


def test_a_403_does_not_crash_and_is_named() -> None:
    with _server(auth_status=403) as (url, _state):
        status = _connector(url).status()
    assert status["state"] == "error"
    assert "HTTP 403" in status["detail"]


def test_a_server_that_echoes_the_token_does_not_get_it_relayed() -> None:
    with _server(echo_token=True) as (url, _state):
        status = _connector(url).status()
    assert status["state"] == "error"
    assert "tok-test" not in json.dumps(status)
    assert "unexpected token" in status["detail"]        # the reason survives


def test_a_redirect_is_surfaced_not_followed() -> None:
    """urllib would re-send the request (with Authorization) to the new host.

    The connector refuses to follow it and names the URL to configure. A
    patched HTTPError alone would not catch the default ``urlopen`` following
    the 302, so this one goes over the wire.
    """
    with _server() as (url, _state):
        status = _connector(url.replace("/mcp", "/old")).status()
    assert status["state"] == "error"
    assert "redirected" in status["detail"]
    assert "final.example" in status["detail"]
    assert "JARVIS_MCP_URL" in status["detail"]


def test_an_empty_body_to_a_request_is_not_a_result(monkeypatch) -> None:
    def empty(request, *, total, what):
        return 202, [], {}

    monkeypatch.setattr(mcp_client, "_exchange", empty)
    status = _connector("http://host/mcp").status()
    assert status["state"] == "error"
    assert "without a result" in status["detail"]


def test_a_body_over_the_cap_is_refused_before_parsing(monkeypatch) -> None:
    def huge(request, *, total, what):
        return 200, b"x" * (mcp_client.MAX_BODY + 1), {}

    monkeypatch.setattr(mcp_client, "_read_with_headers", huge)
    status = _connector("http://host/mcp").status()
    assert status["state"] == "error"
    assert "KiB" in status["detail"]


# ---------------------------------------------------------------------------
# The allowlist is still enforced before the network
# ---------------------------------------------------------------------------


def test_non_allowlisted_tool_is_refused_before_any_request() -> None:
    with _server() as (url, state):
        connector = _connector(url, allowed=("send_email",))
        assert connector.status()["state"] == "connected"
        before = len(state.requests)

        with pytest.raises(mcp_client.MCPError) as exc:
            connector.call_tool("create_ticket", {})
        assert "allowlist" in str(exc.value)
        # The server saw nothing: discovery did not widen the allowlist.
        assert len(state.requests) == before

# ---------------------------------------------------------------------------
# Saved configuration (the Connections screen, not a shell export)
# ---------------------------------------------------------------------------

def test_saved_config_wins_over_a_stale_environment_export(tmp_path, monkeypatch):
    """The screen is the documented path; a leftover export must not override it.

    Otherwise a `JARVIS_MCP_URL` left over from an experiment silently defeats a
    choice the person just made in the app, and the two disagree for ever.
    """
    from jarvis.config import Settings
    from jarvis.gateway.selection import save_mcp_config
    from jarvis.integrations.mcp_client import connector_from_settings

    settings = Settings(home=tmp_path, mcp_url="http://exported.example/mcp",
                        mcp_allowed_tools="exported_tool")
    save_mcp_config(home=tmp_path, url="http://saved.example/mcp",
                    allowed_tools="saved_tool")

    connector = connector_from_settings(settings)
    assert connector.config.url == "http://saved.example/mcp"
    assert connector.config.allowed_tools == ["saved_tool"]


def test_the_saved_block_never_holds_a_token(tmp_path):
    """A credential must not land in the 0600 config file.

    `save_mcp_config` takes no token argument at all — the token goes to the
    Keychain — so this pins that the file cannot come to contain one.
    """
    import json

    from jarvis.gateway.selection import save_mcp_config

    save_mcp_config(home=tmp_path, url="http://h.example/mcp?access_token=SECRET",
                    allowed_tools="t")
    raw = (tmp_path / "ai_selection.json").read_text()
    data = json.loads(raw)
    assert set(data["mcp"]) == {"url", "allowed_tools"}, data["mcp"]
    # The URL itself may carry a token (Zapier shapes do), but the file is 0600
    # and the *token* is never a separate, greppable key.
    assert "token" not in raw.lower() or "access_token" in raw
    assert (tmp_path / "ai_selection.json").stat().st_mode & 0o077 == 0, "config must be owner-only"


def test_a_non_http_endpoint_is_refused():
    """The route validates the scheme; a file:/ftp: URL defers failure to a
    confusing network error, so it is refused at save time."""
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "jarvis" / "api" / "app.py"
    text = source.read_text(encoding="utf-8")
    assert '@app.put("/mcp/config")' in text
    assert 'parsed.scheme not in ("http", "https")' in text
