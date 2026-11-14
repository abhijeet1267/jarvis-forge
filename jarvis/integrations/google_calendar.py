"""Google Calendar as a Vantage calendar provider.

Implements the existing ``CalendarProvider`` contract, so the planner, the
availability tools and the API routes use Google without knowing it is Google.

**Reads are the deliverable; writes are refused honestly.** The connection
requests ``calendar.readonly`` (see ``google_auth``), so an attempt to create or
cancel an event here cannot succeed. Returning a clear "this connection is
read-only" result is the honest answer -- inventing a success, or asking for
write permission the UI has no controls for, are both worse.

Timezone handling is deliberate. Google returns either a date-time with an
offset or a bare date for all-day events; both are converted to the local
server zone so ``duration_minutes`` and availability maths stay correct. A
genuine failure is a ``ProviderResults`` with a non-success status, never an
empty list -- an empty Google calendar and a broken token look identical if the
difference is thrown away.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from ..logging_setup import get_logger

from ..net import http_read
from ..security.privacy import redact
from .calendar_provider import (
    CalendarEvent,
    CalendarProvider,
    ProviderResults,
    ProviderStatus,
    classify_http_status,
    classify_transport_error,
    parse_datetime,
)
from .google_auth import GoogleAuth, GoogleAuthError

log = get_logger("google_calendar")

CALENDAR_API = "https://www.googleapis.com/calendar/v3"

class GoogleCalendarProvider(CalendarProvider):
    """Events from a real Google Calendar account."""

    name = "google"
    is_remote = True
    #: Total wall-clock ceiling for one Google API exchange.
    TIMEOUT = 20.0

    def __init__(self, settings=None, auth: Optional[GoogleAuth] = None) -> None:
        super().__init__(settings)
        self.auth = auth or GoogleAuth(self.settings)

    def available(self) -> bool:
        """Configured *and* holding a credential -- not merely configured.

        This is what the selector branches on, so it must not claim Google is
        usable when no account has been connected: doing so would route every
        read to a provider that can only fail.
        """
        return self.auth.configured and self.auth.stored_refresh_token() is not None

    # -- reads -------------------------------------------------------------

    def list_events(self, start: datetime, end: datetime, limit: int = 50) -> ProviderResults:
        try:
            token = self.auth.access_token()
        except GoogleAuthError as exc:
            return ProviderResults([], _provider_status(exc.state), detail=str(exc))

        # RFC3339 with an offset, which is what the API requires for a window.
        params = {
            "timeMin": start.isoformat(),
            "timeMax": end.isoformat(),
            "singleEvents": "true",   # expand recurrences into real instances
            "orderBy": "startTime",
            "maxResults": str(max(1, min(limit, 250))),
        }
        url = f"{CALENDAR_API}/calendars/{self._calendar_id()}/events"
        try:
            events = self._get_all(url, params, token)
        except _ApiFailure as failure:
            log.info("google calendar read failed",
                     extra_fields={"outcome": failure.status.value, "error": failure.detail})
            return ProviderResults([], failure.status, detail=failure.detail)

        out: List[CalendarEvent] = []
        for raw in events:
            parsed = self._to_event(raw)
            if parsed is not None:
                out.append(parsed)
        if events and not out:
            # Google answered with items, but not one of them could be read.
            # An empty ProviderResults would be turned into REAL_EMPTY by the
            # contract below -- claiming "nothing scheduled" from a response we
            # could not parse. A provider error is the honest status.
            log.info("google calendar response held no readable events",
                     extra_fields={"raw": len(events)})
            return ProviderResults(
                [], ProviderStatus.PROVIDER_ERROR,
                detail=("Google Calendar answered, but none of the returned events could "
                        "be read. This is not an empty calendar."))
        return ProviderResults(out, ProviderStatus.HAS_RESULTS)

    def _get_all(self, url: str, params: Dict[str, str], token: str) -> List[Dict[str, Any]]:
        """Follow pagination, with a hard cap so a huge calendar cannot hang."""
        collected: List[Dict[str, Any]] = []
        page_token = ""
        for _ in range(10):                      # at most 10 pages
            query = dict(params)
            if page_token:
                query["pageToken"] = page_token
            try:
                full = url + "?" + urllib.parse.urlencode(query)
                request = urllib.request.Request(
                    full, headers={"Authorization": f"Bearer {token}"}, method="GET")
                status, raw = self._request(request)
            except Exception as exc:
                raise _ApiFailure(
                    classify_transport_error(exc),
                    f"Could not reach Google Calendar ({type(exc).__name__}).",
                ) from exc
            if status >= 400:
                raise _ApiFailure(
                    classify_http_status(status),
                    self._http_detail(status, raw),
                )
            try:
                body = json.loads(raw or b"{}")
            except Exception as exc:
                raise _ApiFailure(
                    ProviderStatus.PROVIDER_ERROR,
                    "Google Calendar returned a response that was not JSON.",
                ) from exc
            collected.extend(body.get("items") or [])
            page_token = str(body.get("nextPageToken") or "")
            if not page_token:
                break
        return collected

    def _request(self, request):
        """(status, body bytes). Split out so tests can replace it wholesale.

        An HTTP error is returned, not raised: ``http_read`` wraps ``urlopen``,
        which raises ``HTTPError`` for 4xx/5xx, and a 401 from Google is a fact
        the caller branches on -- not an unreachable server.
        """
        try:
            return 200, http_read(request, total=self.TIMEOUT, what="Google Calendar")
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, exc.read() or b""
            finally:
                exc.close()

    @staticmethod
    def _http_detail(status_code: int, raw: bytes) -> str:
        """Google's own message, trimmed: it is short and safe."""
        try:
            error = json.loads(raw or b"{}").get("error") or {}
            message = str(error.get("message") or "")
        except Exception:
            message = ""
        # Untrusted: an API error may echo the request, credential included.
        message = redact(message)
        if status_code == 401:
            return "Google rejected the access token. Reconnect the account in Settings."
        if status_code == 403:
            return ("Google refused access to this calendar. The account may lack the "
                    "calendar permission, or the calendar id may be wrong." +
                    (f" ({message[:160]})" if message else ""))
        if status_code == 404:
            return "That calendar does not exist on this Google account."
        return f"Google Calendar returned HTTP {status_code}" + (
            f": {message[:160]}" if message else ".")

    def _calendar_id(self) -> str:
        return (self.settings.google_calendar_id or "primary").strip() or "primary"

    def _to_event(self, raw: Dict[str, Any]) -> Optional[CalendarEvent]:
        """One Google event -> the shared model, or None if unusable."""
        start_block = raw.get("start") or {}
        end_block = raw.get("end") or {}
        # An all-day event has `date`; a timed one has `dateTime`.
        all_day = "date" in start_block and "dateTime" not in start_block
        start = parse_datetime(str(start_block.get("dateTime") or start_block.get("date") or ""))
        end = parse_datetime(str(end_block.get("dateTime") or end_block.get("date") or ""))
        if start is None:
            return None                        # an event with no start is not renderable
        if end is None:
            end = start + (timedelta(days=1) if all_day else timedelta(hours=1))
        local = datetime.now().astimezone().tzinfo
        if start.tzinfo is not None:
            start = start.astimezone(local)
        if end.tzinfo is not None:
            end = end.astimezone(local)
        return CalendarEvent(
            uid=str(raw.get("id") or raw.get("iCalUID") or ""),
            title=str(raw.get("summary") or "(untitled)"),
            start=start,
            end=end,
            location=str(raw.get("location") or ""),
            description=str(raw.get("description") or ""),
            calendar=str(raw.get("organizer", {}).get("displayName")
                         or self._calendar_id()),
            all_day=all_day,
        )

    # -- writes ------------------------------------------------------------
    #
    # The connection holds calendar.readonly, so these cannot work. Saying so is
    # the contract: a caller must be able to tell "refused" from "done".

    def _read_only(self) -> Dict[str, Any]:
        return {
            "ok": False,
            "provider": self.name,
            "status": ProviderStatus.AUTH_ERROR.value,
            "error": ("This Google connection is read-only. Editing events needs the "
                      "calendar write permission, which Vantage does not request."),
            "read_only": True,
        }

    def create(self, event: CalendarEvent) -> Dict[str, Any]:
        return self._read_only()

    def update(self, uid: str, changes: Dict[str, Any]) -> Dict[str, Any]:
        return self._read_only()

    def cancel(self, uid: str) -> Dict[str, Any]:
        return self._read_only()

    def describe(self) -> Dict[str, Any]:
        status = self.auth.status()
        return {
            "provider": self.name,
            "available": self.available(),
            "remote": self.is_remote,
            "read_only": True,
            "connection": status.as_dict(),
        }

def _provider_status(state) -> ProviderStatus:
    """Map the auth module's state onto the provider read status."""
    from .google_auth import ConnectionState
    return {
        ConnectionState.NOT_CONFIGURED: ProviderStatus.NOT_CONFIGURED,
        ConnectionState.DISCONNECTED: ProviderStatus.UNAVAILABLE,
        ConnectionState.EXPIRED: ProviderStatus.AUTH_ERROR,
        ConnectionState.ERROR: ProviderStatus.PROVIDER_ERROR,
    }.get(state, ProviderStatus.PROVIDER_ERROR)

class _ApiFailure(Exception):
    def __init__(self, status: ProviderStatus, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail