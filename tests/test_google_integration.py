"""Google Calendar + Tasks integration.

The properties under test are the ones that have bitten this project before,
not the happy path alone:

* a saved credential is not a connection -- "connected" is only reported when a
  live call succeeded;
* a failed read is never an empty calendar, because an unreachable provider
  once told a person "nothing scheduled" and handed out a bookable slot;
* nothing secret is logged, returned, or stored in settings;
* an unconfigured integration says how to fix it rather than looking broken.

No test here contacts Google. The network is never touched: adapters are driven
with an injected auth object, and HTTP behaviour is exercised through the
``httpx`` client mock where a response shape matters.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Dict

import httpx
import pytest

from jarvis.config import Settings
from jarvis.integrations.calendar_provider import (
    CalendarEvent,
    ProviderResults,
    ProviderStatus,
)
from jarvis.integrations.google_auth import (
    ConnectionState,
    GoogleAuth,
    GoogleAuthError,
    describe_google,
    scopes_for,
)
from jarvis.integrations.google_calendar import GoogleCalendarProvider
from jarvis.integrations.google_tasks import GoogleTasksClient


# ---------------------------------------------------------------------------
# HTTP fake
# ---------------------------------------------------------------------------


def _install(provider, handler):
    """Route a provider's HTTP through ``handler``, which returns (status, bytes).

    The adapters do their network read in ``_request``, which is where this
    project's ``http_read`` total deadline is applied. Patching that exercises
    the real parsing and status handling while touching no socket.
    """
    import json as _json

    def fake(request):
        status, payload = handler(request)
        if isinstance(payload, (dict, list)):
            payload = _json.dumps(payload).encode()
        return status, payload

    provider._request = fake
    return provider


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeAuth(GoogleAuth):
    """An auth object with a chosen state, and no network."""

    def __init__(self, settings: Settings, *, configured: bool = True,
                 token: str = "stored-refresh-token",
                 access: str = "access-token",
                 raises: Exception | None = None) -> None:
        super().__init__(settings)
        self._configured = configured
        self._token = token
        self._access = access
        self._raises = raises
        self.refreshes = 0

    @property
    def configured(self) -> bool:
        return self._configured

    def stored_refresh_token(self):
        return self._token

    def access_token(self) -> str:
        if self._raises:
            raise self._raises
        self.refreshes += 1
        return self._access

    def status(self, verify_with=None):
        from jarvis.integrations.google_auth import ConnectionStatus

        # Only the *verification* step is faked. The not-configured and
        # no-token checks are the production ones, because those are the
        # behaviours under test -- a fake that answers CONNECTED regardless
        # would pass while the real guard was broken.
        if not self.configured:
            return ConnectionStatus(ConnectionState.NOT_CONFIGURED,
                                    detail="Google is not configured. Set JARVIS_GOOGLE_CLIENT_ID.",
                                    configured=False, has_token=bool(self._token))
        if not self._token:
            return ConnectionStatus(ConnectionState.DISCONNECTED,
                                    detail="No Google account is connected.",
                                    configured=True, has_token=False)
        if self._raises:
            return ConnectionStatus(self._raises.state, detail=str(self._raises),
                                    configured=True, has_token=True)
        return ConnectionStatus(ConnectionState.CONNECTED, detail="Connected to Google.",
                                account="someone@example.com",
                                configured=True, has_token=True)


def _settings(**kw) -> Settings:
    base = dict(google_client_id="client-id.apps.googleusercontent.com")
    base.update(kw)
    return Settings(**base)


# ---------------------------------------------------------------------------
# Configuration and scopes
# ---------------------------------------------------------------------------


def test_unconfigured_says_how_to_fix_it():
    setup = describe_google(Settings(google_client_id=""))
    assert setup["configured"] is False
    # The list must name the actual steps, not merely "not configured".
    joined = " ".join(setup["setup"]).lower()
    assert "calendar api" in joined and "tasks api" in joined
    assert "desktop" in joined


def test_scopes_are_least_privilege():
    settings = _settings()
    scopes = scopes_for(settings)
    # Calendar is read-only: no write scope until event editing exists.
    assert any("calendar.readonly" in s for s in scopes)
    assert not any("auth/calendar\"" in s or s.endswith("/calendar") for s in scopes)
    assert any(s.endswith("/tasks") for s in scopes)


def test_disabling_a_service_drops_its_scope():
    scopes = scopes_for(_settings(google_tasks_enabled=False))
    assert not any(s.endswith("/tasks") for s in scopes)


# ---------------------------------------------------------------------------
# Connection lifecycle
# ---------------------------------------------------------------------------


def test_begin_refuses_without_a_client_id():
    auth = GoogleAuth(Settings(google_client_id=""))
    with pytest.raises(GoogleAuthError) as excinfo:
        auth.begin("http://127.0.0.1:8765/google/callback")
    assert "JARVIS_GOOGLE_CLIENT_ID" in str(excinfo.value)


def test_begin_uses_pkce_and_offline_access():
    import urllib.parse

    auth = GoogleAuth(_settings())
    result = auth.begin("http://127.0.0.1:8765/google/callback")
    query = urllib.parse.parse_qs(urllib.parse.urlparse(result["url"]).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"], "PKCE challenge must be present"
    # offline access is what yields a refresh token; without it the connection
    # silently expires an hour later.
    assert query["access_type"] == ["offline"]


def test_callback_rejects_a_state_it_did_not_issue():
    auth = GoogleAuth(_settings())
    auth.begin("http://127.0.0.1:8765/google/callback")
    with pytest.raises(GoogleAuthError) as excinfo:
        auth.complete("code", "not-the-state")
    # Whatever the wording, the state we did not issue cannot complete, and
    # nothing was connected.
    assert "nothing was connected" in str(excinfo.value).lower()
    assert excinfo.value.state is ConnectionState.DISCONNECTED


def test_callback_without_a_pending_authorization_fails_cleanly():
    # The pending authorization is process-wide by design, so an earlier test
    # can leave one behind. Clear it: the property here is "nothing pending",
    # not "nothing pending as a side effect of test order".
    GoogleAuth._pending = None
    with pytest.raises(GoogleAuthError) as excinfo:
        GoogleAuth(_settings()).complete("code", "state")
    assert "no authorization in progress" in str(excinfo.value).lower()


def test_status_distinguishes_saved_from_connected():
    """A stored token that Google refuses is EXPIRED, not CONNECTED."""
    failing = GoogleAuthError("Google rejected the saved access.", ConnectionState.EXPIRED)
    auth = FakeAuth(_settings(), raises=failing)
    status = auth.status()
    assert status.state is ConnectionState.EXPIRED
    assert status.usable is False
    assert status.has_token is True          # saved...
    assert status.state is not ConnectionState.CONNECTED   # ...but not working


def test_status_not_configured_even_with_a_stored_token():
    auth = FakeAuth(Settings(google_client_id=""), configured=False)
    status = auth.status()
    assert status.state is ConnectionState.NOT_CONFIGURED
    assert status.usable is False


def test_status_dict_never_carries_a_token():
    auth = FakeAuth(_settings(), access="SUPER-SECRET-ACCESS", token="SUPER-SECRET-REFRESH")
    payload = json.dumps(auth.status().as_dict())
    assert "SUPER-SECRET" not in payload


# ---------------------------------------------------------------------------
# Calendar adapter
# ---------------------------------------------------------------------------


def _calendar_with(handler, monkeypatch=None) -> GoogleCalendarProvider:
    """A provider whose HTTP goes through ``handler`` instead of the network.

    The adapters read through ``jarvis.net.http_read``, which is where this
    project enforces a total deadline on every network read. Patching that --
    rather than httpx -- exercises the same path production uses.
    """
    return _install(GoogleCalendarProvider(_settings(), FakeAuth(_settings())), handler)


def test_calendar_read_maps_google_events():
    def handler(request):
        assert "calendars/primary/events" in str(request.full_url)
        return 200, {"items": [
            {
                "id": "evt-1",
                "summary": "Design review",
                "start": {"dateTime": "2026-11-02T14:00:00+00:00"},
                "end": {"dateTime": "2026-11-02T15:00:00+00:00"},
                "location": "Room 2",
            },
            {
                "id": "evt-2",
                "summary": "All day thing",
                "start": {"date": "2026-11-03"},
                "end": {"date": "2026-11-04"},
            },
        ]}

    now = datetime.now().astimezone()
    events = _calendar_with(handler).list_events(now, now + timedelta(days=2))
    assert events.status is ProviderStatus.HAS_RESULTS
    assert len(events) == 2
    assert events[0].title == "Design review"
    assert events[0].as_dict()["duration_minutes"] == 60
    assert events[1].all_day is True
    # An all-day event still spans a real day rather than zero minutes.
    assert events[1].as_dict()["duration_minutes"] == 1440


def test_calendar_empty_is_a_real_empty_not_a_failure():
    events = _calendar_with(lambda r: (200, {"items": []})) \
        .list_events(datetime.now().astimezone(),
                     datetime.now().astimezone() + timedelta(days=1))
    assert events.status is ProviderStatus.REAL_EMPTY
    assert events.status.read_succeeded is True
    assert len(events) == 0


def test_calendar_auth_failure_is_not_an_empty_day():
    events = _calendar_with(lambda r: (401, {"error": {"message": "bad"}})) \
        .list_events(datetime.now().astimezone(),
                     datetime.now().astimezone() + timedelta(days=1))
    assert events.status is ProviderStatus.AUTH_ERROR
    assert events.status.read_succeeded is False
    assert len(events) == 0
    assert "Reconnect" in events.detail


def test_calendar_permission_denied_is_reported_as_such():
    events = _calendar_with(lambda r: (403, {"error": {"message": "no"}})) \
        .list_events(datetime.now().astimezone(),
                     datetime.now().astimezone() + timedelta(days=1))
    assert events.status.read_succeeded is False
    assert "refused access" in events.detail


def test_calendar_malformed_body_is_a_failure():
    events = _calendar_with(lambda r: (200, b"<html>nope</html>")) \
        .list_events(datetime.now().astimezone(),
                     datetime.now().astimezone() + timedelta(days=1))
    assert events.status is ProviderStatus.PROVIDER_ERROR
    assert events.status.read_succeeded is False


def test_calendar_write_is_refused_honestly():
    """The connection is read-only, so a write must say so, not claim success."""
    provider = _calendar_with(lambda r: (200, {}))
    for result in (provider.create(None), provider.update("x", {}), provider.cancel("x")):
        assert result["ok"] is False
        assert result["read_only"] is True
        assert "read-only" in result["error"]


def test_calendar_unconfigured_read_reports_the_setup_problem():
    provider = GoogleCalendarProvider(Settings(google_client_id=""))
    now = datetime.now().astimezone()
    events = provider.list_events(now, now + timedelta(days=1))
    assert events.status is ProviderStatus.NOT_CONFIGURED
    assert "JARVIS_GOOGLE_CLIENT_ID" in events.detail


# ---------------------------------------------------------------------------
# Tasks adapter
# ---------------------------------------------------------------------------


def _tasks_with(handler) -> GoogleTasksClient:
    return _install(GoogleTasksClient(_settings(), FakeAuth(_settings())), handler)


def test_tasks_read_maps_google_tasks():
    client = _tasks_with(lambda r: (200, {"items": [
        {"id": "t1", "title": "Book the demo", "status": "needsAction",
         "due": "2026-11-01T00:00:00.000Z"},
        {"id": "t2", "title": "Done thing", "status": "completed"},
    ]}))
    tasks = client.tasks()
    assert tasks.status is ProviderStatus.HAS_RESULTS
    assert [t.title for t in tasks] == ["Book the demo", "Done thing"]
    assert tasks[0].due is not None
    assert tasks[1].done is True


def test_tasks_empty_list_is_real_empty():
    tasks = _tasks_with(lambda r: (200, {"items": []})).tasks()
    assert tasks.status is ProviderStatus.REAL_EMPTY
    assert tasks.status.read_succeeded is True


def test_tasks_auth_failure_is_not_an_empty_list():
    tasks = _tasks_with(lambda r: (401, {})).tasks()
    assert tasks.status is ProviderStatus.AUTH_ERROR
    assert tasks.status.read_succeeded is False


def test_task_create_sends_title_and_notes_with_provenance():
    seen: Dict[str, Any] = {}

    def handler(request):
        seen.update(json.loads(request.data or b"{}"))
        return 200, {"id": "new", "title": seen.get("title", ""),
                     "status": "needsAction"}

    result = _tasks_with(handler).create("Follow up", source="an email from Priya")
    assert result["ok"] is True
    assert seen["title"] == "Follow up"
    # Google has no provenance field, so the source is visible in the notes.
    assert "Priya" in seen["notes"]


def test_task_create_requires_a_title():
    result = _tasks_with(lambda r: (200, {})).create("   ")
    assert result["ok"] is False
    assert "title" in result["error"].lower()


def test_task_complete_only_claims_done_when_google_says_so():
    """A 200 that did not actually complete the task is not a success."""
    result = _tasks_with(
        lambda r: (200, {"id": "t1", "title": "x", "status": "needsAction"})
    ).complete("t1")
    assert result["ok"] is False
    assert result["status"] == "unverified"


def test_task_complete_succeeds_when_google_reports_completed():
    result = _tasks_with(
        lambda r: (200, {"id": "t1", "title": "x", "status": "completed"})
    ).complete("t1")
    assert result["ok"] is True
    assert result["task"]["done"] is True


# ---------------------------------------------------------------------------
# The planner must not change behaviour when Google is absent
# ---------------------------------------------------------------------------


def test_calendar_selector_falls_back_to_local():
    from jarvis.integrations.calendar_provider import (
        LocalCalendarProvider,
        get_calendar_provider,
    )

    provider = get_calendar_provider(Settings(google_client_id="", calendar_url=""))
    assert isinstance(provider, LocalCalendarProvider), \
        "with Google unconfigured the local calendar must still be the provider"

# ---------------------------------------------------------------------------
# The real transport: HTTP errors must be answers, not "unreachable"
#
# Every test above replaces `_request`, which is exactly how a defect slipped
# through: `http_read` wraps `urlopen`, and `urlopen` RAISES HTTPError on any
# 4xx/5xx. Without these tests a revoked token was reported as "Could not reach
# Google" -- a network problem -- instead of the credential problem it is.
# ---------------------------------------------------------------------------


class _StatusServer:
    """A local HTTP server that answers every request with a chosen status."""

    def __init__(self, status: int, body: bytes) -> None:
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_POST = do_GET

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def test_calendar_401_over_real_transport_is_an_auth_error():
    """The regression: a 401 must not be reported as an unreachable server."""
    server = _StatusServer(401, b'{"error":{"message":"Invalid Credentials"}}')
    try:
        provider = GoogleCalendarProvider(_settings(), FakeAuth(_settings()))
        import jarvis.integrations.google_calendar as mod
        original = mod.CALENDAR_API
        mod.CALENDAR_API = server.url
        try:
            now = datetime.now().astimezone()
            events = provider.list_events(now, now + timedelta(days=1))
        finally:
            mod.CALENDAR_API = original
    finally:
        server.close()
    assert events.status is ProviderStatus.AUTH_ERROR, (
        f"a 401 came back as {events.status.value}; it must be an auth error")
    assert events.status.read_succeeded is False
    assert "Reconnect" in events.detail
    assert "Could not reach" not in events.detail


def test_tasks_401_over_real_transport_is_an_auth_error():
    server = _StatusServer(401, b'{"error":{"message":"Invalid Credentials"}}')
    try:
        client = GoogleTasksClient(_settings(), FakeAuth(_settings()))
        import jarvis.integrations.google_tasks as mod
        original = mod.TASKS_API
        mod.TASKS_API = server.url
        try:
            tasks = client.tasks()
        finally:
            mod.TASKS_API = original
    finally:
        server.close()
    assert tasks.status is ProviderStatus.AUTH_ERROR, (
        f"a 401 came back as {tasks.status.value}; it must be an auth error")
    assert "Could not reach" not in tasks.detail


def test_calendar_403_over_real_transport_is_reported_as_refusal():
    server = _StatusServer(403, b'{"error":{"message":"Forbidden"}}')
    try:
        provider = GoogleCalendarProvider(_settings(), FakeAuth(_settings()))
        import jarvis.integrations.google_calendar as mod
        original = mod.CALENDAR_API
        mod.CALENDAR_API = server.url
        try:
            now = datetime.now().astimezone()
            events = provider.list_events(now, now + timedelta(days=1))
        finally:
            mod.CALENDAR_API = original
    finally:
        server.close()
    assert events.status.read_succeeded is False
    assert "refused access" in events.detail


def test_unreachable_host_is_not_reported_as_a_refusal():
    """The opposite direction: a dead port must read as a network problem."""
    import socket
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    dead_port = sock.getsockname()[1]
    sock.close()                                   # nothing is listening now
    provider = GoogleCalendarProvider(_settings(), FakeAuth(_settings()))
    import jarvis.integrations.google_calendar as mod
    original = mod.CALENDAR_API
    mod.CALENDAR_API = f"http://127.0.0.1:{dead_port}"
    try:
        now = datetime.now().astimezone()
        events = provider.list_events(now, now + timedelta(days=1))
    finally:
        mod.CALENDAR_API = original
    assert events.status.read_succeeded is False
    assert events.status is not ProviderStatus.AUTH_ERROR
    assert "Could not reach" in events.detail


# ---------------------------------------------------------------------------
# The planner consumes Google, and still says when it could not
# ---------------------------------------------------------------------------


class _FakeConnectedGoogle:
    """Stands in for GoogleCalendarProvider once the selector has chosen it."""

    name = "google"
    is_remote = True

    def __init__(self, results):
        self._results = results

    def available(self) -> bool:
        return True

    def list_events(self, start, end, limit=50):
        return self._results


def _google_events(*titles):
    now = datetime.now().astimezone().replace(minute=0, second=0, microsecond=0)
    return ProviderResults(
        [CalendarEvent(uid=f"g{i}", title=t,
                       start=now.replace(hour=9 + i),
                       end=now.replace(hour=10 + i))
         for i, t in enumerate(titles)],
        ProviderStatus.HAS_RESULTS,
    )


def _run_start_day(monkeypatch, provider):
    """Run the real planner tool with the selector pinned to ``provider``."""
    import asyncio

    import jarvis.tools.planner as planner_mod
    import jarvis.integrations.calendar_provider as cp_mod
    from jarvis.tools.base import ToolContext

    original = cp_mod.get_calendar_provider
    cp_mod.get_calendar_provider = lambda *a, **k: provider
    try:
        ctx = ToolContext(settings=_settings())
        return asyncio.run(planner_mod.start_day(ctx))
    finally:
        cp_mod.get_calendar_provider = original


def test_the_planner_uses_the_connected_google_calendar():
    """The integration is only real if the planner reads from it."""
    result = _run_start_day(None, _FakeConnectedGoogle(_google_events("Google-only standup")))
    message = result.message or ""
    assert "Google-only standup" in message
    assert "1 event(s) today" in message
    assert "could not be read" not in message.lower()


def test_the_planner_does_not_claim_an_empty_day_when_google_fails():
    """A failed Google read must read as a failure, not "nothing scheduled"."""
    failed = ProviderResults(
        [], ProviderStatus.AUTH_ERROR,
        detail="Google rejected the access token. Reconnect the account in Settings.")
    result = _run_start_day(None, _FakeConnectedGoogle(failed))
    message = result.message or ""
    assert "could not be read" in message.lower(), message[:200]
    assert "nothing scheduled" not in message.lower()


def test_the_planner_still_reads_the_local_calendar_without_google():
    """Disconnecting Google must not take local reminders away."""
    from jarvis.tools.calendar import create_event  # noqa: F401  (import check)
    import asyncio

    import jarvis.tools.planner as planner_mod
    from jarvis.tools.base import ToolContext
    from jarvis.integrations.calendar_provider import get_calendar_provider

    provider = get_calendar_provider(_settings(google_client_id=""))
    assert provider.name != "google"
    ctx = ToolContext(settings=_settings(google_client_id=""))
    result = asyncio.run(planner_mod.start_day(ctx))
    # Whatever the local calendar holds, the tool must have produced an agenda
    # rather than an error.
    assert result.ok is True


# ---------------------------------------------------------------------------
# Availability: a failed Google read must never yield a bookable slot
#
# This is the sharpest rule in the integration brief. "The first free slot is
# 08:00" from a calendar that could not be read is worse than an error: the
# person books over a real meeting and only finds out later.
# ---------------------------------------------------------------------------


def _run_find_free_time(provider):
    """Run the real find_free_time tool with the calendar pinned to ``provider``.

    ``jarvis/tools/calendar.py`` imports ``get_calendar_provider`` by name, so
    the patch has to land on *that* module's reference -- patching the source
    module silently does nothing, which is exactly how a probe can "prove" a
    bug that is not there.
    """
    import asyncio

    import jarvis.tools.calendar as cal
    from jarvis.tools.base import ToolContext

    original = cal.get_calendar_provider
    cal.get_calendar_provider = lambda *a, **k: provider
    try:
        ctx = ToolContext(settings=_settings())
        return asyncio.run(cal.find_free_time(ctx, date="tomorrow", duration_minutes=60))
    finally:
        cal.get_calendar_provider = original


@pytest.mark.parametrize("status", [
    ProviderStatus.AUTH_ERROR,
    ProviderStatus.NETWORK_ERROR,
    ProviderStatus.UNAVAILABLE,
    ProviderStatus.PROVIDER_ERROR,
    ProviderStatus.NOT_CONFIGURED,
])
def test_a_failed_google_read_never_offers_a_free_slot(status):
    result = _run_find_free_time(
        _FakeConnectedGoogle(ProviderResults([], status, detail="Google refused the read.")))
    assert result.ok is False, "a free slot was offered from an unreadable calendar"
    assert (result.output or {}).get("slot") is None
    # And it says why, rather than refusing silently.
    assert result.error
    assert "not read" in result.error.lower() or "could not" in result.error.lower()
    assert "not an empty day" in result.error.lower() or "nothing was read" in result.error.lower()


def test_an_empty_google_calendar_does_offer_a_slot():
    """The other direction: a successful empty read is a real clear day."""
    result = _run_find_free_time(
        _FakeConnectedGoogle(ProviderResults([], ProviderStatus.REAL_EMPTY)))
    assert result.ok is True
    assert (result.output or {}).get("slot") is not None


def test_the_google_auth_failure_message_points_at_the_google_connection():
    """A CalDAV fix ("check your username and password") is wrong advice now."""
    result = _run_find_free_time(
        _FakeConnectedGoogle(ProviderResults(
            [], ProviderStatus.AUTH_ERROR, detail="Google rejected the access token.")))
    assert "Google" in result.error, result.error


# ---------------------------------------------------------------------------
# Google's own error text is untrusted input
#
# An API error body can echo the request, credential included. That text becomes
# our `detail`, which is logged and returned to a client -- so it is redacted
# before it travels. Measured: a 500 carrying "ya29.SECRET..." arrived intact
# before this was applied.
# ---------------------------------------------------------------------------

_ECHOED = b'{"error":{"message":"backend saw token ya29.SECRET_ABCDEF123"}}'


def test_tasks_error_detail_does_not_carry_an_echoed_token():
    client = GoogleTasksClient(_settings(), FakeAuth(_settings()))
    client._request = lambda request: (500, _ECHOED)
    result = client.tasks()
    assert "ya29.SECRET_ABCDEF123" not in (result.detail or ""), result.detail
    assert "redacted" in result.detail.lower()


def test_calendar_error_detail_does_not_carry_an_echoed_token():
    provider = GoogleCalendarProvider(_settings(), FakeAuth(_settings()))
    provider._request = lambda request: (500, _ECHOED)
    now = datetime.now().astimezone()
    result = provider.list_events(now, now + timedelta(days=1))
    assert "ya29.SECRET_ABCDEF123" not in (result.detail or ""), result.detail
    assert "redacted" in result.detail.lower()


def test_error_detail_still_explains_the_problem():
    """Redaction must not swallow the useful part of the message."""
    client = GoogleTasksClient(_settings(), FakeAuth(_settings()))
    client._request = lambda request: (
        500, b'{"error":{"message":"Backend Error quota exceeded"}}')
    detail = client.tasks().detail
    assert "quota exceeded" in detail.lower()


# ---------------------------------------------------------------------------
# Credential storage fails closed
#
# The refresh token is a long-lived credential. If the keychain cannot hold it,
# the connection must fail -- writing it to a settings file or a plain file
# would be worse than not connecting. Measured: `KeychainStorage`'s no-keyring
# fallback returns False and stores nothing, which is the right shape; these
# tests hold the OAuth layer to it.
# ---------------------------------------------------------------------------


def test_a_token_that_cannot_be_stored_is_not_reported_as_connected(monkeypatch):
    import jarvis.security.providers as providers

    monkeypatch.setattr(providers.KeychainStorage, "store",
                        staticmethod(lambda *a, **k: False))
    monkeypatch.setattr(providers.KeychainStorage, "retrieve",
                        staticmethod(lambda *a, **k: None))
    auth = GoogleAuth(_settings())
    assert auth.store_refresh_token("1//refresh") is False
    assert auth.stored_refresh_token() is None
    status = auth.status()
    assert status.usable is False
    assert status.state is ConnectionState.DISCONNECTED


def test_a_failed_store_says_why_and_does_not_claim_success(monkeypatch):
    """Drive the real `complete()` so the post-exchange branch is exercised.

    Google grants the request but the keychain refuses the token: the person
    must get an error naming the keychain, not a screen that looks connected.
    """
    import jarvis.integrations.google_auth as auth_mod
    import jarvis.security.providers as providers

    # The exchange itself succeeds and returns a refresh token...
    monkeypatch.setattr(GoogleAuth, "_post_token", lambda self, payload: {
        "access_token": "ya29.access", "refresh_token": "1//refresh",
        "expires_in": 3600,
    })
    monkeypatch.setattr(GoogleAuth, "_verify", lambda self, token: ("a@b.c", []))
    # ...but the keychain will not take it.
    monkeypatch.setattr(providers.KeychainStorage, "store",
                        staticmethod(lambda *a, **k: False))

    auth = GoogleAuth(_settings())
    pending = auth.begin("http://127.0.0.1:8765/google/callback")
    with pytest.raises(GoogleAuthError) as excinfo:
        auth.complete("code", pending["state"])
    message = str(excinfo.value).lower()
    assert "keychain" in message
    assert excinfo.value.state is ConnectionState.ERROR


def test_the_keychain_fallback_never_writes_to_a_file():
    """The no-keyring backend must fail closed, not fall back to disk."""
    import inspect

    import jarvis.security.providers as providers

    source = inspect.getsource(providers)
    fallback_start = source.index("Fallback for systems without keyring")
    fallback = source[fallback_start:fallback_start + 600]
    for forbidden in ("open(", "write_text", "write_bytes", "json.dump"):
        assert forbidden not in fallback, (
            f"the keychain fallback appears to write to a file ({forbidden})")


def test_tasks_follows_pagination_instead_of_truncating_at_100():
    """A list longer than one Google page must be read to the end."""
    pages = {
        "": {"items": [{"id": f"a{i}", "title": f"Task {i}", "status": "needsAction"}
                       for i in range(100)],
             "nextPageToken": "page2"},
        "page2": {"items": [{"id": "b1", "title": "Second page task",
                             "status": "needsAction"}],
                  "nextPageToken": ""},
    }
    seen_tokens = []

    def handler(request):
        from urllib.parse import parse_qs, urlparse
        token = parse_qs(urlparse(request.full_url).query).get("pageToken", [""])[0]
        seen_tokens.append(token)
        return 200, pages[token]

    client = _tasks_with(handler)
    # Ask for more than one page's worth.
    tasks = client.tasks(limit=200)
    assert len(tasks) == 101, f"only {len(tasks)} tasks returned"
    assert any(t.title == "Second page task" for t in tasks)
    assert seen_tokens == ["", "page2"], seen_tokens


def test_tasks_stops_at_the_requested_limit():
    """The walk must not fetch more than asked for."""
    calls = []

    def handler(request):
        calls.append(1)
        return 200, {"items": [{"id": "x", "title": "T", "status": "needsAction"}],
                     "nextPageToken": "more"}

    client = _tasks_with(handler)
    tasks = client.tasks(limit=1)
    assert len(tasks) == 1
    assert len(calls) == 1, "fetched again after the limit was already reached"


def test_google_write_failures_have_distinct_statuses():
    """A caller must be able to tell "not set up" from "Google failed".

    A blanket 502 said "the upstream failed" for a problem that is the
    operator's own configuration, which sends them looking in the wrong place.
    """
    from jarvis.api.app import _google_write_status

    assert _google_write_status({"ok": False, "status": "not_configured"}) == 409
    assert _google_write_status({"ok": False, "status": "auth_error"}) == 401
    # No account connected is also "connect one", not "the upstream failed".
    assert _google_write_status({"ok": False, "status": "unavailable"}) == 401
    assert _google_write_status({"ok": False, "status": "provider_error"}) == 502
    assert _google_write_status({"ok": False}) == 502


# ---------------------------------------------------------------------------
# The authorization must survive between the two HTTP requests
#
# `begin` runs in one request and `complete` in another, and the routes build a
# fresh GoogleAuth for each. When the pending authorization was an *instance*
# attribute it was always gone by the callback, so every real connect failed
# with "There is no authorization in progress" -- while every single-instance
# test passed. These tests deliberately use two objects.
# ---------------------------------------------------------------------------


def test_pending_authorization_survives_a_second_instance():
    starter = GoogleAuth(_settings())
    pending = starter.begin("http://127.0.0.1:8765/google/callback")
    # A different object, as the callback route creates.
    finisher = GoogleAuth(_settings())
    with pytest.raises(GoogleAuthError) as excinfo:
        finisher.complete("code", pending["state"])
    message = str(excinfo.value).lower()
    assert "no authorization in progress" not in message, (
        "the pending authorization was lost between requests: " + message)


def test_state_mismatch_still_binds_across_instances():
    """Sharing the pending value must not weaken the state check."""
    starter = GoogleAuth(_settings())
    starter.begin("http://127.0.0.1:8765/google/callback")
    finisher = GoogleAuth(_settings())
    with pytest.raises(GoogleAuthError) as excinfo:
        finisher.complete("code", "forged-state")
    assert "nothing was connected" in str(excinfo.value).lower()
    assert excinfo.value.state is ConnectionState.DISCONNECTED


def test_the_pending_authorization_is_single_use_across_instances():
    """After a callback, the same state cannot be replayed by another object."""
    starter = GoogleAuth(_settings())
    pending = starter.begin("http://127.0.0.1:8765/google/callback")
    # Burn it (the exchange fails without a real client, but the state is
    # consumed either way -- that is the property under test).
    try:
        GoogleAuth(_settings()).complete("code", pending["state"])
    except GoogleAuthError:
        pass
    with pytest.raises(GoogleAuthError) as excinfo:
        GoogleAuth(_settings()).complete("code", pending["state"])
    # The authorization is gone: either the slot is empty or a different state
    # now owns it. What matters is that this one cannot be redeemed again.
    assert excinfo.value.state is ConnectionState.DISCONNECTED
    message = str(excinfo.value).lower()
    assert ("nothing was connected" in message
            or "no authorization in progress" in message), message


# ---------------------------------------------------------------------------
# Two attempts, one pending slot
#
# Only the newest authorization is held. That is fine, but a stale tab's
# callback must not destroy the newer attempt the person is about to finish --
# and a forged state must still be refused.
# ---------------------------------------------------------------------------


def test_a_stale_tab_does_not_cancel_a_newer_attempt():
    settings = _settings()
    first = GoogleAuth(settings).begin("http://127.0.0.1:8765/google/callback")
    second = GoogleAuth(settings).begin("http://127.0.0.1:8765/google/callback")
    assert first["state"] != second["state"]

    with pytest.raises(GoogleAuthError) as excinfo:
        GoogleAuth(settings).complete("code", first["state"])
    # Refused, in words that name the real situation.
    assert "earlier connection attempt" in str(excinfo.value).lower()

    # The newer attempt must survive: the person is still in that tab.
    assert GoogleAuth._pending is not None, "the newer attempt was cancelled"
    assert GoogleAuth._pending["state"] == second["state"]


def test_a_forged_state_is_still_refused():
    """Whatever the wording, a state we did not issue must not complete."""
    settings = _settings()
    GoogleAuth(settings).begin("http://127.0.0.1:8765/google/callback")
    with pytest.raises(GoogleAuthError) as excinfo:
        GoogleAuth(settings).complete("code", "forged")
    assert excinfo.value.state is ConnectionState.DISCONNECTED
    assert "nothing was connected" in str(excinfo.value).lower()

# ---------------------------------------------------------------------------
# Concurrency: the pending authorization is shared *and* now reachable from
# several threads
#
# The Google routes run their blocking exchanges on worker threads (they used
# to block the event loop), so `begin` and `complete` can genuinely overlap.
# These tests drive real threads; each one fails against the unlocked version.
# ---------------------------------------------------------------------------

def _fresh_pending():
    GoogleAuth._pending = None

def test_two_simultaneous_completions_redeem_the_state_only_once():
    """A double-submitted callback must not exchange the same code twice.

    The race window is between reading ``_pending`` and consuming it. It is
    widened deterministically here by making the state comparison rendezvous:
    under the lock only one thread can ever reach it, so the barrier times out
    and one exchange happens; without the lock both meet at the barrier and
    both redeem the code.
    """
    import threading
    import time as _time

    _fresh_pending()
    settings = _settings()
    pending = GoogleAuth(settings).begin("http://127.0.0.1:8765/google/callback")

    exchanges: list = []
    rendezvous = threading.Barrier(2, timeout=0.4)

    def meeting_compare(left, right):
        try:
            rendezvous.wait()
        except threading.BrokenBarrierError:
            pass                                # the other thread never arrived
        return _real_compare(left, right)

    _real_compare = __import__("secrets").compare_digest
    original_post = GoogleAuth._post_token
    original_verify = GoogleAuth._verify

    def fake_post(self, payload):
        exchanges.append(payload["code"])
        return {"access_token": "a", "refresh_token": "1//r", "expires_in": 3600}

    import jarvis.integrations.google_auth as auth_mod
    original_compare = auth_mod.secrets.compare_digest
    auth_mod.secrets.compare_digest = meeting_compare
    GoogleAuth._post_token = fake_post
    GoogleAuth._verify = lambda self, token: ("a@b.c", [])
    started = threading.Barrier(2)
    try:
        outcomes: list = []

        def run():
            started.wait()
            try:
                GoogleAuth(settings).complete("code", pending["state"])
                outcomes.append("connected")
            except GoogleAuthError as exc:
                outcomes.append(str(exc).lower())

        threads = [threading.Thread(target=run) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        auth_mod.secrets.compare_digest = original_compare
        GoogleAuth._post_token = original_post
        GoogleAuth._verify = original_verify
        _fresh_pending()

    assert len(exchanges) == 1, f"the code was exchanged {len(exchanges)} times"
    assert sum(o == "connected" for o in outcomes) == 1, outcomes
    assert any("no authorization in progress" in o for o in outcomes if o != "connected")


def test_a_begin_during_a_complete_is_not_cancelled():
    """A newer authorization started mid-exchange must survive.

    The old code consumed by assigning ``_pending = None`` after validating,
    so a ``begin`` that landed in that window was wiped -- the person's newer
    tab would then fail with "no authorization in progress". The consume is
    now inside the same lock as the check, so ``begin`` either lands before
    the check (and the old callback is refused) or after the consume (and the
    new attempt survives). This drives the second ordering.
    """
    import threading

    _fresh_pending()
    settings = _settings()
    old = GoogleAuth(settings).begin("http://127.0.0.1:8765/google/callback")

    _real_compare = __import__("secrets").compare_digest
    begun: dict = {}
    racing: list = []

    def compare_with_racing_begin(left, right):
        # Start a newer attempt while this completion is between its state
        # check and its consume. Under the lock it blocks; without the lock it
        # lands and is then wiped by the consume.
        def start_new():
            begun.update(GoogleAuth(settings).begin("http://127.0.0.1:8765/google/callback"))

        thread = threading.Thread(target=start_new)
        racing.append(thread)
        thread.start()
        thread.join(timeout=0.3)               # must NOT complete under the lock
        return _real_compare(left, right)

    original_post = GoogleAuth._post_token
    original_verify = GoogleAuth._verify
    import jarvis.integrations.google_auth as auth_mod
    original_compare = auth_mod.secrets.compare_digest
    auth_mod.secrets.compare_digest = compare_with_racing_begin
    GoogleAuth._post_token = lambda self, payload: {
        "access_token": "a", "refresh_token": "1//r", "expires_in": 3600}
    GoogleAuth._verify = lambda self, token: ("a@b.c", [])
    try:
        GoogleAuth(settings).complete("code", old["state"])
    finally:
        for thread in racing:
            thread.join(timeout=2)             # let the blocked begin finish
        auth_mod.secrets.compare_digest = original_compare
        GoogleAuth._post_token = original_post
        GoogleAuth._verify = original_verify
        pending_after = GoogleAuth._pending
        _fresh_pending()

    assert begun.get("state"), "the newer begin did not run at all"
    assert pending_after is not None, (
        "the complete wiped an authorization that started while it was in flight")
    assert pending_after["state"] == begun["state"]


# ---------------------------------------------------------------------------
# The access-token cache is process-wide, like the pending state
# ---------------------------------------------------------------------------

def test_a_second_instance_reuses_the_cached_access_token(monkeypatch):
    """The routes build a fresh GoogleAuth per request; the cache must survive.

    An instance cache meant every read refreshed, adding a token round trip to
    every calendar/tasks call.
    """
    import jarvis.integrations.google_auth as auth_mod

    GoogleAuth._cached_token = None
    GoogleAuth._cached_expiry = 0.0
    calls: list = []

    def fake_post(self, payload):
        calls.append(1)
        return {"access_token": "cached-access", "expires_in": 3600}

    original_post = GoogleAuth._post_token
    GoogleAuth._post_token = fake_post
    monkeypatch.setattr(GoogleAuth, "stored_refresh_token",
                        lambda self: "1//refresh")
    try:
        first = GoogleAuth(_settings()).access_token()
        second = GoogleAuth(_settings()).access_token()      # a different object
    finally:
        GoogleAuth._post_token = original_post
        GoogleAuth._cached_token = None
        GoogleAuth._cached_expiry = 0.0

    assert first == second == "cached-access"
    assert len(calls) == 1, f"refreshed {len(calls)} times for two reads"


def test_disconnect_clears_the_cache_for_every_instance(monkeypatch):
    """A token cached before a disconnect must not be served after it.

    This was the dangerous half of the per-instance cache: an object that had
    cached a token kept returning it after another object disconnected.
    """
    import jarvis.security.providers as providers

    GoogleAuth._cached_token = "stale-access"
    GoogleAuth._cached_expiry = __import__("time").time() + 3600
    monkeypatch.setattr(providers.KeychainStorage, "retrieve",
                        staticmethod(lambda *a, **k: "1//refresh"))
    monkeypatch.setattr(providers.KeychainStorage, "delete",
                        staticmethod(lambda *a, **k: True))
    # No network for the revoke call: answer as Google's "already gone".
    monkeypatch.setattr(GoogleAuth, "_read",
                        lambda self, *a, **k: (400, b'{"error":"invalid_token"}'))

    result = GoogleAuth(_settings()).disconnect()
    assert result["removed"] is True
    assert GoogleAuth._cached_token is None
    assert GoogleAuth._cached_expiry == 0.0


def test_concurrent_refreshes_do_not_stampede_google():
    """Two readers on an empty cache must cause one refresh, not two."""
    import threading
    import time as _time

    _fresh_pending()
    GoogleAuth._cached_token = None
    GoogleAuth._cached_expiry = 0.0
    calls: list = []
    started = threading.Barrier(4)

    def fake_post(self, payload):
        calls.append(1)
        _time.sleep(0.05)                          # the window a stampede needs
        return {"access_token": "one-token", "expires_in": 3600}

    original_post = GoogleAuth._post_token
    original_stored = GoogleAuth.stored_refresh_token
    GoogleAuth._post_token = fake_post
    GoogleAuth.stored_refresh_token = lambda self: "1//refresh"
    try:
        results: list = []

        def run():
            started.wait()
            results.append(GoogleAuth(_settings()).access_token())

        threads = [threading.Thread(target=run) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        GoogleAuth._post_token = original_post
        GoogleAuth.stored_refresh_token = original_stored
        GoogleAuth._cached_token = None
        GoogleAuth._cached_expiry = 0.0

    assert results == ["one-token"] * 4
    assert len(calls) == 1, f"{len(calls)} refreshes for four concurrent readers"


def test_a_rejected_access_token_is_dropped_from_the_cache():
    """A 400 from tokeninfo must not leave the bad token cached for its TTL."""
    GoogleAuth._cached_token = "rejected-token"
    GoogleAuth._cached_expiry = __import__("time").time() + 3600
    original_read = GoogleAuth._read
    try:
        GoogleAuth._read = lambda self, *a, **k: (400, b'{"error":"invalid_token"}')
        with pytest.raises(GoogleAuthError) as excinfo:
            GoogleAuth(_settings())._verify("rejected-token")
    finally:
        GoogleAuth._read = original_read
    assert excinfo.value.state is ConnectionState.EXPIRED
    assert GoogleAuth._cached_token is None


# ---------------------------------------------------------------------------
# Status classification is by state, not by wording; and a response we could
# not parse is never reported as "empty"
# ---------------------------------------------------------------------------

def test_a_disconnected_account_is_not_reported_as_a_google_failure(monkeypatch):
    """The fix differs -- "connect an account", not "Google is broken"."""
    result = GoogleTasksClient(_settings(), _DisconnectedAuth()).tasks()
    assert result.status is ProviderStatus.UNAVAILABLE, result.status
    assert result.status.read_succeeded is False


class _DisconnectedAuth(FakeAuth):
    def __init__(self):
        super().__init__(_settings(), token="", access="")
        self._token = ""

    def access_token(self):
        raise GoogleAuthError(
            "No Google account is connected. Connect one in Settings → Connections.",
            ConnectionState.DISCONNECTED)


def test_unparseable_events_are_not_an_empty_calendar():
    """A 200 whose events all lack a start is a provider error, not a clear day."""
    provider = _calendar_with(lambda r: (200, {"items": [{"id": "a"}, {"id": "b"}]}))
    now = datetime.now().astimezone()
    events = provider.list_events(now, now + timedelta(days=1))
    assert events.status is ProviderStatus.PROVIDER_ERROR
    assert events.status.read_succeeded is False
    assert "not an empty calendar" in events.detail


def test_unparseable_tasks_are_not_an_empty_list():
    """Same rule for tasks: no readable item is a failure, not "no tasks"."""
    tasks = _tasks_with(lambda r: (200, {"items": [{}, {"title": "no id"}]})).tasks()
    assert tasks.status is ProviderStatus.PROVIDER_ERROR
    assert tasks.status.read_succeeded is False
    assert "not an empty list" in tasks.detail


# ---------------------------------------------------------------------------
# Disconnect: an already-revoked token is not "Google could not be reached"
# ---------------------------------------------------------------------------

def test_disconnect_treats_an_already_revoked_token_as_revoked(monkeypatch):
    import jarvis.security.providers as providers

    monkeypatch.setattr(providers.KeychainStorage, "retrieve",
                        staticmethod(lambda *a, **k: "1//refresh"))
    monkeypatch.setattr(providers.KeychainStorage, "delete",
                        staticmethod(lambda *a, **k: True))
    # Google's answer for a token that is already dead.
    monkeypatch.setattr(GoogleAuth, "_read",
                        lambda self, *a, **k: (400, b'{"error":"invalid_token"}'))
    result = GoogleAuth(_settings()).disconnect()
    assert result["revoked"] is True, result
    assert "could not be reached" not in result["detail"], result["detail"]


def test_disconnect_still_reports_a_real_network_failure_honestly(monkeypatch):
    import jarvis.security.providers as providers

    monkeypatch.setattr(providers.KeychainStorage, "retrieve",
                        staticmethod(lambda *a, **k: "1//refresh"))
    monkeypatch.setattr(providers.KeychainStorage, "delete",
                        staticmethod(lambda *a, **k: True))

    def unreachable(*a, **k):
        raise OSError("network is down")

    monkeypatch.setattr(GoogleAuth, "_read", unreachable)
    result = GoogleAuth(_settings()).disconnect()
    assert result["revoked"] is False
    assert "could not be reached" in result["detail"]
    assert result["removed"] is True, "the local token must go regardless"


# ---------------------------------------------------------------------------
# The access-log scrubber is installed on every launch path
#
# `run.py` and `app.run()` install it; the macOS app launches uvicorn directly
# on `jarvis.api.app:app` and never reached either. Measured: the authorization
# code appeared verbatim in that log. Importing the app module is the one point
# every path shares.
# ---------------------------------------------------------------------------

def test_importing_the_app_installs_the_access_log_scrubber():
    import logging

    import jarvis.api.app  # noqa: F401  (import is the behaviour under test)
    from jarvis.log_safety import ScrubCredentialsFilter

    for name in ("uvicorn.access", "uvicorn.error"):
        filters = logging.getLogger(name).filters
        assert any(isinstance(f, ScrubCredentialsFilter) for f in filters), (
            f"{name} has no credential scrubber; a code in /google/callback "
            "would reach the log")


def test_a_stored_token_without_a_client_id_is_told_apart_from_no_account(monkeypatch):
    """Two different problems that used to read the same.

    Without a client id the status said "not configured" and reported
    ``has_token=False`` — hiding the fact that an account *had* been authorised
    and that clearing it is an option. The setup needed is a client id, not a
    new account.
    """
    from jarvis.integrations.google_auth import ConnectionState, GoogleAuth

    auth = GoogleAuth(Settings(google_client_id=""))
    monkeypatch.setattr(auth, "stored_refresh_token", lambda: "a-stored-refresh-token")
    status = auth.status()
    assert status.state is ConnectionState.NOT_CONFIGURED
    assert status.has_token is True
    assert "authorised earlier" in status.detail
    assert "Disconnect" in status.detail

    # With no stored token the same state must not claim an earlier authorisation.
    monkeypatch.setattr(auth, "stored_refresh_token", lambda: None)
    fresh = auth.status()
    assert fresh.has_token is False
    assert "authorised earlier" not in fresh.detail
    assert "JARVIS_GOOGLE_CLIENT_ID" in fresh.detail
