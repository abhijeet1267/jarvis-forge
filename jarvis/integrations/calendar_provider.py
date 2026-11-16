"""Calendar providers (v0.7).

Two backends, both real:

* :class:`LocalCalendarProvider` — events in SQLite. Full CRUD, works offline,
  and is the default so the feature is useful without an account.
* :class:`CalDavProvider` — reads and writes iCalendar (RFC 5545) over CalDAV
  using ``httpx``. No third-party calendar library is installed, so the
  iCalendar handling here is a deliberately small subset: VEVENT with
  DTSTART/DTEND/SUMMARY/LOCATION/DESCRIPTION/UID. Anything it cannot parse is
  reported rather than silently dropped.

A read returns :class:`ProviderResults`: a real list of events that also carries
the :class:`ProviderStatus` it ended in, so "the calendar is empty"
(``REAL_EMPTY``) can never be confused with "the server was unreachable"
(``UNAVAILABLE``), "the credentials were rejected" (``AUTH_ERROR``), a broken
connection (``NETWORK_ERROR``) or a refused command (``PROVIDER_ERROR``).
"""

from __future__ import annotations

import re
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional

from ..config import Settings, get_settings
from ..core.models import new_id
from ..db.database import get_db
from ..logging_setup import get_logger
from ..security.privacy import redact

log = get_logger("integrations.calendar")

#: Formats accepted for a user-supplied date/time string.
_DATETIME_FORMATS = [
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y",
]


# ---------------------------------------------------------------------------
# What a read actually encountered
# ---------------------------------------------------------------------------
class ProviderStatus(str, Enum):
    """The state a provider read ended in.

    ``REAL_EMPTY`` and "returned nothing" are not the same fact, and collapsing
    them is how an unreachable CalDAV server came to be reported to a user as
    "Nothing scheduled for today" -- and how a failed read came to hand out a
    bookable free slot. The four failure states are separated because each one
    asks the user for a different action: start the server, fix the password,
    or wait for the network.

    ``HAS_RESULTS`` exists so that "answered, and here is what it said" is also
    an explicit state rather than an absence of one.
    """

    HAS_RESULTS = "has_results"
    REAL_EMPTY = "real_empty"
    UNAVAILABLE = "unavailable"
    AUTH_ERROR = "auth_error"
    NETWORK_ERROR = "network_error"
    PROVIDER_ERROR = "provider_error"
    #: The provider is not set up at all (no client id, no account connected).
    #: Distinct from PROVIDER_ERROR because the fix is "finish the setup", not
    #: "something is broken" -- and a screen that says the wrong one sends the
    #: person looking in the wrong place.
    NOT_CONFIGURED = "not_configured"

    @property
    def read_succeeded(self) -> bool:
        """True only when the provider answered and its answer can be trusted."""
        return self in (ProviderStatus.HAS_RESULTS, ProviderStatus.REAL_EMPTY)


class ProviderResults(List[Any]):
    """A list of items that also records what the provider encountered.

    This *is* a ``list``: the REST endpoints, the briefing builder and the tools
    all iterate, ``len()`` and boolean-test the result of a read, and those
    consumers are not allowed to be wrong about a failure. So a failed read
    stays an empty list to them, while ``status`` says why it is empty. Only a
    read that actually succeeded may report :attr:`ProviderStatus.REAL_EMPTY`.

    ``detail`` and ``endpoint`` are redacted, credential-free fragments meant
    for a user-facing sentence.
    """

    def __init__(
        self,
        items: Optional[Iterable[Any]] = None,
        status: ProviderStatus = ProviderStatus.REAL_EMPTY,
        detail: str = "",
        endpoint: str = "",
    ) -> None:
        super().__init__(items or [])
        if self and status is ProviderStatus.REAL_EMPTY:
            # Items in hand means the provider answered with something.
            status = ProviderStatus.HAS_RESULTS
        if not self and status is ProviderStatus.HAS_RESULTS:
            status = ProviderStatus.REAL_EMPTY
        self.status = status
        self.detail = detail or ""
        self.endpoint = endpoint or ""

    @property
    def read_succeeded(self) -> bool:
        return self.status.read_succeeded


def read_status(result: Any) -> ProviderStatus:
    """What a read result reports about itself, labelled or not.

    A provider that does not participate in the status contract (a plain list
    from a test double, say) can only ever be described by what is visible:
    items, or none. It is never described as a failure, because nothing said so.
    """
    status = getattr(result, "status", None)
    if isinstance(status, ProviderStatus):
        return status
    return ProviderStatus.HAS_RESULTS if result else ProviderStatus.REAL_EMPTY


def subset_read(source: Any, items: Iterable[Any]) -> ProviderResults:
    """A filtered view of a read that keeps the status the read encountered.

    Without this, ``[e for e in events if ...]`` over a failed read turns
    "could not reach the server" back into a bare ``[]``.
    """
    return ProviderResults(
        items,
        read_status(source),
        getattr(source, "detail", "") or "",
        getattr(source, "endpoint", "") or "",
    )


def status_token(value: Any) -> Optional[ProviderStatus]:
    """The status a provider put into a dict result, if it put one in."""
    if isinstance(value, ProviderStatus):
        return value
    if not value:
        return None
    try:
        return ProviderStatus(str(value))
    except ValueError:
        return None


def safe_endpoint(url: str) -> str:
    """A server reference safe to show a user: no scheme, no credentials.

    Calendar and mail URLs are user-entered and may carry ``user:password@``;
    an error sentence must not repeat it.
    """
    from urllib.parse import urlsplit

    text = (url or "").strip()
    if not text:
        return ""
    parts = urlsplit(text)
    host = parts.netloc.rpartition("@")[2]
    if not host:
        return text
    return f"{host}{parts.path}" if parts.path else host


def classify_http_status(status_code: int) -> ProviderStatus:
    """Map an HTTP/CalDAV response code onto the state it actually is."""
    if status_code in (401, 407, 419):
        return ProviderStatus.AUTH_ERROR
    if status_code == 408:
        return ProviderStatus.UNAVAILABLE
    return ProviderStatus.PROVIDER_ERROR


def classify_transport_error(exc: BaseException) -> ProviderStatus:
    """Map a raised transport/protocol failure onto a state.

    Ordering matters: ``httpx.TimeoutException`` is a ``TransportError``
    subclass, ``ssl.SSLError`` is an ``OSError``, and ``imaplib.IMAP4.abort`` is
    an ``imaplib.IMAP4.error`` -- each pair needs the more specific check first.
    """
    import socket

    text = str(exc)
    lowered = text.lower()

    # A refusal from the egress firewall is not a transport failure. Telling the
    # person the network had a problem when the read was *deliberately* blocked
    # would send them debugging a connection that is fine -- the same mistake
    # the provider path avoids by naming "egress_refused".
    try:
        from ..security.egress import EgressRefused

        if isinstance(exc, EgressRefused):
            # The read was blocked on purpose. UNAVAILABLE is the honest state:
            # the provider cannot be read right now, and the detail carries the
            # reason rather than implying the network failed.
            return ProviderStatus.UNAVAILABLE
    except Exception:  # pragma: no cover
        pass

    try:  # httpx is a declared dependency; stay safe if it is missing anyway.
        import httpx
    except Exception:  # pragma: no cover
        httpx = None

    if httpx is not None:
        if isinstance(exc, httpx.TimeoutException):
            return ProviderStatus.UNAVAILABLE
        if isinstance(exc, httpx.ConnectError):
            return ProviderStatus.UNAVAILABLE
        if isinstance(exc, httpx.TransportError):
            return ProviderStatus.NETWORK_ERROR

    try:
        import imaplib
    except Exception:  # pragma: no cover
        imaplib = None

    if imaplib is not None and isinstance(exc, imaplib.IMAP4.error):
        # ``abort`` is raised when the connection dropped mid-command.
        if isinstance(exc, imaplib.IMAP4.abort) or "socket error" in lowered:
            return ProviderStatus.NETWORK_ERROR
        if any(marker in lowered for marker in ("login", "authenticate", "credentials", "password")):
            return ProviderStatus.AUTH_ERROR
        return ProviderStatus.PROVIDER_ERROR

    try:
        import ssl as _ssl
    except Exception:  # pragma: no cover
        _ssl = None

    if _ssl is not None and isinstance(exc, _ssl.SSLError):
        return ProviderStatus.NETWORK_ERROR
    if isinstance(exc, (socket.gaierror, socket.timeout, ConnectionRefusedError, TimeoutError)):
        return ProviderStatus.UNAVAILABLE
    if isinstance(exc, OSError):
        return ProviderStatus.NETWORK_ERROR
    return ProviderStatus.PROVIDER_ERROR


def error_detail(exc: BaseException, settings: Optional[Settings] = None) -> str:
    """A one-line, redacted description of a failure for the user to act on."""
    first_line = (str(exc).strip().splitlines() or [""])[0]
    name = type(exc).__name__
    text = f"{name}: {first_line}" if first_line else name
    # Credentials can arrive embedded in a URL inside a transport error.
    text = re.sub(r"//[^/@\s]+:[^/@\s]+@", "//", text)
    text = text[:200]
    return redact(text, settings) if settings is not None else redact(text)


def parse_datetime(value: str, default: Optional[datetime] = None) -> Optional[datetime]:
    """Parse a date/time string into an aware datetime.

    Returns ``None`` when the value cannot be understood, so the caller can ask
    rather than guessing a time.

    Two kinds of input are understood:

    * the strict formats in ``_DATETIME_FORMATS`` — ``2026-10-23 09:00``;
    * **ordinary English** — ``tomorrow at 9:00 AM``, ``next Friday at 3pm``,
      ``in 2 hours``, ``9am tomorrow`` — via
      :func:`jarvis.integrations.natural_time.parse_natural_datetime`.

    The English form matters because of what it used to cost: the assistant
    proposed ``"tomorrow at 9:00 AM"``, the person approved it, and the
    calendar tool then refused the time — so the approval was spent on
    nothing. One of twelve ordinary time phrases parsed before this.
    """
    if not value:
        return default
    text = value.strip()

    # Already ISO with an offset.
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
        return parsed
    except ValueError:
        pass

    for fmt in _DATETIME_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
        except ValueError:
            continue

    # Ordinary English. Tried last, so an explicit format always wins and
    # "2026-10-23" is never reinterpreted as something else.
    from .natural_time import parse_natural_datetime

    return parse_natural_datetime(text)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


@dataclass
class CalendarEvent:
    """One calendar event."""

    uid: str
    title: str
    start: datetime
    end: datetime
    location: str = ""
    description: str = ""
    calendar: str = "default"
    all_day: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "uid": self.uid,
            "title": self.title,
            "start": _iso(self.start),
            "end": _iso(self.end),
            "location": self.location,
            "description": self.description,
            "calendar": self.calendar,
            "all_day": self.all_day,
            "duration_minutes": int((self.end - self.start).total_seconds() // 60),
        }


def events_overlap(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    """Half-open interval overlap: touching endpoints do not conflict."""
    return a_start < b_end and b_start < a_end


class CalendarProvider(ABC):
    """Read and write calendar events."""

    name: str = "base"
    is_remote: bool = False

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()

    @abstractmethod
    def available(self) -> bool:
        ...

    @abstractmethod
    def list_events(self, start: datetime, end: datetime, limit: int = 50) -> "ProviderResults":
        ...

    @abstractmethod
    def create(self, event: CalendarEvent) -> Dict[str, Any]:
        ...

    @abstractmethod
    def update(self, uid: str, changes: Dict[str, Any]) -> Dict[str, Any]:
        ...

    @abstractmethod
    def cancel(self, uid: str) -> Dict[str, Any]:
        ...

    def search(self, query: str, limit: int = 20) -> "ProviderResults":
        needle = (query or "").strip().lower()
        now = datetime.now().astimezone()
        window = self.list_events(now - timedelta(days=365), now + timedelta(days=365), limit=500)
        if not needle:
            return subset_read(window, window[:limit])
        return subset_read(
            window,
            [
                e
                for e in window
                if needle in e.title.lower()
                or needle in e.location.lower()
                or needle in e.description.lower()
            ][:limit],
        )

    def conflicts(self, start: datetime, end: datetime, ignore_uid: str = "") -> "ProviderResults":
        window = self.list_events(start - timedelta(days=1), end + timedelta(days=1), limit=200)
        return subset_read(
            window,
            [
                event
                for event in window
                if event.uid != ignore_uid and events_overlap(start, end, event.start, event.end)
            ],
        )

    def describe(self) -> Dict[str, Any]:
        return {"provider": self.name, "available": self.available(), "remote": self.is_remote}


# ---------------------------------------------------------------------------
# Local SQLite calendar
# ---------------------------------------------------------------------------
class LocalCalendarProvider(CalendarProvider):
    """Events stored in SQLite. Real CRUD, no account needed."""

    name = "local_calendar"
    is_remote = False

    def __init__(self, settings: Optional[Settings] = None) -> None:
        super().__init__(settings)
        self.db = get_db()

    def available(self) -> bool:
        return True

    def list_events(self, start: datetime, end: datetime, limit: int = 50) -> ProviderResults:
        rows = self.db.query(
            """
            SELECT * FROM calendar_events
            WHERE start_at < ? AND end_at > ?
            ORDER BY start_at ASC
            LIMIT ?
            """,
            (_iso(end), _iso(start), limit),
        )
        events = [self._row_to_event(r) for r in rows]
        # The database answered, so an empty result genuinely means "nothing
        # scheduled" rather than "I could not look".
        return ProviderResults(events, ProviderStatus.HAS_RESULTS if events else ProviderStatus.REAL_EMPTY)

    def create(self, event: CalendarEvent) -> Dict[str, Any]:
        if not event.uid:
            event.uid = new_id("EVT")
        self.db.execute(
            """
            INSERT INTO calendar_events
                (uid, title, start_at, end_at, location, description, calendar, all_day)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.uid,
                event.title,
                _iso(event.start),
                _iso(event.end),
                event.location,
                event.description,
                event.calendar,
                1 if event.all_day else 0,
            ),
        )
        return {"ok": True, "provider": self.name, "uid": event.uid, "event": event.as_dict()}

    def update(self, uid: str, changes: Dict[str, Any]) -> Dict[str, Any]:
        existing = self.get(uid)
        if existing is None:
            return {"ok": False, "error": f"no event with id {uid!r}"}

        fields, params = [], []
        for column, value in (
            ("title", changes.get("title")),
            ("location", changes.get("location")),
            ("description", changes.get("description")),
        ):
            if value is not None:
                fields.append(f"{column} = ?")
                params.append(value)
        if changes.get("start") is not None:
            fields.append("start_at = ?")
            params.append(_iso(changes["start"]))
        if changes.get("end") is not None:
            fields.append("end_at = ?")
            params.append(_iso(changes["end"]))

        if not fields:
            return {"ok": False, "error": "nothing to change"}

        params.append(uid)
        self.db.execute(f"UPDATE calendar_events SET {', '.join(fields)} WHERE uid = ?", tuple(params))
        return {"ok": True, "provider": self.name, "uid": uid, "event": self.get(uid).as_dict()}

    def cancel(self, uid: str) -> Dict[str, Any]:
        existing = self.get(uid)
        if existing is None:
            return {"ok": False, "error": f"no event with id {uid!r}"}
        self.db.execute("DELETE FROM calendar_events WHERE uid = ?", (uid,))
        return {"ok": True, "provider": self.name, "uid": uid, "cancelled": existing.title}

    def get(self, uid: str) -> Optional[CalendarEvent]:
        rows = self.db.query("SELECT * FROM calendar_events WHERE uid = ?", (uid,))
        return self._row_to_event(rows[0]) if rows else None

    @staticmethod
    def _row_to_event(row: Any) -> CalendarEvent:
        start = datetime.fromisoformat(row["start_at"])
        end = datetime.fromisoformat(row["end_at"])
        return CalendarEvent(
            uid=row["uid"],
            title=row["title"],
            start=start,
            end=end,
            location=row["location"] or "",
            description=row["description"] or "",
            calendar=row["calendar"] or "default",
            all_day=bool(row["all_day"]),
        )

    def describe(self) -> Dict[str, Any]:
        base = super().describe()
        rows = self.db.query("SELECT COUNT(*) AS n FROM calendar_events")
        base.update(
            {
                "event_count": rows[0]["n"] if rows else 0,
                "note": "Local calendar — stored in SQLite, works offline.",
            }
        )
        return base


# ---------------------------------------------------------------------------
# CalDAV
# ---------------------------------------------------------------------------
def to_ics(event: CalendarEvent) -> str:
    """Serialise one event as a minimal VEVENT."""
    def stamp(value: datetime) -> str:
        return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    def escape(value: str) -> str:
        return (
            (value or "")
            .replace("\\", "\\\\")
            .replace(";", "\\;")
            .replace(",", "\\,")
            .replace("\n", "\\n")
        )

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Vantage//EN",
        "BEGIN:VEVENT",
        f"UID:{event.uid}",
        f"DTSTAMP:{stamp(datetime.now(timezone.utc))}",
        f"DTSTART:{stamp(event.start)}",
        f"DTEND:{stamp(event.end)}",
        f"SUMMARY:{escape(event.title)}",
    ]
    if event.location:
        lines.append(f"LOCATION:{escape(event.location)}")
    if event.description:
        lines.append(f"DESCRIPTION:{escape(event.description)}")
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(lines) + "\r\n"


def _unfold(text: str) -> List[str]:
    """Undo RFC 5545 line folding."""
    lines: List[str] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if line.startswith((" ", "\t")) and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return lines


def from_ics(text: str) -> List[CalendarEvent]:
    """Parse VEVENTs from an iCalendar document.

    A deliberately small subset. Anything unparseable is skipped rather than
    guessed at.
    """
    events: List[CalendarEvent] = []
    current: Optional[Dict[str, str]] = None

    for line in _unfold(text):
        if line == "BEGIN:VEVENT":
            current = {}
            continue
        if line == "END:VEVENT":
            if current is not None:
                event = _event_from_fields(current)
                if event is not None:
                    events.append(event)
            current = None
            continue
        if current is None or ":" not in line:
            continue

        key, _, value = line.partition(":")
        key = key.split(";")[0].strip().upper()
        current[key] = value.strip()

    return events


def _event_from_fields(fields: Dict[str, str]) -> Optional[CalendarEvent]:
    start = _parse_ics_datetime(fields.get("DTSTART", ""))
    if start is None:
        return None
    end = _parse_ics_datetime(fields.get("DTEND", "")) or (start + timedelta(hours=1))

    def unescape(value: str) -> str:
        return (
            value.replace("\\n", "\n")
            .replace("\\,", ",")
            .replace("\\;", ";")
            .replace("\\\\", "\\")
        )

    return CalendarEvent(
        uid=fields.get("UID", "") or new_id("EVT"),
        title=unescape(fields.get("SUMMARY", "")) or "(untitled)",
        start=start,
        end=end,
        location=unescape(fields.get("LOCATION", "")),
        description=unescape(fields.get("DESCRIPTION", "")),
    )


def _parse_ics_datetime(value: str) -> Optional[datetime]:
    text = (value or "").strip()
    if not text:
        return None
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S", "%Y%m%d"):
        try:
            parsed = datetime.strptime(text, fmt)
            if fmt.endswith("Z"):
                return parsed.replace(tzinfo=timezone.utc)
            return parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
        except ValueError:
            continue
    return None


class CalDavProvider(CalendarProvider):
    """Read and write events on a CalDAV server."""

    name = "caldav"
    is_remote = True

    def __init__(self, settings: Optional[Settings] = None) -> None:
        super().__init__(settings)
        self.url = settings.calendar_url
        self.user = settings.calendar_user
        self.password = settings.calendar_password

    def available(self) -> bool:
        return bool(self.url and self.user and self.password)

    def _headers(self) -> Dict[str, str]:
        import base64

        token = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
        return {"Authorization": f"Basic {token}", "Content-Type": "text/calendar; charset=utf-8"}

    def _resource(self, uid: str) -> str:
        return f"{self.url.rstrip('/')}/{uid}.ics"

    def _failure(self, status: ProviderStatus, detail: str) -> ProviderResults:
        """An empty read that says why it is empty."""
        return ProviderResults([], status, detail=detail, endpoint=safe_endpoint(self.url))

    def _write_failure(self, status: ProviderStatus, detail: str) -> Dict[str, Any]:
        """A write result carrying the same states a read can report.

        The dict contract is what the tools already branch on; ``status`` lets
        them say "your password was rejected" instead of "caldav returned 401".
        """
        return {
            "ok": False,
            "provider": self.name,
            "error": detail,
            "status": status.value,
            "detail": detail,
            "endpoint": safe_endpoint(self.url),
        }

    def list_events(self, start: datetime, end: datetime, limit: int = 50) -> ProviderResults:
        import httpx

        body = f"""<?xml version="1.0" encoding="utf-8" ?>
<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop><c:calendar-data/></d:prop>
  <c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT"/></c:comp-filter></c:filter>
</c:calendar-query>"""

        from ..security.egress import require  # noqa: E402

        try:
            require(self.url)
            with httpx.Client(timeout=20.0) as client:
                response = client.request(
                    "REPORT", self.url, content=body.encode(),
                    headers={**self._headers(), "Depth": "1"},
                )
        except Exception as exc:
            status = classify_transport_error(exc)
            detail = error_detail(exc, self.settings)
            log.info(
                "caldav unreachable",
                extra_fields={"outcome": status.value, "error": detail},
            )
            return self._failure(status, detail)

        if response.status_code >= 400:
            # The server answered; it just did not give us a calendar. That is a
            # failure, not an empty day.
            status = classify_http_status(response.status_code)
            detail = f"HTTP {response.status_code}"
            log.info(
                "caldav query failed",
                extra_fields={"status": response.status_code, "outcome": status.value},
            )
            return self._failure(status, detail)

        events: List[CalendarEvent] = []
        for block in re.findall(r"BEGIN:VCALENDAR.*?END:VCALENDAR", response.text, re.S):
            for event in from_ics(block.replace("&#13;", "\r")):
                if events_overlap(start, end, event.start, event.end):
                    events.append(event)
        events.sort(key=lambda e: e.start)
        return ProviderResults(
            events[:limit],
            ProviderStatus.HAS_RESULTS if events[:limit] else ProviderStatus.REAL_EMPTY,
            endpoint=safe_endpoint(self.url),
        )

    def create(self, event: CalendarEvent) -> Dict[str, Any]:
        import httpx

        if not event.uid:
            event.uid = f"jarvis-{uuid.uuid4().hex[:12]}"
        from ..security.egress import require  # noqa: E402

        try:
            require(self.url)
            with httpx.Client(timeout=20.0) as client:
                response = client.put(
                    self._resource(event.uid),
                    content=to_ics(event).encode(),
                    headers=self._headers(),
                )
        except Exception as exc:
            status = classify_transport_error(exc)
            return self._write_failure(status, f"caldav unreachable: {error_detail(exc, self.settings)}")

        if response.status_code >= 400:
            status = classify_http_status(response.status_code)
            return self._write_failure(status, f"caldav returned HTTP {response.status_code}")
        return {"ok": True, "provider": self.name, "uid": event.uid, "event": event.as_dict()}

    def update(self, uid: str, changes: Dict[str, Any]) -> Dict[str, Any]:
        import httpx

        window = self.list_events(
            datetime.now().astimezone() - timedelta(days=3650),
            datetime.now().astimezone() + timedelta(days=3650),
            limit=1000,
        )
        existing = next((e for e in window if e.uid == uid), None)
        if existing is None:
            # "there is no such event" is only an honest answer if the lookup
            # itself answered. A failed read must not be reported as "missing".
            if not read_status(window).read_succeeded:
                return self._write_failure(read_status(window), getattr(window, "detail", "") or "read failed")
            return {"ok": False, "provider": self.name, "error": f"no event {uid!r}"}

        for key in ("title", "location", "description"):
            if changes.get(key) is not None:
                setattr(existing, key, changes[key])
        if changes.get("start") is not None:
            existing.start = changes["start"]
        if changes.get("end") is not None:
            existing.end = changes["end"]

        from ..security.egress import require  # noqa: E402

        try:
            require(self.url)
            with httpx.Client(timeout=20.0) as client:
                response = client.put(
                    self._resource(uid), content=to_ics(existing).encode(), headers=self._headers()
                )
        except Exception as exc:
            return self._write_failure(
                classify_transport_error(exc),
                f"caldav unreachable: {error_detail(exc, self.settings)}",
            )
        if response.status_code >= 400:
            return self._write_failure(
                classify_http_status(response.status_code), f"caldav returned HTTP {response.status_code}"
            )
        return {"ok": True, "provider": self.name, "uid": uid, "event": existing.as_dict()}

    def cancel(self, uid: str) -> Dict[str, Any]:
        import httpx

        from ..security.egress import require  # noqa: E402

        try:
            require(self.url)
            with httpx.Client(timeout=20.0) as client:
                response = client.delete(self._resource(uid), headers=self._headers())
        except Exception as exc:
            return self._write_failure(
                classify_transport_error(exc),
                f"caldav unreachable: {error_detail(exc, self.settings)}",
            )
        if response.status_code >= 400:
            return self._write_failure(
                classify_http_status(response.status_code), f"caldav returned HTTP {response.status_code}"
            )
        return {"ok": True, "provider": self.name, "uid": uid}

    def describe(self) -> Dict[str, Any]:
        base = super().describe()
        base.update({"url": self.url, "account": self.user})
        return base


def get_calendar_provider(settings: Optional[Settings] = None) -> CalendarProvider:
    """Prefer a connected Google account, then CalDAV, then the local calendar.

    Order matters and is explicit: Google is the service this integration adds,
    so a connected Google account wins. Both remote providers are only selected
    when an account is actually usable -- ``available()`` checks for a stored
    credential, not merely configuration, so a half-finished setup falls back to
    the local calendar instead of routing every read to a provider that can
    only fail.

    The local calendar is always the fallback, never removed: it is where
    local-only reminders live, and a disconnected Google must not take those
    away.
    """
    settings = settings or get_settings()

    if settings.google_calendar_enabled:
        from .google_calendar import GoogleCalendarProvider

        google = GoogleCalendarProvider(settings)
        if google.available():
            log.info("calendar provider selected", extra_fields={"provider": google.name})
            return google

    remote = CalDavProvider(settings)
    if remote.available():
        log.info("calendar provider selected", extra_fields={"provider": remote.name})
        return remote
    return LocalCalendarProvider(settings)


def describe_calendar(settings: Optional[Settings] = None) -> Dict[str, Any]:
    settings = settings or get_settings()
    remote = CalDavProvider(settings)
    local = LocalCalendarProvider(settings)
    out = {
        "active": get_calendar_provider(settings).name,
        "configured_account": remote.available(),
        "local": local.describe(),
        "writes_require_confirmation": True,
    }
    # Report Google's connection honestly, including when it is not set up:
    # a screen that shows nothing at all is indistinguishable from one that
    # failed to load.
    try:
        from .google_auth import describe_google
        from .google_calendar import GoogleCalendarProvider

        out["google"] = {
            "setup": describe_google(settings),
            "provider": GoogleCalendarProvider(settings).describe(),
        }
    except Exception:                       # never let an optional provider break this
        out["google"] = {"available": False, "error": "Google integration unavailable"}
    return out
