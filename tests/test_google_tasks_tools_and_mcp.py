"""Google Tasks tools + the MCP connector.

These tests cover the two things this change adds and the properties that
matter for each. They are written the way the rest of this suite is: every
assertion is about a failure mode the project has actually hit, not a happy
path.

Google Tasks tools
  * a failed read is a failure, never "no tasks";
  * a write reports success only when Google's own response carries the task;
  * the write tools are EXTERNAL and confirm -- a phone request must not be
    able to complete a Google write without approval.

MCP connector
  * a configured URL is not a connection; "connected" needs a live handshake;
  * the allowlist is the only thing that makes a tool callable, and discovery
    never widens it;
  * the token appears in no status payload, detail, or error message;
  * a refusal and an unreachable server are different states;
  * the tool adapter registers one Vantage tool per allowlisted name, all at
    the EXTERNAL tier.

No test contacts a real server. The MCP transport is driven through the same
``http_read`` seam the production code uses.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple

import pytest

from jarvis.config import Settings
from jarvis.core.models import RiskLevel
from jarvis.integrations import mcp_client
from jarvis.integrations.calendar_provider import ProviderResults, ProviderStatus
from jarvis.tools import google_tasks as gt
from jarvis.tools import mcp_tools

# ---------------------------------------------------------------------------
# Google Tasks tools
# ---------------------------------------------------------------------------


class _Ctx:
    """The subset of ToolContext these handlers read."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()
        self.device_id = "laptop_main"


def _client_with(monkeypatch, **methods):
    """Point ``gt._client`` at an object with the given methods."""
    fake = type("FakeClient", (), methods)()
    monkeypatch.setattr(gt, "_client", lambda settings: fake)
    return fake


@pytest.mark.asyncio
async def test_list_failure_is_a_failure_not_an_empty_list(monkeypatch):
    _client_with(
        monkeypatch,
        tasks=lambda *a, **kw: ProviderResults(
            [], ProviderStatus.AUTH_ERROR, detail="Google no longer accepts the saved access."),
    )
    result = await gt.list_google_tasks(_Ctx())
    assert result.ok is False
    assert result.output["read_succeeded"] is False
    assert result.output["tasks"] == []
    # The reason survives: "no tasks" would be a different, false claim.
    assert "Google no longer accepts" in result.error


@pytest.mark.asyncio
async def test_a_real_empty_list_is_reported_as_empty(monkeypatch):
    _client_with(
        monkeypatch,
        tasks=lambda *a, **kw: ProviderResults([], ProviderStatus.REAL_EMPTY),
    )
    result = await gt.list_google_tasks(_Ctx())
    assert result.ok is True
    assert result.output["read_succeeded"] is True
    assert "No open Google tasks" in result.message


@pytest.mark.asyncio
async def test_list_maps_tasks(monkeypatch):
    class T:
        def as_dict(self):
            return {"id": "t1", "title": "Buy milk", "done": False, "list_id": "@default"}

    _client_with(monkeypatch,
                 tasks=lambda *a, **kw: ProviderResults([T()], ProviderStatus.HAS_RESULTS))
    result = await gt.list_google_tasks(_Ctx())
    assert result.ok is True
    assert result.output["tasks"][0]["title"] == "Buy milk"
    assert result.output["source"] == "google"


@pytest.mark.asyncio
async def test_create_requires_a_title(monkeypatch):
    called: List[Any] = []
    _client_with(monkeypatch, create=lambda *a, **k: called.append((a, k)) or {"ok": True})
    result = await gt.add_google_task(_Ctx(), title="   ")
    assert result.ok is False
    assert not called, "a task with no title must not reach Google"


@pytest.mark.asyncio
async def test_create_rejects_an_unreadable_due_date(monkeypatch):
    called: List[Any] = []
    _client_with(monkeypatch, create=lambda *a, **k: called.append((a, k)) or {"ok": True})
    result = await gt.add_google_task(_Ctx(), title="x", due="next tuesday-ish")
    assert result.ok is False
    assert "YYYY-MM-DD" in result.error
    assert not called


@pytest.mark.asyncio
async def test_create_reports_success_only_with_google_s_id(monkeypatch):
    class T:
        def as_dict(self):
            return {"id": "", "title": "x"}

    # A 200 with no id is not proof a task exists; do not claim it did.
    _client_with(monkeypatch, create=lambda *a, **k: {"ok": True, "task": T().as_dict()})
    result = await gt.add_google_task(_Ctx(), title="x")
    assert result.ok is True
    assert result.verified is False


@pytest.mark.asyncio
async def test_create_failure_carries_google_s_reason(monkeypatch):
    _client_with(monkeypatch,
                 create=lambda *a, **k: {"ok": False, "error": "Tasks API disabled.",
                                         "status": "provider_error"})
    result = await gt.add_google_task(_Ctx(), title="x")
    assert result.ok is False
    assert "Tasks API disabled" in result.error


@pytest.mark.asyncio
async def test_complete_needs_a_reference(monkeypatch):
    called: List[Any] = []
    _client_with(monkeypatch, complete=lambda *a, **k: called.append((a, k)) or {"ok": True})
    result = await gt.complete_google_task(_Ctx(), task_ref="")
    assert result.ok is False
    assert not called


@pytest.mark.asyncio
async def test_complete_does_not_claim_done_when_google_disagrees(monkeypatch):
    # An id-shaped reference skips title resolution and goes straight to Google,
    # which is what this test is about: a 200 that did not change the status.
    _client_with(monkeypatch, complete=lambda *a, **k: {
        "ok": False, "status": "unverified",
        "error": "Google accepted the update but still reports the task as not completed",
    })
    result = await gt.complete_google_task(_Ctx(), task_ref="task-id-0001")
    assert result.ok is False
    assert "still reports" in result.error


class _Task:
    def __init__(self, id: str, title: str) -> None:
        self.id, self.title = id, title


def _listing(items, status=ProviderStatus.HAS_RESULTS, detail=""):
    return ProviderResults(items, status, detail=detail)


@pytest.mark.asyncio
async def test_complete_resolves_a_title_to_the_one_matching_task(monkeypatch):
    calls: List[Any] = []

    class Client:
        def tasks(self, **kw):
            return _listing([_Task("id-buy-milk", "Buy milk"),
                             _Task("id-call", "Call the dentist")])

        def complete(self, task_id, list_id=""):
            calls.append(task_id)
            return {"ok": True, "task": {"id": task_id, "title": "Buy milk"}}

    _client_with(monkeypatch)
    monkeypatch.setattr(gt, "_client", lambda settings: Client())
    result = await gt.complete_google_task(_Ctx(), task_ref="buy milk")
    assert result.ok is True
    assert calls == ["id-buy-milk"]


@pytest.mark.asyncio
async def test_complete_refuses_an_ambiguous_title(monkeypatch):
    calls: List[Any] = []

    class Client:
        def tasks(self, **kw):
            return _listing([_Task("a", "Pay rent"), _Task("b", "Pay rent")])

        def complete(self, task_id, list_id=""):
            calls.append(task_id)
            return {"ok": True}

    _client_with(monkeypatch)
    monkeypatch.setattr(gt, "_client", lambda settings: Client())
    result = await gt.complete_google_task(_Ctx(), task_ref="Pay rent")
    assert result.ok is False
    assert not calls, "an ambiguous title must not complete an arbitrary task"
    assert "Several" in result.error or "2 open" in result.error


@pytest.mark.asyncio
async def test_complete_refuses_a_title_that_is_not_there(monkeypatch):
    calls: List[Any] = []

    class Client:
        def tasks(self, **kw):
            return _listing([_Task("a", "Buy milk")])

        def complete(self, task_id, list_id=""):
            calls.append(task_id)
            return {"ok": True}

    _client_with(monkeypatch)
    monkeypatch.setattr(gt, "_client", lambda settings: Client())
    result = await gt.complete_google_task(_Ctx(), task_ref="Buy bread")
    assert result.ok is False
    assert not calls
    assert "did not change anything" in result.error


@pytest.mark.asyncio
async def test_complete_does_not_resolve_against_an_unreadable_list(monkeypatch):
    calls: List[Any] = []

    class Client:
        def tasks(self, **kw):
            return _listing([], ProviderStatus.AUTH_ERROR, detail="revoked")

        def complete(self, task_id, list_id=""):
            calls.append(task_id)
            return {"ok": True}

    _client_with(monkeypatch)
    monkeypatch.setattr(gt, "_client", lambda settings: Client())
    result = await gt.complete_google_task(_Ctx(), task_ref="Buy milk")
    assert result.ok is False
    assert not calls
    assert "revoked" in result.error


def test_write_tools_are_external_and_confirm():
    """A Google write leaves the machine and must go through approval."""
    by_name = {spec.name: spec for spec, _ in gt.SPECS}
    for name in ("add_google_task", "complete_google_task"):
        assert by_name[name].risk is RiskLevel.EXTERNAL, name
        assert by_name[name].requires_confirmation is True, name
    assert by_name["list_google_tasks"].risk is RiskLevel.READ


# ---------------------------------------------------------------------------
# MCP: transport, allowlist, status
# ---------------------------------------------------------------------------


def _connector(url: str = "http://127.0.0.1:9/mcp", token: str = "",
               allowed: Tuple[str, ...] = ("send_email",), name: str = "test") -> Any:
    return mcp_client.MCPConnector(mcp_client.MCPConfig(
        name=name, url=url, token=token, allowed_tools=list(allowed)))


def _install_transport(monkeypatch, handler):
    """Drive ``_exchange`` through a handler returning a parsed payload.

    ``_exchange`` is the seam that wraps the bounded read and turns a response
    body into JSON-RPC messages, so the fake returns messages rather than
    bytes -- matching what the production code hands back, so the envelope
    handling below it is real. A notification (no id) gets the spec's 202 with
    an empty body, as a real server would answer it.
    """
    import json as _json

    def fake(request, *, total, what):
        payload = _json.loads(request.data.decode())
        if "id" not in payload:                    # notifications/initialized
            return 202, [], {}
        payload = handler(request)
        if isinstance(payload, Exception):
            raise payload
        return 200, [payload], {}

    monkeypatch.setattr(mcp_client, "_exchange", fake)


def _method_of(request) -> str:
    return json.loads(request.data.decode())["method"]


def test_not_configured_is_named_not_guessed():
    status = _connector(url="", allowed=()).status()
    assert status["state"] == "not_configured"
    assert "JARVIS_MCP_URL" in status["detail"]
    assert status["tool_count"] == 0


def test_connected_requires_a_live_handshake(monkeypatch):
    def handler(request):
        method = _method_of(request)
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": 1,
                    "result": {"serverInfo": {"name": "zapier", "version": "1.2"}}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": 2, "result": {"tools": [
                {"name": "send_email", "description": "Send an email"},
                {"name": "delete_everything", "description": "Danger"},
            ]}}
        raise AssertionError(method)

    _install_transport(monkeypatch, handler)
    status = _connector().status()
    assert status["state"] == "connected"
    assert status["server"]["name"] == "zapier"
    assert status["tool_count"] == 2
    # Discovery lists both, but only the allowlisted one is callable.
    assert [t["name"] for t in status["tools"]] == ["send_email", "delete_everything"]
    assert status["callable"] == ["send_email"]


def test_discovered_but_unlisted_tool_refuses(monkeypatch):
    called: List[str] = []

    def handler(request):
        called.append(_method_of(request))
        return {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}

    _install_transport(monkeypatch, handler)
    with pytest.raises(mcp_client.MCPError) as exc:
        _connector().call_tool("delete_everything", {})
    assert "allowlist" in str(exc.value)
    # It never reached the server: the boundary is before the network.
    assert "tools/call" not in called


def test_unreachable_server_is_an_error_state_not_a_connection(monkeypatch):
    # Patch the transport beneath the classifier, so the classification is
    # under test rather than stubbed out.
    def refuse(request, *, total, what):
        raise ConnectionResetError()

    monkeypatch.setattr(mcp_client, "_read_with_headers", refuse)
    status = _connector().status()
    assert status["state"] == "error"
    assert "Could not reach" in status["detail"]


def test_json_rpc_error_is_reported_with_the_server_s_message(monkeypatch):
    _install_transport(monkeypatch, lambda request: {
        "jsonrpc": "2.0", "id": 1,
        "error": {"code": -32602, "message": "Invalid params: 'to' is required"},
    })
    status = _connector().status()
    assert status["state"] == "error"
    assert "'to' is required" in status["detail"]


def test_a_successful_envelope_without_result_is_not_success(monkeypatch):
    _install_transport(monkeypatch, lambda request: {"jsonrpc": "2.0", "id": 1})
    with pytest.raises(mcp_client.MCPError):
        _connector().initialize()


def test_list_tools_follows_pagination(monkeypatch):
    pages = {
        "": {"tools": [{"name": "a"}], "nextCursor": "p2"},
        "p2": {"tools": [{"name": "b"}], "nextCursor": ""},
    }

    def handler(request):
        params = json.loads(request.data.decode())["params"]
        return {"jsonrpc": "2.0", "id": 1, "result": pages[params.get("cursor", "")]}

    _install_transport(monkeypatch, handler)
    assert [t["name"] for t in _connector().list_tools()] == ["a", "b"]


def test_in_band_tool_error_is_a_failure_not_a_result(monkeypatch):
    _install_transport(monkeypatch, lambda request: {
        "jsonrpc": "2.0", "id": 1,
        "result": {"isError": True, "content": [{"type": "text", "text": "no such inbox"}]},
    })
    with pytest.raises(mcp_client.MCPError) as exc:
        _connector().call_tool("send_email", {})
    assert "no such inbox" in str(exc.value)


def test_a_successful_call_returns_its_text(monkeypatch):
    _install_transport(monkeypatch, lambda request: {
        "jsonrpc": "2.0", "id": 1,
        "result": {"content": [{"type": "text", "text": "Sent."}]},
    })
    result = _connector().call_tool("send_email", {"to": "x"})
    assert mcp_client.result_text(result) == "Sent."


# ---------------------------------------------------------------------------
# MCP: the credential never leaks
# ---------------------------------------------------------------------------


TOKEN = "sk-live-TOPSECRET-0123456789"


def test_status_never_carries_the_token(monkeypatch):
    def handler(request):
        if _method_of(request) == "initialize":
            return {"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {"name": "s"}}}
        return {"jsonrpc": "2.0", "id": 2, "result": {"tools": []}}

    _install_transport(monkeypatch, handler)
    connector = _connector(url="http://host/mcp?access_token=" + TOKEN, token=TOKEN)
    payload = json.dumps(connector.status())
    assert TOKEN not in payload
    assert connector.status()["has_token"] is True


def test_an_error_that_echoes_the_token_is_redacted(monkeypatch):
    _install_transport(monkeypatch, lambda request: {
        "jsonrpc": "2.0", "id": 1,
        "error": {"code": -32000, "message": f"bad credential {TOKEN}"},
    })
    status = _connector(token=TOKEN).status()
    assert TOKEN not in json.dumps(status)


def test_public_config_masks_a_token_in_the_path(monkeypatch):
    public = mcp_client.MCPConfig(
        url=f"http://host/mcp/{TOKEN}", token=TOKEN).public()
    assert TOKEN not in json.dumps(public)


def test_parse_allowed_tools_is_order_preserving_and_empty_by_default():
    assert mcp_client.parse_allowed_tools("") == []
    assert mcp_client.parse_allowed_tools(" b , a ,, b ") == ["b", "a"]


# ---------------------------------------------------------------------------
# MCP: tool adapter
# ---------------------------------------------------------------------------


def test_adapter_registers_one_tool_per_allowlisted_name():
    connector = _connector(allowed=("send_email", "create_ticket"))
    specs = [spec for spec, _ in mcp_tools.specs_for(connector)]
    assert [s.name for s in specs] == ["mcp__test__send_email", "mcp__test__create_ticket"]
    # Nothing from a server's catalogue is registered implicitly.
    assert all(s.risk is RiskLevel.EXTERNAL for s in specs)
    assert all(s.requires_confirmation for s in specs)


def test_adapter_registers_nothing_when_unconfigured():
    assert mcp_tools.specs_for(_connector(url="")) == []


@pytest.mark.asyncio
async def test_adapter_handler_reports_an_mcp_failure_as_fail(monkeypatch):
    connector = _connector(allowed=("send_email",))

    def boom(name, args):
        raise mcp_client.MCPError("endpoint refused (HTTP 401).")

    connector.call_tool = boom
    monkeypatch.setattr(mcp_client, "active_connector", lambda: connector)
    _, handler = mcp_tools.specs_for(connector)[0]
    result = await handler(_Ctx())
    assert result.ok is False
    assert "401" in result.error


@pytest.mark.asyncio
async def test_adapter_handler_forwards_named_arguments(monkeypatch):
    connector = _connector(allowed=("send_email",))
    seen: Dict[str, Any] = {}

    def call(name, args):
        seen.update(name=name, args=args)
        return {"content": [{"type": "text", "text": "ok"}]}

    connector.call_tool = call
    monkeypatch.setattr(mcp_client, "active_connector", lambda: connector)
    _, handler = mcp_tools.specs_for(connector)[0]
    await handler(_Ctx(), to="me@example.com", subject="hi")
    assert seen["name"] == "send_email"
    assert seen["args"] == {"to": "me@example.com", "subject": "hi"}


def test_tool_name_is_namespaced():
    assert mcp_tools.tool_name("zapier", "send-email") == "mcp__zapier__send-email"
    assert mcp_tools.tool_name("a b", "x/y") == "mcp__a_b__x_y"


# ---------------------------------------------------------------------------
# Routing: only an explicit "Google" changes the destination
# ---------------------------------------------------------------------------


def _plan(text: str):
    from jarvis.core.planner import RulePlanner
    from jarvis.tools import build_default_registry

    plan = RulePlanner(build_default_registry()).parse(text)
    return plan.intent, [(a.tool, a.params) for a in (plan.actions or [])]


@pytest.mark.parametrize("text,tool,title", [
    ("add a task to Google Tasks buy milk", "add_google_task", "buy milk"),
    ("create a task in google tasks called Call the dentist",
     "add_google_task", "Call the dentist"),
    # "google" before "task" is the same instruction. It used to fall through
    # to the registry fallback, which asked "which did you mean?" between the
    # local list and Google -- a question this sentence already answered.
    ("add a google task to buy milk", "add_google_task", "buy milk"),
    ("create a google task to call the dentist", "add_google_task", "call the dentist"),
])
def test_a_google_task_request_routes_to_google(text, tool, title):
    intent, actions = _plan(text)
    assert intent == tool
    assert actions[0][0] == tool
    assert actions[0][1]["title"] == title

def test_a_google_task_phrasing_never_asks_which_list():
    """Naming Google settles the destination; the fallback must not re-open it."""
    intent, _ = _plan("add a google task to buy milk")
    assert intent != "capability_ambiguous"

def test_a_plain_task_request_still_goes_to_the_local_list():
    """The Google rule must not swallow the local task list."""
    intent, actions = _plan("add a task to buy milk")
    assert intent == "add_task"
    assert actions[0][0] == "add_task"


def test_listing_google_tasks_routes_to_google():
    intent, actions = _plan("show my google tasks")
    assert intent == "list_google_tasks"
    assert actions[0][0] == "list_google_tasks"


def test_completing_a_google_task_routes_to_google():
    intent, actions = _plan("mark buy milk done in Google Tasks")
    assert intent == "complete_google_task"
    assert actions[0][1]["task_ref"] == "buy milk"


def test_without_the_word_google_the_local_planner_is_unchanged():
    """The reason the trigger is "google" and nothing less.

    Two task lists that swap on a phrasing change would be worse than the gap
    these rules close, so an ordinary "add a task" must still be local.
    """
    intent, actions = _plan("add a task to buy milk")
    assert intent == "add_task"
    assert actions[0][0] == "add_task"

# ---------------------------------------------------------------------------
# The HTTP surface, live
# ---------------------------------------------------------------------------


def _api_client():
    from fastapi.testclient import TestClient

    from jarvis.api.app import app as APP

    return TestClient(APP, base_url="http://127.0.0.1:8765",
                      headers={"X-JARVIS-Token": __import__("os").environ["JARVIS_API_TOKEN"]})


def test_mcp_status_is_authenticated():
    from fastapi.testclient import TestClient

    from jarvis.api.app import app as APP

    resp = TestClient(APP, base_url="http://127.0.0.1:8765").get("/mcp/status")
    assert resp.status_code in (401, 403)


def test_mcp_status_reports_not_configured_without_a_url():
    body = _api_client().get("/mcp/status").json()
    assert body["connection"]["state"] == "not_configured"
    assert body["connection"]["tools"] == []
    assert body["callable_tools"] == []


def test_the_phone_surface_cannot_reach_mcp():
    """The connector's endpoint and catalogue are not a phone capability."""
    from jarvis.api.app import PHONE_ALLOWED
    from jarvis.companion.gateway import PHONE_ALLOWED as GATEWAY_ALLOWED

    assert "/mcp/status" not in PHONE_ALLOWED
    assert "/mcp/status" not in GATEWAY_ALLOWED


def test_every_status_branch_carries_the_same_keys():
    """A consumer must not KeyError on the state where nothing is callable.

    The live runbook read ``connection["callable"]`` and crashed on
    ``not_configured``, because only the connected branch supplied it. The
    unit tests had the same blind spot: they read a key that happened to exist
    in the branch they exercised. Asserting the shape across all three
    non-connected branches is what closes it.
    """
    shapes = [
        _connector(url="").status(),                       # not_configured
        _connector(url="http://127.0.0.1:9/mcp").status(probe=False),  # disconnected
    ]
    keys = set(shapes[0])
    for shape in shapes:
        assert set(shape) == keys, f"{shape['state']} dropped {keys - set(shape)}"
        assert shape["callable"] == []
        assert shape["tools"] == []
        assert shape["tool_count"] == 0


def test_a_read_only_calendar_refusal_is_not_reported_as_a_broken_token():
    """Creating on a read-only Google connection must say *why*, not "reconnect".

    Google is connected with ``calendar.readonly`` on purpose. `create_event`
    used to route that refusal through the read-failure path, which rendered
    "Google rejected the access — it was revoked or expired. Reconnect…" for an
    account that was working. This pins the honest message and the local
    alternative.
    """
    import asyncio

    from jarvis.tools.calendar import create_event

    class _ReadOnly:
        name = "google"
        def create(self, event):
            return {
                "ok": False,
                "provider": "google",
                "status": "auth_error",
                "read_only": True,
                "error": "This Google connection is read-only.",
            }

    class _Ctx:
        device_id = "laptop_main"
        settings = Settings()

    import jarvis.tools.calendar as cal
    original = cal.get_calendar_provider
    cal.get_calendar_provider = lambda settings=None: _ReadOnly()
    try:
        result = asyncio.run(create_event(_Ctx(), title="Standup",
                                          start="tomorrow at 9", duration_minutes=30))
    finally:
        cal.get_calendar_provider = original

    assert result.ok is False
    message = (result.error or "").lower()
    assert "read-only" in message
    assert "reconnect" not in message, message
    assert "revoked" not in message, message
    assert result.output.get("read_only") is True
    assert result.output.get("local_alternative") is True


# ---------------------------------------------------------------------------
# A 403 says which 403 it is
# ---------------------------------------------------------------------------

def test_a_disabled_tasks_api_is_reported_as_a_project_setting():
    """Google's SERVICE_DISABLED is not "the account lacks permission".

    Measured 2026-10-28: a token carrying the tasks scope was still refused,
    because tasks.googleapis.com was disabled in the OAuth project. The old
    sentence ("the account may lack the tasks permission") sends the reader to
    re-consent, which cannot fix it.
    """
    from jarvis.integrations.google_tasks import GoogleTasksClient

    body = json.dumps({"error": {
        "code": 403,
        "status": "PERMISSION_DENIED",
        "message": "Google Tasks API has not been used in project 726165035122 "
                   "before or it is disabled.",
        "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo",
                     "reason": "SERVICE_DISABLED"}],
    }}).encode()
    detail = GoogleTasksClient._http_detail(403, body)
    assert "not enabled" in detail.lower()
    assert "project" in detail.lower()
    assert "may lack the tasks permission" not in detail


def test_a_plain_403_still_names_the_permission():
    from jarvis.integrations.google_tasks import GoogleTasksClient

    body = json.dumps({"error": {"code": 403, "status": "PERMISSION_DENIED",
                                 "message": "Insufficient Permission"}}).encode()
    detail = GoogleTasksClient._http_detail(403, body)
    assert "may lack the tasks permission" in detail


def test_a_403_with_no_body_does_not_crash():
    from jarvis.integrations.google_tasks import GoogleTasksClient

    assert isinstance(GoogleTasksClient._http_detail(403, b""), str)
