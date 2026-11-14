"""MCP: one remote server, an allowlist, and an honest connection state.

This is deliberately not a framework. It is the smallest transport that lets
Vantage talk to a real Model Context Protocol server over HTTP (the Streamable
HTTP / JSON-RPC shape that Zapier MCP, n8n's MCP node and most hosted servers
expose), discover what it offers, and call one named tool.

Where Google is a *typed* adapter (``google_calendar``, ``google_tasks``), this
is the opposite: the remote server owns the tool catalogue, so Vantage owns the
boundaries instead. Those boundaries are the reason the module exists:

* **An allowlist, never auto-discovery-as-enablement.** ``tools/list`` is
  information. Discovering a tool does not make it callable: only a name on
  ``mcp_allowed_tools`` may be invoked, and the list is empty by default. A
  server that advertises 400 actions does not hand Vantage 400 capabilities.
* **Writes stay behind the policy path.** Nothing here executes a tool on its
  own. The tool adapter in ``jarvis/tools`` registers each allowlisted tool at
  ``RiskLevel.EXTERNAL``, so the same approval flow as every other external
  write applies -- including the rule that a phone may not approve its own
  request.
* **Bounded and unsent.** One total wall-clock budget per exchange (``net``),
  a bounded response body, and a cap on how many tools are echoed back. Errors
  are classifications with a reason, never a bare exception.
* **The token is not a setting.** An MCP endpoint routinely carries a bearer
  token in its URL or header. Keeping it out of ``Settings`` is the same rule
  ``google_auth`` follows: settings are read into logs and diagnostics. The
  value is held in memory and masked by ``security.privacy.redact`` in every
  message this module produces.

Nothing here claims a server is connected because a URL is configured. The
states mirror Google's on purpose: ``not_configured``, ``disconnected``,
``connected`` (a live ``initialize``/``tools/list`` succeeded), ``error``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from .. import net
from ..logging_setup import get_logger
from ..security.privacy import MASK, redact

log = get_logger("mcp")

#: Cap on an echoed tool description -- a hostile or noisy server must not be
#: able to inflate a status payload (and the log line that accompanies it).
MAX_DESCRIPTION = 300

#: How many discovered tools a status payload will list. The count is always
#: the true count; only the sample is bounded.
MAX_LISTED_TOOLS = 50

#: A bounded response body, in bytes. A tool result larger than this is a
#: refusal with a reason, not a multi-megabyte surprise in the transcript.
MAX_BODY = 512 * 1024

#: The response header a Streamable-HTTP server may issue on ``initialize`` and
#: expects echoed on every later request. Optional: a stateless server issues
#: none, and the client keeps working.
SESSION_HEADER = "Mcp-Session-Id"

#: Sent from the second request on, stating the negotiated protocol version.
PROTOCOL_HEADER = "MCP-Protocol-Version"

class MCPState(str, Enum):
    NOT_CONFIGURED = "not_configured"
    DISCONNECTED = "disconnected"
    CONNECTED = "connected"
    ERROR = "error"

class MCPError(RuntimeError):
    """A failure whose message is safe to show a person (already redacted)."""

    def __init__(self, message: str, state: MCPState = MCPState.ERROR) -> None:
        super().__init__(message)
        self.state = state

@dataclass
class MCPConfig:
    """Where one server lives, and what it is allowed to do.

    ``token`` is *not* serialised by :meth:`public`; status payloads are built
    from that method and never touch the raw value.
    """

    name: str = "mcp"
    url: str = ""
    token: str = ""
    #: ``Authorization`` value to send when ``token`` is set. Some servers want
    #: ``Bearer <token>``, others a bare header value; the prefix is config.
    auth_header: str = "Authorization"
    auth_prefix: str = "Bearer "
    allowed_tools: List[str] = field(default_factory=list)

    @property
    def configured(self) -> bool:
        # A token embedded in the URL is still a token; do not print it.
        return bool((self.url or "").strip())

    def public(self) -> Dict[str, Any]:
        """The safe description: no token, and a redacted URL.

        ``redact`` masks a token that ended up in the query string, which is
        how a hosted MCP URL is often shaped. The host and path survive so the
        person can still tell *which* server this is.
        """
        return {
            "name": self.name,
            "url": redact((self.url or "").strip()),
            "configured": self.configured,
            "has_token": bool(self.token),
            "allowed_tools": list(self.allowed_tools),
        }

def parse_allowed_tools(raw: str) -> List[str]:
    """Parse ``JARVIS_MCP_ALLOWED_TOOLS``: a comma-separated list.

    Names are trimmed and de-duplicated in order. An empty string means the
    allowlist is empty, which is the safe default -- not "allow everything".
    """
    seen: List[str] = []
    for part in (raw or "").split(","):
        name = part.strip()
        if name and name not in seen:
            seen.append(name)
    return seen

#: The connector for this process. Class-level for the same reason
#: ``GoogleAuth._pending`` is: routes and tools construct fresh objects per
#: request, and configuration genuinely belongs to the process, not to a
#: short-lived object. Tests inject their own instance instead.
_active: Optional["MCPConnector"] = None

def configure(connector: Optional["MCPConnector"]) -> None:
    """Install (or clear) the process-wide connector. Used by startup and tests."""
    global _active
    _active = connector

def active_connector() -> Optional["MCPConnector"]:
    return _active

def connector_from_settings(settings: Any) -> "MCPConnector":
    """Build a connector from saved configuration, then from ``Settings``.

    Saved configuration (the Connections screen, ``save_mcp_config``) wins over a
    shell export, because the screen is the documented path and an environment
    variable left over from an experiment must not silently override a choice the
    person made in the app. Either source is fine; neither is required.
    """
    saved: Dict[str, str] = {}
    try:
        from ..gateway.selection import mcp_config

        saved = mcp_config(getattr(settings, "home", None))
    except Exception:                     # a broken file must not break the connector
        saved = {}

    name = (saved.get("name") or getattr(settings, "mcp_name", "") or "mcp").strip() or "mcp"
    url = (saved.get("url") or getattr(settings, "mcp_url", "") or "").strip()
    allowed_raw = saved.get("allowed_tools") or getattr(settings, "mcp_allowed_tools", "") or ""
    # The token is only ever in the environment or the Keychain -- never this file.
    token = getattr(settings, "mcp_token", "") or ""
    if not token:
        try:
            from ..security.providers import KeychainStorage

            token = KeychainStorage.retrieve("mcp") or ""
        except Exception:
            token = ""
    return MCPConnector(MCPConfig(
        name=name,
        url=url,
        token=token,
        allowed_tools=parse_allowed_tools(allowed_raw),
    ))

def _read_with_headers(request: urllib.request.Request, *, total: float,
                       what: str) -> Tuple[int, bytes, Dict[str, str]]:
    """One bounded read that also returns the response headers.

    ``net.http_read`` returns bytes only, and the Streamable-HTTP handshake
    needs one header (``Mcp-Session-Id``). This is deliberately the same
    TOTAL-deadline pattern as ``net.http_read`` -- a worker thread and
    ``join(total)`` -- rather than a second, unbounded ``urlopen`` call. The
    sentence-level budget is consumed the same way, so one MCP exchange cannot
    outrun the sentence it belongs to.
    """
    allowed = min(float(total), net.remaining_budget())
    if allowed <= 0:
        raise net.DeadlineExceeded(
            float(total),
            f"{what} (the sentence's network budget was already spent before this read started)")

    box: Dict[str, Any] = {}

    def read_it() -> None:
        try:
            with _OPENER.open(request, timeout=net.GAP_TIMEOUT) as response:
                box["status"] = int(getattr(response, "status", 200) or 200)
                box["headers"] = {str(k).lower(): str(v) for k, v in response.headers.items()}
                box["body"] = response.read()
        except BaseException as exc:      # surfaced by the caller thread below
            box["error"] = exc

    worker = threading.Thread(target=read_it, daemon=True)
    started = time.monotonic()
    worker.start()
    worker.join(allowed)
    if worker.is_alive():
        net._consume(allowed)
        raise net.DeadlineExceeded(allowed, what)
    net._consume(time.monotonic() - started)
    if "error" in box:
        raise box["error"]
    return box.get("status", 200), bytes(box.get("body") or b""), box.get("headers") or {}


#: HTTP statuses urllib may (or may not) follow on its own. When one reaches
#: us it means the client refused to re-send the request, and the honest fix
#: is the final URL, not a retry loop.
_REDIRECT_CODES = (301, 302, 303, 307, 308)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect.

    Two reasons, both real: urllib turns a redirected POST into a GET (so the
    MCP request is broken anyway), and it re-sends the request headers --
    including ``Authorization`` -- to whatever host the ``Location`` names.
    A bearer token must not travel to a host the person did not configure. The
    3xx is surfaced so the status can say which final URL to use.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _exchange(request: urllib.request.Request, *, total: float,
              what: str) -> Tuple[int, List[Any], Dict[str, str]]:
    """One bounded POST, decoded into JSON-RPC messages plus response headers.

    A Streamable-HTTP server may answer with one JSON object, a JSON array
    (batch), or an SSE stream whose ``data:`` events carry the same messages.
    All three are normalised to a list here; picking the message that answers
    a given request id is :func:`_pick_message`'s job.
    """
    try:
        status, raw, headers = _read_with_headers(request, total=total, what=what)
    except urllib.error.HTTPError as exc:
        code = int(exc.code)
        try:
            body = exc.read() or b""
        finally:
            exc.close()
        error_headers = {str(k).lower(): str(v) for k, v in (exc.headers or {}).items()}
        if code in _REDIRECT_CODES:
            location = redact(error_headers.get("location") or "").strip()
            target = location[:200] if location else "another URL"
            raise MCPError(
                f"{what} redirected the request (HTTP {code}) to {target}. "
                "Set JARVIS_MCP_URL to the final endpoint.", MCPState.ERROR) from exc
        detail = _error_detail(body) or _text_detail(body)
        suffix = f": {detail}" if detail else ""
        if code in (401, 403):
            raise MCPError(
                f"{what} refused the credential (HTTP {code}){suffix}. Check "
                "JARVIS_MCP_TOKEN -- a hosted endpoint's token can expire or be "
                "revoked.", MCPState.ERROR) from exc
        raise MCPError(
            f"{what} refused the request (HTTP {code}){suffix}.", MCPState.ERROR) from exc
    except net.DeadlineExceeded as exc:
        raise MCPError(str(exc), MCPState.ERROR) from exc
    except Exception as exc:                  # DNS, timeout, TLS, reset
        raise MCPError(
            f"Could not reach {what} ({type(exc).__name__}). "
            "Check the endpoint and that the server is running.",
        ) from exc

    if len(raw) > MAX_BODY:
        raise MCPError(f"{what} returned more than {MAX_BODY // 1024} KiB, so it was not read.")

    content_type = headers.get("content-type", "").lower()
    try:
        if "text/event-stream" in content_type or raw.lstrip()[:5] in (b"event", b"data:"):
            messages = _parse_sse(raw)
            if not messages and raw.strip():
                raise MCPError(f"{what} answered with a stream that carried no JSON-RPC message.")
            return status, messages, headers
        if not raw.strip():
            # A 202 with an empty body is the correct answer to a notification,
            # and a legitimate "nothing to say" to anything else -- the caller
            # decides which one it asked for.
            return status, [], headers
        data = json.loads(raw)
    except MCPError:
        raise
    except Exception as exc:
        raise MCPError(f"{what} returned a response that was not JSON.") from exc
    if isinstance(data, dict):
        return status, [data], headers
    if isinstance(data, list):
        return status, [m for m in data if isinstance(m, (dict, list))], headers
    raise MCPError("The MCP server answered with an unexpected JSON shape.")


def _parse_sse(raw: bytes) -> List[Any]:
    """The JSON-RPC messages carried by an SSE body.

    Only ``data:`` fields matter; ``event:``/``id:``/comments and keep-alive
    lines are ignored. One ``data:`` block may itself be a JSON array (a
    batch), which is flattened.
    """
    messages: List[Any] = []

    def flush(lines: List[str]) -> None:
        if not lines:
            return
        chunk = "\n".join(lines).strip()
        if not chunk:
            return
        try:
            data = json.loads(chunk)
        except Exception:
            return                        # a non-JSON event is not a response
        if isinstance(data, list):
            messages.extend(m for m in data if isinstance(m, (dict, list)))
        elif isinstance(data, dict):
            messages.append(data)

    pending: List[str] = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.rstrip("\r")
        if not line:
            flush(pending)
            pending = []
            continue
        if line.startswith(":"):
            continue                      # SSE comment / keep-alive
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "data":
            pending.append(value)
    flush(pending)
    return messages


def _pick_message(messages: List[Any], request_id: int) -> Dict[str, Any]:
    """The response for ``request_id``, from a single message or a batch.

    A server that sends one response is answered with it even if its id does
    not match (some proxies rewrite it). A batch must contain the right id:
    guessing among several responses would attach another call's result to
    this one -- the exact "read succeeded but lied" shape this project keeps
    fixing.
    """
    flat: List[Dict[str, Any]] = []
    for message in messages:
        if isinstance(message, list):
            flat.extend(m for m in message if isinstance(m, dict))
        elif isinstance(message, dict):
            flat.append(message)
    responses = [m for m in flat if "result" in m or "error" in m]
    for message in responses:
        if message.get("id") == request_id and not isinstance(message.get("id"), bool):
            return message
    if len(responses) == 1:
        return responses[0]
    if not responses:
        raise MCPError("The MCP server answered without a result.")
    raise MCPError("The MCP server's batch response carried no message for this request.")


def _error_detail(body: bytes) -> str:
    """A short, redacted reason from an error body, if one is readable."""
    try:
        data = json.loads(body or b"{}")
    except Exception:
        return ""
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            err = err.get("message") or err.get("code")
        if not err and data.get("error_description"):
            err = data.get("error_description")
        if not err:
            err = data.get("message") or data.get("detail")
        if err:
            return redact(str(err))[:160]
    return ""


def _text_detail(body: bytes) -> str:
    """A short, redacted reason from a non-JSON error body (often HTML)."""
    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        return ""
    if "<" in text and ">" in text:
        import re
        text = re.sub(r"<[^>]+>", " ", text)
    text = " ".join(text.split())
    return redact(text)[:160]

class MCPConnector:
    """One MCP server, one allowlist, one status method.

    Each call is a complete JSON-RPC request over Streamable HTTP. A session
    id issued by ``initialize`` (``Mcp-Session-Id``) is kept for the lifetime
    of this connector and echoed on later requests, because the spec lets a
    server *require* that; a server that issues none is served statelessly,
    which is what the previous implementation assumed for every server.
    """

    #: Total wall-clock ceiling for one exchange.
    TIMEOUT = 20.0

    PROTOCOL_VERSION = "2025-06-18"

    def __init__(self, config: MCPConfig) -> None:
        self.config = config
        self._next_id = 1
        #: Issued by the server's initialize response headers, if any.
        self._session_id: Optional[str] = None
        #: The version agreed at initialize and sent on later requests.
        self._protocol_version: Optional[str] = None

    # -- transport ---------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.config.token:
            headers[self.config.auth_header] = self.config.auth_prefix + self.config.token
        if self._session_id:
            headers[SESSION_HEADER] = self._session_id
        if self._protocol_version:
            headers[PROTOCOL_HEADER] = self._protocol_version
        return headers

    def _scrub(self, text: str) -> str:
        """Mask this connector's own token, then run the general redaction.

        ``privacy.redact`` knows the *settings* secrets; an MCP token is held
        on ``MCPConfig`` and may not be in that set (a test-injected connector
        certainly is not). A server that echoes the credential in an error
        body must not have it relayed. Exact-value masking first -- that is
        certain, not heuristic -- then the shared patterns.
        """
        token = (self.config.token or "").strip()
        if token and token in text:
            text = text.replace(token, MASK)
        return redact(text)

    def _send(self, payload: Dict[str, Any]) -> Tuple[int, List[Any], Dict[str, str]]:
        request = urllib.request.Request(
            self.config.url.strip(), method="POST",
            data=json.dumps(payload).encode(),
            headers=self._headers(),
        )
        return _exchange(request, total=self.TIMEOUT,
                         what=f"MCP server {self.config.name}")

    def _notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> bool:
        """Send a JSON-RPC notification (no id). A 202 with no body is normal.

        A failed notification does not fail the handshake that preceded it:
        the session is still usable, and if it is not, the next request fails
        honestly on its own. The failure is logged rather than swallowed.
        """
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "method": method,
                                   "params": params or {}}
        try:
            self._send(payload)
        except MCPError as exc:
            log.warning("MCP notification %s was not accepted: %s",
                        method, redact(str(exc))[:200])
            return False
        return True

    def _rpc(self, method: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """Send one JSON-RPC request and return its ``result``.

        A JSON-RPC *error* is raised as :class:`MCPError` with the server's own
        message (redacted). A successful envelope missing ``result`` is a
        malformed server, reported as such rather than as ``None``.
        """
        if not self.config.configured:
            raise MCPError(
                "No MCP server is configured. Set JARVIS_MCP_URL to an MCP endpoint.",
                MCPState.NOT_CONFIGURED,
            )
        # A hosted server may require a session before it accepts any other
        # request, and the tool adapter builds a fresh connector per call. So
        # the first non-handshake request performs the handshake itself. The
        # allowlist was already checked in ``call_tool``, so this never turns
        # a refused name into a network request.
        if method != "initialize" and self._protocol_version is None:
            self.initialize()
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": method,
            "params": params or {},
        }
        self._next_id += 1
        try:
            _status, messages, headers = self._send(payload)
        except MCPError as exc:
            # The transport builds its message from the server's body, which
            # may echo the credential; mask it before it travels any further.
            raise MCPError(self._scrub(str(exc)), exc.state) from None
        if method == "initialize":
            session_id = (headers.get(SESSION_HEADER.lower()) or "").strip()
            if session_id:
                self._session_id = session_id
        body = _pick_message(messages, payload["id"])
        if body.get("error"):
            err = body["error"]
            if isinstance(err, dict):
                msg = err.get("message") or err.get("code") or "the server refused it"
            else:
                msg = str(err)
            raise MCPError(
                f"{self.config.name} answered with an error: "
                f"{self._scrub(str(msg))[:200]}")
        if "result" not in body:
            raise MCPError(f"{self.config.name} answered without a result.")
        return body["result"]

    # -- protocol ----------------------------------------------------------

    def initialize(self) -> Dict[str, Any]:
        """The handshake. Its success is what "connected" means."""
        # A handshake starts a new session, so anything left from a previous
        # one is dropped first: the spec forbids sending ``initialize`` on an
        # existing session, and ``status()`` may be pressed more than once.
        self._session_id = None
        self._protocol_version = None
        result = self._rpc("initialize", {
            "protocolVersion": self.PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "vantage", "version": "1.0"},
        })
        if isinstance(result, dict):
            version = result.get("protocolVersion")
            if isinstance(version, str) and version.strip():
                self._protocol_version = version.strip()
        if not self._protocol_version:
            self._protocol_version = self.PROTOCOL_VERSION
        # Required by the spec after a successful initialize. Servers that do
        # not want it answer 202 with no body; ``_notify`` treats that as fine.
        self._notify("notifications/initialized")
        return result if isinstance(result, dict) else {}

    def list_tools(self) -> List[Dict[str, Any]]:
        """Every tool the server advertises, from every page.

        Pagination is followed (``nextCursor``) rather than truncated at the
        first page: a server that pages its catalogue would otherwise silently
        hide tools, which is exactly the "read succeeded but lied" shape this
        project keeps fixing. A page cap bounds a server that pages forever.
        """
        tools: List[Dict[str, Any]] = []
        cursor = ""
        for _ in range(20):                        # hard page cap
            result = self._rpc("tools/list", {"cursor": cursor} if cursor else {})
            if not isinstance(result, dict):
                break
            page = result.get("tools") or []
            if isinstance(page, list):
                tools.extend(t for t in page if isinstance(t, dict) and t.get("name"))
            cursor = str(result.get("nextCursor") or "")
            if not cursor:
                break
        return tools

    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Call one allowlisted tool.

        The allowlist is checked here, at the transport, not only in the tool
        adapter that wraps this method: a future caller that reaches the
        connector directly must hit the same boundary.
        """
        if not name:
            raise MCPError("A tool name is required.")
        if name not in self.config.allowed_tools:
            raise MCPError(
                f"{name!r} is not on this server's allowlist, so it was not called. "
                "Add it to JARVIS_MCP_ALLOWED_TOOLS deliberately.",
            )
        result = self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        if not isinstance(result, dict):
            raise MCPError(f"{self.config.name} returned an unreadable tool result.")
        # MCP reports a tool-level failure in-band with isError; keep that
        # distinct from "the call itself failed", which raised above.
        if result.get("isError"):
            raise MCPError(self._scrub(_content_text(result)) or f"{name} reported an error.")
        return result

    # -- status ------------------------------------------------------------

    def status(self, *, probe: bool = True) -> Dict[str, Any]:
        """The real state, in the same vocabulary the Google screen uses.

        ``probe=False`` reports configuration without a network call, which is
        what a page refresh wants when the server is known to be down. With
        ``probe=True`` (the default) "connected" means a live handshake
        succeeded -- a saved URL is not a connection.
        """
        base = self.config.public()
        # Every branch carries the same keys. A consumer that reads
        # `status["callable"]` must not KeyError on the one state where nothing
        # is callable -- that is how "not configured" would look like a crash
        # rather than an honest answer. Found by the live runbook, not a unit
        # test: the tests read a key that only the connected branch supplied.
        empty = {"tools": [], "tool_count": 0, "callable": [], "server": None}
        if not self.config.configured:
            return {**base, **empty, "state": MCPState.NOT_CONFIGURED.value,
                    "detail": "No MCP server is configured. Set JARVIS_MCP_URL."}
        if not probe:
            return {**base, **empty, "state": MCPState.DISCONNECTED.value,
                    "detail": "Configured; not checked yet."}
        try:
            server = self.initialize()
            tools = self.list_tools()
        except MCPError as exc:
            return {**base, **empty, "state": exc.state.value, "detail": str(exc)}

        info = server.get("serverInfo") if isinstance(server.get("serverInfo"), dict) else {}
        advertised = [
            {"name": str(t.get("name")),
             "description": redact(str(t.get("description") or ""))[:MAX_DESCRIPTION]}
            for t in tools
        ]
        return {
            **base,
            "state": MCPState.CONNECTED.value,
            "detail": "Connected to the MCP server.",
            "server": {"name": str(info.get("name") or self.config.name),
                       "version": str(info.get("version") or "")},
            "tool_count": len(tools),
            "tools": advertised[:MAX_LISTED_TOOLS],
            # Spelled out so a reader never has to infer that "listed" is not
            # "callable": only names in the allowlist may be invoked.
            "callable": [t for t in self.config.allowed_tools
                         if t in {a["name"] for a in advertised}],
        }

def _content_text(result: Dict[str, Any]) -> str:
    """Flatten an MCP ``content`` array into a readable, redacted string."""
    parts: List[str] = []
    for item in result.get("content") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text" and item.get("text"):
            parts.append(str(item["text"]))
        elif item.get("type") == "resource" and isinstance(item.get("resource"), dict):
            text = item["resource"].get("text")
            if text:
                parts.append(str(text))
    return redact(" ".join(parts).strip())[:2000]

def result_text(result: Dict[str, Any]) -> str:
    """The text a person would read from a tool result (exported for the adapter)."""
    return _content_text(result)

def describe_mcp(config: Optional[MCPConfig] = None) -> Dict[str, Any]:
    """Setup requirements, with no secret in them."""
    if config is None:
        return {
            "configured": False,
            "setup": [
                "Create an MCP server endpoint (Zapier MCP, an n8n MCP node, or another "
                "Streamable-HTTP MCP server)",
                "Set JARVIS_MCP_URL to its endpoint",
                "Set JARVIS_MCP_TOKEN if the endpoint needs a bearer token",
                "List only the tools you want callable in JARVIS_MCP_ALLOWED_TOOLS",
            ],
        }
    public = config.public()
    setup: List[str] = []
    if not config.configured:
        setup.append("Set JARVIS_MCP_URL to the server's endpoint.")
    if not config.allowed_tools:
        setup.append(
            "The allowlist is empty, so no tool can be called. Add the names you "
            "want to JARVIS_MCP_ALLOWED_TOOLS.")
    return {**public, "setup": setup}