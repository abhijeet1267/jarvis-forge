"""Phase 10: an unreachable provider must never read as an empty day or inbox.

The defect this pins down: :class:`CalDavProvider` and
:class:`ImapSmtpProvider` swallowed every failure into ``[]``, so
``list_events`` answered "Nothing scheduled for today" against a closed port and
``find_free_time`` invented a bookable slot from a read that never happened. The
mail side did the same with "I found no messages matching ..." / "There is
nothing to summarise."

Nothing here reaches a real calendar account or mailbox, and no provider state
is invented to make a green run:

* unreachable states come from port 9 (discard -- nothing listens here), exactly
  like the watch-bridge tests already do;
* the states a closed port cannot produce (HTTP 401/500, an IMAP command
  answering ``NO``) come from a localhost socket that returns *an error status
  and an empty body*, and from an IMAP stub that returns no message data at all.

The property under test is that "there is nothing" and "I could not look" are
different sentences, for every one of ``REAL_EMPTY``, ``UNAVAILABLE``,
``AUTH_ERROR``, ``NETWORK_ERROR`` and ``PROVIDER_ERROR``.

The sentences are asserted through ``ActionResult.summary``, which is where a
handler's wording actually lands: ``ok()`` puts it in ``message``, ``fail()`` in
``error``.
"""

from __future__ import annotations

import imaplib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

import pytest

from jarvis.config import Settings
from jarvis.integrations.calendar_provider import (
    CalDavProvider,
    LocalCalendarProvider,
    ProviderResults,
    ProviderStatus,
    read_status,
)
from jarvis.integrations.email_provider import (
    ImapSmtpProvider,
    LocalMailProvider,
)
from jarvis.tools import calendar as calendar_tools
from jarvis.tools import email as email_tools
from jarvis.tools.base import ToolContext
from jarvis.tools.calendar import (
    daily_schedule,
    find_free_time,
    list_events,
    next_event,
    search_events,
)
from jarvis.tools.email import read_email, search_email, summarize_email

#: Nothing is listening on port 9 (discard) on this machine.
CLOSED_CALENDAR_URL = "http://127.0.0.1:9/dav"
CLOSED_IMAP_HOST = "127.0.0.1"
CLOSED_IMAP_PORT = 9

#: The five states the tool layer must be able to tell apart.
ALL_STATES = [
    ProviderStatus.REAL_EMPTY,
    ProviderStatus.UNAVAILABLE,
    ProviderStatus.AUTH_ERROR,
    ProviderStatus.NETWORK_ERROR,
    ProviderStatus.PROVIDER_ERROR,
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def ctx() -> ToolContext:
    return ToolContext()


@pytest.fixture()
def bind_calendar(monkeypatch):
    """Make the calendar tools read from one specific provider."""

    def _bind(provider):
        monkeypatch.setattr(calendar_tools, "get_calendar_provider", lambda settings=None: provider)
        return provider

    return _bind


@pytest.fixture()
def bind_mailbox(monkeypatch):
    """Make the mail tools read from one specific provider."""

    def _bind(provider):
        monkeypatch.setattr(email_tools, "get_email_provider", lambda settings=None: provider)
        return provider

    return _bind


@pytest.fixture()
def unreachable_calendar() -> CalDavProvider:
    """A configured CalDAV account whose server is not there."""
    provider = CalDavProvider(
        Settings(
            calendar_url=CLOSED_CALENDAR_URL,
            calendar_user="me@example.com",
            calendar_password="not-a-real-password",
        )
    )
    assert provider.available(), "a remote account must be configured to be selected"
    return provider


def _closed_mailbox() -> ImapSmtpProvider:
    """A configured mail account whose IMAP server is not there."""
    provider = ImapSmtpProvider(
        Settings(
            email_address="me@example.com",
            email_password="not-a-real-password",
            email_imap_host=CLOSED_IMAP_HOST,
            email_imap_port=CLOSED_IMAP_PORT,
            email_smtp_host=CLOSED_IMAP_HOST,
            email_smtp_port=1,
        )
    )
    assert provider.available()
    return provider


@pytest.fixture()
def unreachable_mailbox() -> ImapSmtpProvider:
    return _closed_mailbox()


@pytest.fixture()
def http_calendar():
    """CalDAV endpoints served on localhost that only ever return an error.

    The bodies are empty on purpose: the server never claims to hold an event.
    """
    servers: List[ThreadingHTTPServer] = []

    def _make(status: int) -> CalDavProvider:
        class _ErrorHandler(BaseHTTPRequestHandler):
            def _refuse(self) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_REPORT = _refuse
            do_GET = _refuse
            do_POST = _refuse
            do_PROPFIND = _refuse

            def log_message(self, *_args: Any) -> None:  # keep the run quiet
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), _ErrorHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return CalDavProvider(
            Settings(
                calendar_url=f"http://127.0.0.1:{server.server_port}/dav",
                calendar_user="me@example.com",
                calendar_password="not-a-real-password",
            )
        )

    yield _make
    for server in servers:
        server.shutdown()
        server.server_close()


class _StubIMAPConnection:
    """An IMAP session that replies with protocol statuses and no messages.

    Only statuses are canned here -- ``search`` never returns a UID, so no mail
    is fabricated.
    """

    def __init__(
        self,
        select: Tuple[str, List[bytes]] = ("OK", [b"0"]),
        search: Tuple[str, List[bytes]] = ("OK", [b""]),
        fetch: Tuple[str, List[Any]] = ("NO", [None]),
    ) -> None:
        self._select = select
        self._search = search
        self._fetch = fetch
        self.logged_out = False

    def select(self, mailbox: str):  # noqa: ARG002
        return self._select

    def search(self, charset, *criteria):  # noqa: ARG002
        return self._search

    def fetch(self, num, spec):  # noqa: ARG002
        return self._fetch

    def logout(self):
        self.logged_out = True
        return ("BYE", [b""])


@pytest.fixture()
def imap_login_rejected(monkeypatch) -> ImapSmtpProvider:
    """A session whose login the server refuses (IMAP ``LOGIN failed``).

    A closed port cannot produce an authentication reply, so the protocol error
    imaplib raises for a rejected login is forced here, on one provider
    instance so other states stay reachable in the same test. Only the error is
    forced -- no message data is invented.
    """
    provider = _closed_mailbox()

    def _reject() -> None:
        raise imaplib.IMAP4.error("b'LOGIN failed.' LOGIN invalid credentials")

    monkeypatch.setattr(provider, "_imap", _reject)
    return provider


@pytest.fixture()
def imap_command_refused(monkeypatch, unreachable_mailbox):
    """A session that answers ``NO`` to the search command."""
    connection = _StubIMAPConnection(search=("NO", [b""]))
    monkeypatch.setattr(unreachable_mailbox, "_imap", lambda: connection)
    return unreachable_mailbox, connection


@pytest.fixture()
def empty_local_mailbox(tmp_area: Path) -> LocalMailProvider:
    """A real, genuinely empty local maildir."""
    provider = LocalMailProvider(Settings(home=tmp_area / "mailhome", api_token=""))
    provider.folder("inbox")
    return provider


# ---------------------------------------------------------------------------
# Provider layer: an empty result must say *why* it is empty
# ---------------------------------------------------------------------------
def test_closed_port_calendar_read_is_empty_and_labelled_unavailable(
    unreachable_calendar: CalDavProvider,
) -> None:
    """A failed read is empty (so existing consumers keep working) but labelled."""
    from datetime import datetime, timedelta

    now = datetime.now().astimezone()
    result = unreachable_calendar.list_events(now, now + timedelta(days=1))

    assert list(result) == [], "no events may be claimed from a failed read"
    assert bool(result) is False, "a list-shaped result keeps len()/truthiness working"
    assert read_status(result) is ProviderStatus.UNAVAILABLE
    assert result.read_succeeded is False
    assert result.detail, "the user needs to be told which failure happened"


def test_genuinely_empty_local_calendar_is_labelled_real_empty() -> None:
    """The one case where 'nothing scheduled' is true stays a success."""
    from datetime import datetime, timedelta

    provider = LocalCalendarProvider()
    far_future = datetime.now().astimezone() + timedelta(days=3650)
    result = provider.list_events(far_future, far_future + timedelta(days=1))

    assert list(result) == []
    assert read_status(result) is ProviderStatus.REAL_EMPTY
    assert result.read_succeeded is True


def test_empty_list_is_no_longer_ambiguous(
    unreachable_calendar: CalDavProvider,
) -> None:
    """Same emptiness, different meanings -- the status is what distinguishes them."""
    from datetime import datetime, timedelta

    now = datetime.now().astimezone()
    failed = unreachable_calendar.list_events(now, now + timedelta(days=1))
    empty = LocalCalendarProvider().list_events(now + timedelta(days=3650), now + timedelta(days=3651))

    assert list(failed) == list(empty) == []
    assert read_status(failed) is not read_status(empty)
    # An unlabelled plain list can only ever be described as empty, never as a
    # failure -- it is not allowed to invent a provider problem either.
    assert read_status([]) is ProviderStatus.REAL_EMPTY


def test_search_events_propagates_the_failed_read(unreachable_calendar: CalDavProvider) -> None:
    """``search()`` filters ``list_events()``; filtering must not launder status."""
    result = unreachable_calendar.search("design review", limit=5)

    assert list(result) == []
    assert read_status(result) is ProviderStatus.UNAVAILABLE


# ---------------------------------------------------------------------------
# Tool layer: the five calendar states produce five different sentences
# ---------------------------------------------------------------------------
async def _call(tool: Callable[..., Any], ctx: ToolContext, **kwargs: Any):
    return await tool(ctx, **kwargs)


FALSE_CALENDAR_STATEMENTS: List[Tuple[str, Callable[..., Any], str, dict]] = [
    ("list_events", list_events, "Nothing scheduled", {"date": "today"}),
    ("next_event", next_event, "nothing coming up", {}),
    ("search_events", search_events, "no events matching", {"query": "design review"}),
    ("find_free_time", find_free_time, "free 60-minute slot", {"date": "today", "duration_minutes": 60}),
    ("daily_schedule", daily_schedule, "is clear", {"date": "today"}),
]


@pytest.mark.parametrize(
    "tool_name,tool,forbidden,params",
    FALSE_CALENDAR_STATEMENTS,
    ids=[name for name, *_ in FALSE_CALENDAR_STATEMENTS],
)
async def test_unreachable_calendar_is_not_reported_as_an_empty_day(
    ctx: ToolContext,
    bind_calendar,
    unreachable_calendar: CalDavProvider,
    tool_name: str,
    tool: Callable[..., Any],
    forbidden: str,
    params: dict,
) -> None:
    """The reproduced Phase 9 defect: a closed port answered "nothing scheduled"."""
    bind_calendar(unreachable_calendar)
    result = await _call(tool, ctx, **params)

    assert result.ok is False, f"{tool_name} reported a failed read as a success"
    assert result.verified is False
    assert forbidden.lower() not in result.summary.lower()
    assert result.output.get("provider_status") == ProviderStatus.UNAVAILABLE.value
    assert "not an empty day" in result.summary.lower()


async def test_find_free_time_refuses_to_propose_a_slot_from_a_failed_read(
    ctx: ToolContext, bind_calendar, unreachable_calendar: CalDavProvider
) -> None:
    """The worst output: a bookable slot invented by a read that never happened."""
    bind_calendar(unreachable_calendar)
    result = await find_free_time(ctx, date="today", duration_minutes=60)

    assert result.ok is False
    assert result.output.get("slot") in (None, {}), "no slot may be offered"
    assert "will not suggest" in result.summary.lower()


async def test_http_401_is_an_auth_error_not_an_unreachable_server(
    ctx: ToolContext, bind_calendar, http_calendar
) -> None:
    """'Check your password' and 'cannot reach the server' are different actions."""
    provider = bind_calendar(http_calendar(401))
    from datetime import datetime, timedelta

    raw = provider.list_events(datetime.now().astimezone(), datetime.now().astimezone() + timedelta(days=1))
    assert read_status(raw) is ProviderStatus.AUTH_ERROR

    result = await list_events(ctx, date="today")
    assert result.ok is False
    assert result.output["provider_status"] == ProviderStatus.AUTH_ERROR.value
    assert "password" in result.summary.lower()
    assert "could not reach" not in result.summary.lower()


async def test_http_500_is_a_provider_error_not_an_empty_day(
    ctx: ToolContext, bind_calendar, http_calendar
) -> None:
    provider = bind_calendar(http_calendar(500))
    from datetime import datetime, timedelta

    raw = provider.list_events(datetime.now().astimezone(), datetime.now().astimezone() + timedelta(days=1))
    assert read_status(raw) is ProviderStatus.PROVIDER_ERROR

    result = await list_events(ctx, date="today")
    assert result.ok is False
    assert result.output["provider_status"] == ProviderStatus.PROVIDER_ERROR.value
    assert "nothing scheduled" not in result.summary.lower()


async def test_auth_failure_and_unreachable_server_are_different_sentences(
    ctx: ToolContext, bind_calendar, http_calendar, unreachable_calendar: CalDavProvider
) -> None:
    """Neither state may borrow the other's wording."""
    bind_calendar(http_calendar(401))
    auth = await list_events(ctx, date="today")

    bind_calendar(unreachable_calendar)
    unreachable = await list_events(ctx, date="today")

    assert auth.ok is False and unreachable.ok is False
    assert auth.summary != unreachable.summary
    assert "password" in auth.summary.lower()
    assert "password" not in unreachable.summary.lower()
    assert "reach" in unreachable.summary.lower()
    assert "reach" not in auth.summary.lower()


async def test_each_failure_state_reads_as_its_own_sentence(
    ctx: ToolContext, bind_calendar, unreachable_calendar: CalDavProvider
) -> None:
    """Every non-empty state has a distinct sentence and is a refusal."""
    seen: dict = {}
    for state in (
        ProviderStatus.UNAVAILABLE,
        ProviderStatus.AUTH_ERROR,
        ProviderStatus.NETWORK_ERROR,
        ProviderStatus.PROVIDER_ERROR,
    ):
        bind_calendar(_StubbedCalendar(state))
        result = await list_events(ctx, date="today")
        assert result.ok is False, state
        assert result.output["provider_status"] == state.value
        seen[state] = result.summary

    assert len(set(seen.values())) == len(seen), f"sentences collide: {seen}"


class _StubbedCalendar(LocalCalendarProvider):
    """A local calendar whose reads answer with a chosen status and no events."""

    name = "stubbed"

    def __init__(self, state: ProviderStatus) -> None:
        super().__init__()
        self._state = state

    def list_events(self, start, end, limit: int = 50) -> ProviderResults:  # noqa: D102
        return ProviderResults([], self._state, detail="forced by test", endpoint="stub.example")


# ---------------------------------------------------------------------------
# Tool layer: real emptiness is still a success
# ---------------------------------------------------------------------------
async def test_empty_local_calendar_still_reads_as_a_clear_day(
    ctx: ToolContext, bind_calendar
) -> None:
    from datetime import datetime, timedelta

    provider = LocalCalendarProvider()
    bind_calendar(provider)
    day = (datetime.now().astimezone() + timedelta(days=4000)).date().isoformat()

    result = await list_events(ctx, date=day)
    assert result.ok is True
    assert result.output["provider_status"] == ProviderStatus.REAL_EMPTY.value
    assert "Nothing scheduled" in result.summary


# ---------------------------------------------------------------------------
# Mail: the same defect, the same distinction
# ---------------------------------------------------------------------------
async def test_closed_port_mailbox_is_not_reported_as_an_empty_inbox(
    ctx: ToolContext, bind_mailbox, unreachable_mailbox: ImapSmtpProvider
) -> None:
    bind_mailbox(unreachable_mailbox)
    result = await search_email(ctx, query="invoice")

    assert result.ok is False, "a failed mailbox read was reported as a success"
    assert result.verified is False
    assert "found no messages" not in result.summary.lower()
    assert result.output.get("provider_status") == ProviderStatus.UNAVAILABLE.value
    assert "not an empty inbox" in result.summary.lower()


async def test_closed_port_mailbox_is_not_summarised_as_nothing_to_summarise(
    ctx: ToolContext, bind_mailbox, unreachable_mailbox: ImapSmtpProvider
) -> None:
    bind_mailbox(unreachable_mailbox)
    result = await summarize_email(ctx, limit=5)

    assert result.ok is False
    assert "nothing to summarise" not in result.summary.lower()
    assert result.output.get("provider_status") == ProviderStatus.UNAVAILABLE.value


async def test_imap_login_failure_is_auth_error_not_unreachable(
    ctx: ToolContext, bind_mailbox, imap_login_rejected: ImapSmtpProvider
) -> None:
    bind_mailbox(imap_login_rejected)
    result = await search_email(ctx, query="invoice")

    assert result.ok is False
    assert result.output["provider_status"] == ProviderStatus.AUTH_ERROR.value
    assert "password" in result.summary.lower()
    assert "reach" not in result.summary.lower()
    assert "found no messages" not in result.summary.lower()


async def test_imap_command_refused_is_provider_error_not_empty_inbox(
    ctx: ToolContext, bind_mailbox, imap_command_refused
) -> None:
    provider, connection = imap_command_refused
    bind_mailbox(provider)
    result = await search_email(ctx, query="invoice")

    assert result.ok is False
    assert result.output["provider_status"] == ProviderStatus.PROVIDER_ERROR.value
    assert "found no messages" not in result.summary.lower()
    assert connection.logged_out, "the session must still be closed"


async def test_imap_session_closed_mid_flight_is_a_network_error(
    ctx: ToolContext, bind_mailbox, monkeypatch, unreachable_mailbox: ImapSmtpProvider
) -> None:
    """The server was reachable; the transport died. That is not 'unavailable'."""
    connection = _StubIMAPConnection()
    monkeypatch.setattr(unreachable_mailbox, "_imap", lambda: connection)

    def _drop(*_args, **_kwargs):
        raise imaplib.IMAP4.abort("socket error: connection closed unexpectedly")

    monkeypatch.setattr(connection, "search", _drop)
    bind_mailbox(unreachable_mailbox)

    result = await search_email(ctx, query="invoice")
    assert result.ok is False
    assert result.output["provider_status"] == ProviderStatus.NETWORK_ERROR.value
    assert "broke" in result.summary.lower()


async def test_imap_answered_and_found_nothing_is_real_empty(
    ctx: ToolContext, bind_mailbox, monkeypatch, unreachable_mailbox: ImapSmtpProvider
) -> None:
    """The one empty-inbox sentence that is allowed to be a success."""
    monkeypatch.setattr(
        unreachable_mailbox, "_imap", lambda: _StubIMAPConnection(search=("OK", [b""]))
    )
    bind_mailbox(unreachable_mailbox)
    result = await search_email(ctx, query="invoice")

    assert result.ok is True
    assert result.output["provider_status"] == ProviderStatus.REAL_EMPTY.value
    assert "found no messages" in result.summary.lower()


async def test_local_maildir_empty_inbox_is_a_real_empty(
    ctx: ToolContext, bind_mailbox, empty_local_mailbox: LocalMailProvider
) -> None:
    bind_mailbox(empty_local_mailbox)

    searched = await search_email(ctx, query="invoice")
    assert searched.ok is True
    assert searched.output["provider_status"] == ProviderStatus.REAL_EMPTY.value

    summarised = await summarize_email(ctx, limit=5)
    assert summarised.ok is True
    assert summarised.output["provider_status"] == ProviderStatus.REAL_EMPTY.value
    assert "nothing to summarise" in summarised.summary.lower()


async def test_read_email_distinguishes_rejected_credentials_from_no_server(
    ctx: ToolContext, bind_mailbox, imap_login_rejected: ImapSmtpProvider, unreachable_mailbox: ImapSmtpProvider
) -> None:
    """``read_email`` must not call both failures the same thing either."""
    bind_mailbox(imap_login_rejected)
    auth = await read_email(ctx, uid="msg-1")

    bind_mailbox(unreachable_mailbox)
    unreachable = await read_email(ctx, uid="msg-1")

    assert auth.ok is False and unreachable.ok is False
    assert auth.output["provider_status"] == ProviderStatus.AUTH_ERROR.value
    assert unreachable.output["provider_status"] == ProviderStatus.UNAVAILABLE.value
    assert "password" in auth.summary.lower()
    assert "password" not in unreachable.summary.lower()


async def test_missing_local_message_is_still_reported_as_missing(
    ctx: ToolContext, bind_mailbox, empty_local_mailbox: LocalMailProvider
) -> None:
    """A provider that answered and found nothing must not be dressed up as down."""
    bind_mailbox(empty_local_mailbox)
    result = await read_email(ctx, uid="no-such-message")

    assert result.ok is False
    assert "no-such-message" in result.summary
    assert result.output.get("provider_status") in (None, ProviderStatus.REAL_EMPTY.value)


# ---------------------------------------------------------------------------
# Hygiene: no credential may ride along in a user-facing sentence
# ---------------------------------------------------------------------------
async def test_failure_sentences_never_leak_the_calendar_password(
    ctx: ToolContext, bind_calendar, unreachable_calendar: CalDavProvider
) -> None:
    bind_calendar(unreachable_calendar)
    result = await list_events(ctx, date="today")

    assert "not-a-real-password" not in result.summary
    assert "not-a-real-password" not in str(result.output)


async def test_failure_sentences_never_leak_the_mail_password(
    ctx: ToolContext, bind_mailbox, imap_login_rejected: ImapSmtpProvider
) -> None:
    bind_mailbox(imap_login_rejected)
    result = await search_email(ctx, query="invoice")

    assert "not-a-real-password" not in result.summary
    assert "not-a-real-password" not in str(result.output)
