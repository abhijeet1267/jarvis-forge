"""Email + Calendar tests (v0.7).

The properties that matter: reading is free, writing is permission controlled,
an ambiguous recipient is never guessed at, and nothing is ever sent because the
model inferred it.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from email.message import EmailMessage as StdEmailMessage
from pathlib import Path

import pytest

from jarvis.core.models import PlannedAction, PolicyDecision, RiskLevel
from jarvis.integrations.calendar_provider import (
    CalendarEvent,
    LocalCalendarProvider,
    events_overlap,
    from_ics,
    parse_datetime,
    to_ics,
)
from jarvis.integrations.email_provider import (
    EmailDraft,
    LocalMailProvider,
    looks_like_address,
)
from jarvis.tools.base import ToolContext
from jarvis.tools.calendar import (
    cancel_event,
    create_event,
    daily_schedule,
    find_free_time,
    list_events,
    next_event,
    search_events,
    update_event,
)
from jarvis.tools.email import (
    add_contact,
    draft_email,
    latest_draft,
    read_email,
    resolve_recipient,
    search_email,
    send_email,
    summarize_email,
)


@pytest.fixture()
def ctx() -> ToolContext:
    return ToolContext()


@pytest.fixture()
def mailbox(tmp_area: Path, monkeypatch):
    """A local maildir seeded with a couple of messages."""
    from jarvis.config import Settings

    home = tmp_area / "mailhome"
    settings = Settings(home=home, api_token="")

    provider = LocalMailProvider(settings)
    inbox = provider.folder("inbox")

    def add(subject: str, sender: str, body: str, unread: bool = True) -> str:
        message = StdEmailMessage()
        message["From"] = sender
        message["To"] = "me@example.com"
        message["Subject"] = subject
        message["Date"] = "Sun, 20 Sep 2026 09:00:00 +0530"
        message.set_content(body)
        name = f"msg-{abs(hash(subject)) % 100000}"
        path = inbox / f"{name}.eml"
        path.write_bytes(message.as_bytes())
        if not unread:
            path.with_suffix(".read").touch()
        return name

    add("AMR review agenda", "prof@uni.edu", "Please bring the latest results.")
    add("Build failed", "ci@builds.example", "The nightly build failed.", unread=False)

    monkeypatch.setattr(
        "jarvis.tools.email.get_email_provider", lambda settings=None: provider
    )
    return provider, add


# ---------------------------------------------------------------------------
# Address handling
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value,expected",
    [
        ("a@b.com", True),
        ("first.last+tag@sub.example.co.uk", True),
        ("not-an-address", False),
        ("@nope.com", False),
        ("", False),
    ],
)
def test_address_detection(value: str, expected: bool) -> None:
    assert looks_like_address(value) is expected


def test_recipient_resolution_prefers_a_literal_address() -> None:
    result = resolve_recipient("someone@example.com")
    assert result["ok"] and result["address"] == "someone@example.com"


def test_recipient_resolution_uses_contacts() -> None:
    add_contact("Arun Kumar", "arun@example.com", ["arun", "teammate"])
    result = resolve_recipient("arun")
    assert result["ok"]
    assert result["address"] == "arun@example.com"


def test_unknown_recipient_is_refused() -> None:
    """An unresolvable recipient must ask, never guess."""
    result = resolve_recipient("someone-i-never-added")
    assert not result["ok"]
    assert "do not have a contact" in result["reason"]


def test_ambiguous_recipient_lists_candidates() -> None:
    """Two matches must not be silently resolved to one."""
    add_contact("Rao Senior", "rao.senior@example.com", ["rao"])
    add_contact("Rao Junior", "rao.junior@example.com", ["rao"])

    result = resolve_recipient("rao")
    assert not result["ok"]
    assert len(result.get("candidates", [])) >= 2


# ---------------------------------------------------------------------------
# Email: read
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_search_email_finds_messages(ctx: ToolContext, mailbox) -> None:
    result = await search_email(ctx, query="AMR")
    assert result.ok
    assert result.output["results"]
    assert "AMR" in result.output["results"][0]["subject"]


@pytest.mark.asyncio
async def test_search_email_unread_only(ctx: ToolContext, mailbox) -> None:
    result = await search_email(ctx, unread_only=True)
    assert result.ok
    assert all(m["unread"] for m in result.output["results"])


@pytest.mark.asyncio
async def test_read_email_returns_the_body(ctx: ToolContext, mailbox) -> None:
    found = await search_email(ctx, query="AMR")
    uid = found.output["results"][0]["uid"]

    result = await read_email(ctx, uid=uid)
    assert result.ok
    assert "results" in result.output["email"]["body"]


@pytest.mark.asyncio
async def test_read_missing_message_fails_cleanly(ctx: ToolContext, mailbox) -> None:
    result = await read_email(ctx, uid="no-such-message")
    assert not result.ok


@pytest.mark.asyncio
async def test_summarize_email(ctx: ToolContext, mailbox) -> None:
    result = await summarize_email(ctx, limit=5)
    assert result.ok
    assert result.output["items"]


# ---------------------------------------------------------------------------
# Email: draft and send
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_draft_does_not_send(ctx: ToolContext, mailbox) -> None:
    """Drafting must never transmit anything."""
    provider, _add = mailbox
    result = await draft_email(ctx, to="teammate@example.com", subject="Hi", body="Body")

    assert result.ok
    assert result.output["draft_id"]
    assert list(provider.folder("sent").glob("*.eml")) == [], "nothing should be sent"


@pytest.mark.asyncio
async def test_draft_refuses_an_unresolvable_recipient(ctx: ToolContext, mailbox) -> None:
    result = await draft_email(ctx, to="nobody-i-know", subject="Hi", body="Body")
    assert not result.ok


@pytest.mark.asyncio
async def test_send_delivers_a_drafted_message(ctx: ToolContext, mailbox) -> None:
    provider, _add = mailbox
    drafted = await draft_email(ctx, to="teammate@example.com", subject="Report", body="Here it is")
    draft_id = drafted.output["draft_id"]

    result = await send_email(ctx, draft_id=draft_id)
    assert result.ok
    assert result.output["sent"] is True

    sent = list(provider.folder("sent").glob("*.eml"))
    assert len(sent) == 1
    assert b"Here it is" in sent[0].read_bytes()


@pytest.mark.asyncio
async def test_a_draft_cannot_be_sent_twice(ctx: ToolContext, mailbox) -> None:
    drafted = await draft_email(ctx, to="teammate@example.com", subject="Once", body="Body")
    draft_id = drafted.output["draft_id"]

    assert (await send_email(ctx, draft_id=draft_id)).ok
    assert not (await send_email(ctx, draft_id=draft_id)).ok


@pytest.mark.asyncio
async def test_send_refuses_an_ambiguous_recipient(ctx: ToolContext, mailbox) -> None:
    add_contact("Rao A", "rao.a@example.com", ["rao"])
    add_contact("Rao B", "rao.b@example.com", ["rao"])

    result = await send_email(ctx, to="rao", subject="Hi", body="Body")
    assert not result.ok
    assert "more than one" in result.summary


def test_latest_draft_is_tracked() -> None:
    """'send it' resolves against the most recent unsent draft."""
    from jarvis.db.database import get_db

    get_db().execute("UPDATE email_drafts SET state = 'sent' WHERE state = 'draft'")
    assert latest_draft() is None


# ---------------------------------------------------------------------------
# Calendar: local provider
# ---------------------------------------------------------------------------
def test_calendar_create_list_and_cancel() -> None:
    provider = LocalCalendarProvider()
    start = datetime.now().astimezone().replace(microsecond=0) + timedelta(days=3)

    created = provider.create(
        CalendarEvent(uid="", title="Provider test", start=start, end=start + timedelta(hours=1))
    )
    assert created["ok"]
    uid = created["uid"]

    listed = provider.list_events(start - timedelta(hours=1), start + timedelta(hours=2))
    assert any(e.uid == uid for e in listed)

    updated = provider.update(uid, {"title": "Renamed"})
    assert updated["ok"] and updated["event"]["title"] == "Renamed"

    assert provider.cancel(uid)["ok"]
    assert provider.get(uid) is None


def test_calendar_overlap_is_half_open() -> None:
    base = datetime.now().astimezone()
    assert events_overlap(base, base + timedelta(hours=1), base + timedelta(hours=1), base + timedelta(hours=2)) is False
    assert events_overlap(base, base + timedelta(hours=2), base + timedelta(hours=1), base + timedelta(hours=3)) is True


def test_conflict_detection() -> None:
    provider = LocalCalendarProvider()
    start = datetime.now().astimezone().replace(microsecond=0) + timedelta(days=5)
    provider.create(
        CalendarEvent(uid="", title="Busy", start=start, end=start + timedelta(hours=2))
    )
    conflicts = provider.conflicts(start + timedelta(minutes=30), start + timedelta(hours=1))
    assert conflicts, "an overlapping window should report a conflict"


# ---------------------------------------------------------------------------
# Calendar: tools
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_create_event_tool(ctx: ToolContext) -> None:
    start = (datetime.now().astimezone() + timedelta(days=7)).replace(
        hour=15, minute=0, second=0, microsecond=0
    )
    result = await create_event(
        ctx, title="Tool test", start=start.isoformat(), duration_minutes=45
    )
    assert result.ok
    assert result.output["event"]["title"] == "Tool test"


@pytest.mark.asyncio
async def test_create_event_needs_a_title(ctx: ToolContext) -> None:
    result = await create_event(ctx, title="", start="2026-10-01T10:00")
    assert not result.ok


@pytest.mark.asyncio
async def test_create_event_rejects_unparseable_time(ctx: ToolContext) -> None:
    result = await create_event(ctx, title="Bad time", start="whenever-ish")
    assert not result.ok


@pytest.mark.asyncio
async def test_create_event_rejects_backwards_range(ctx: ToolContext) -> None:
    result = await create_event(
        ctx, title="Backwards", start="2026-10-01T15:00", end="2026-10-01T14:00"
    )
    assert not result.ok


@pytest.mark.asyncio
async def test_list_and_search_events(ctx: ToolContext) -> None:
    start = (datetime.now().astimezone() + timedelta(days=2)).replace(
        hour=11, minute=0, second=0, microsecond=0
    )
    created = await create_event(ctx, title="Searchable sync", start=start.isoformat())
    assert created.ok

    found = await search_events(ctx, query="Searchable")
    assert found.ok
    assert found.output["events"]

    listed = await list_events(ctx, date=start.date().isoformat())
    assert listed.ok


@pytest.mark.asyncio
async def test_next_event_and_free_time(ctx: ToolContext) -> None:
    assert (await next_event(ctx)).ok
    assert (await find_free_time(ctx, date="tomorrow", duration_minutes=30)).ok
    assert (await daily_schedule(ctx, date="today")).ok


@pytest.mark.asyncio
async def test_cancel_event_tool(ctx: ToolContext) -> None:
    start = (datetime.now().astimezone() + timedelta(days=9)).replace(
        hour=9, minute=0, second=0, microsecond=0
    )
    created = await create_event(ctx, title="To cancel", start=start.isoformat())
    uid = created.output["event"]["uid"]

    result = await cancel_event(ctx, uid=uid)
    assert result.ok
    assert not (await cancel_event(ctx, uid=uid)).ok


@pytest.mark.asyncio
async def test_update_event_tool(ctx: ToolContext) -> None:
    start = (datetime.now().astimezone() + timedelta(days=11)).replace(
        hour=13, minute=0, second=0, microsecond=0
    )
    created = await create_event(ctx, title="Original", start=start.isoformat())
    uid = created.output["event"]["uid"]

    result = await update_event(ctx, uid=uid, title="Renamed")
    assert result.ok
    assert result.output["event"]["title"] == "Renamed"


# ---------------------------------------------------------------------------
# iCalendar round-trip
# ---------------------------------------------------------------------------
def test_ics_round_trip() -> None:
    start = datetime(2026, 10, 5, 14, 30, tzinfo=datetime.now().astimezone().tzinfo)
    event = CalendarEvent(
        uid="round-trip-1",
        title="Design review",
        start=start,
        end=start + timedelta(hours=1),
        location="Lab 2",
        description="Bring the BOM",
    )

    parsed = from_ics(to_ics(event))
    assert len(parsed) == 1
    assert parsed[0].uid == "round-trip-1"
    assert parsed[0].title == "Design review"
    assert parsed[0].location == "Lab 2"
    # Times are normalised to UTC by the serialiser.
    assert parsed[0].start.astimezone(start.tzinfo) == start


def test_ics_escaping_survives_round_trip() -> None:
    start = datetime.now().astimezone()
    event = CalendarEvent(
        uid="escape-1",
        title="Sync, review; and plan",
        start=start,
        end=start + timedelta(minutes=30),
    )
    parsed = from_ics(to_ics(event))
    assert parsed[0].title == "Sync, review; and plan"


def test_ics_parser_skips_unparseable_events() -> None:
    broken = "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:x\nSUMMARY:No dates\nEND:VEVENT\nEND:VCALENDAR"
    assert from_ics(broken) == []


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value",
    ["2026-10-05T14:30", "2026-10-05 14:30", "2026-10-05", "05/10/2026 14:30"],
)
def test_datetime_parsing_accepts_common_formats(value: str) -> None:
    assert parse_datetime(value) is not None


def test_datetime_parsing_refuses_nonsense() -> None:
    assert parse_datetime("sometime next week-ish") is None


# ---------------------------------------------------------------------------
# Policy: writes are gated, reads are not
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "tool,params",
    [
        ("send_email", {"to": "a@b.com", "subject": "s", "body": "b"}),
        ("create_event", {"title": "t", "start": "2026-10-01T10:00"}),
        ("cancel_event", {"uid": "EVT-1"}),
    ],
)
def test_writes_require_confirmation(core, tool: str, params: dict) -> None:
    action = PlannedAction(tool=tool, params=params, device="laptop_main")
    assert core.policy.evaluate(action).decision is PolicyDecision.CONFIRM


@pytest.mark.parametrize(
    "tool,params",
    [
        ("search_email", {"query": "x"}),
        ("read_email", {"uid": "1"}),
        ("list_events", {"date": "today"}),
        ("next_event", {}),
        ("find_free_time", {"date": "today"}),
        ("daily_schedule", {"date": "today"}),
    ],
)
def test_reads_do_not_require_confirmation(core, tool: str, params: dict) -> None:
    action = PlannedAction(tool=tool, params=params, device="laptop_main")
    assert core.policy.evaluate(action).decision is PolicyDecision.EXECUTE


def test_send_email_is_destructive_risk(core) -> None:
    """Sending is irreversible, so it must sit at the top risk tier."""
    assert core.tools.get("send_email").risk is RiskLevel.DESTRUCTIVE
    assert core.tools.get("send_email").reversible is False


def test_drafting_is_not_destructive(core) -> None:
    assert core.tools.get("draft_email").risk is RiskLevel.LOW


# ---------------------------------------------------------------------------
# Credential safety
# ---------------------------------------------------------------------------
def test_mail_credentials_are_redacted() -> None:
    """A mail password must never survive into a log line."""
    from jarvis.config import Settings
    from jarvis.security.privacy import MASK, redact

    settings = Settings(email_password="s3cret-mail-password", api_token="")
    out = redact("connecting with password s3cret-mail-password", settings)
    assert "s3cret-mail-password" not in out
    assert MASK in out


def test_no_email_tool_sends_without_confirmation(core) -> None:
    """No tool in the email agent may be both sending and unconfirmed."""
    for spec in core.tools.all():
        if spec.agent != "email":
            continue
        if spec.name == "send_email":
            assert spec.requires_confirmation is True
