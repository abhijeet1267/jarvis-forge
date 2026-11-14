"""R6.4 — calendar and email, end to end: proposal → approval → execution → result.

The calendar and email *tools* exist and are unit-tested. What was never
pinned is the path a person actually takes: a request in, a proposal out, an
approval if the action is consequential, an execution, and a result that says
what really happened.

Four things are pinned here, and each is a claim the product makes:

1. **A consequential action asks for approval.** ``create_event`` is an
   EXTERNAL, confirmation-requiring tool. A request to create a meeting must
   not quietly create one; it must come back as a decision for the person.
2. **A rejected action does not execute.** Answering "no" must leave the
   calendar exactly as it was.
3. **The result is verified where possible, and uncertainty is reported
   honestly.** ``create_event`` now reads the calendar back and confirms the
   event by its id. If the read-back fails, or the event is not there, the
   result says so — it does not claim a success it did not observe.
4. **The same shape for email**: search, read, summarise, and identify the
   requested item — with a draft that is not a send, and a send that is.
"""

from __future__ import annotations

import asyncio
from email.message import EmailMessage as StdEmailMessage
from pathlib import Path
from typing import Any, Dict, List

import pytest

from jarvis.core.models import (
    CommandRequest,
    PolicyDecision,
    Verification,
)
from jarvis.tools.base import ToolContext
from jarvis.integrations.email_provider import LocalMailProvider


@pytest.fixture()
def ctx() -> ToolContext:
    return ToolContext()


@pytest.fixture()
def mailbox(tmp_area: Path, monkeypatch):
    """A local maildir seeded with a couple of messages.

    The same shape the email unit tests use: a real provider over a real
    maildir, so the acceptance path reads actual messages rather than a mock.
    """
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

    add("Invoice 2026-09", "billing@example.com",
        "Your invoice for September is attached. Total due: 42.00 EUR.")
    add("Build failed", "ci@builds.example", "The nightly build failed.", unread=False)

    monkeypatch.setattr(
        "jarvis.tools.email.get_email_provider", lambda settings=None: provider
    )
    return provider, add


# ---------------------------------------------------------------------------
# 1. A consequential action asks for approval
# ---------------------------------------------------------------------------
def test_creating_an_event_asks_for_approval_and_creates_nothing(core):
    """The whole point of the gate: a meeting is not created until it is approved.

    Before the approval is given, the calendar must be untouched. A test that
    only checked "the response asked for confirmation" would pass even if the
    event had been created on the way past.
    """
    from jarvis.integrations.calendar_provider import get_calendar_provider
    from jarvis.config import get_settings
    from datetime import datetime, timedelta

    provider = get_calendar_provider(get_settings())
    start = datetime.now().astimezone().replace(microsecond=0) + timedelta(days=1)
    before = len(provider.list_events(start - timedelta(days=1), start + timedelta(days=1)))

    response = asyncio.run(core.handle(CommandRequest(
        text=f"create a calendar event called Standup on {start:%Y-%m-%d %H:%M}",
        source="r64",
        session_id="R64-1",
    )))

    assert response.decision is PolicyDecision.CONFIRM, (
        f"a calendar write must be gated; decision was {response.decision}"
    )
    assert response.needs_confirmation is True
    assert response.confirmation_id, "a pending approval needs an id to answer"
    assert response.actions == [], "nothing may run before the approval"

    after = len(provider.list_events(start - timedelta(days=1), start + timedelta(days=1)))
    assert after == before, "the event was created before it was approved"


def test_approving_the_request_then_creates_the_event(core):
    """The full path: the request, the approval, the event, the confirmation."""
    from jarvis.integrations.calendar_provider import get_calendar_provider
    from jarvis.config import get_settings
    from datetime import datetime, timedelta

    provider = get_calendar_provider(get_settings())
    start = datetime.now().astimezone().replace(microsecond=0) + timedelta(days=2)

    response = asyncio.run(core.handle(CommandRequest(
        text=f"create a calendar event called Review on {start:%Y-%m-%d %H:%M}",
        source="r64",
        session_id="R64-2",
    )))
    assert response.decision is PolicyDecision.CONFIRM, response.decision
    confirmation_id = response.confirmation_id

    approved = asyncio.run(core.resolve_confirmation(
        confirmation_id, approve=True, session_id="R64-2"
    ))

    assert approved.decision is PolicyDecision.EXECUTE, approved.decision
    assert approved.actions, "the approved action did not run"
    result = approved.actions[0]
    assert result.ok, result.error
    # The read-back confirmed it, so the result may say "verified".
    assert result.verification is Verification.SUCCESS, (
        f"the event was created and read back, so the result should be verified; "
        f"got {result.verification} ({result.verification_note})"
    )

    # And it really is in the calendar.
    window = provider.list_events(start - timedelta(days=1), start + timedelta(days=1))
    titles = [e.title for e in window]
    assert "Review" in titles, titles


def test_rejecting_the_request_creates_nothing(core):
    """A refused approval must leave the world exactly as it was."""
    from jarvis.integrations.calendar_provider import get_calendar_provider
    from jarvis.config import get_settings
    from datetime import datetime, timedelta

    provider = get_calendar_provider(get_settings())
    start = datetime.now().astimezone().replace(microsecond=0) + timedelta(days=3)
    before = list(provider.list_events(start - timedelta(days=1), start + timedelta(days=1)))

    response = asyncio.run(core.handle(CommandRequest(
        text=f"create a calendar event called Cancelled on {start:%Y-%m-%d %H:%M}",
        source="r64",
        session_id="R64-3",
    )))
    assert response.decision is PolicyDecision.CONFIRM, response.decision

    denied = asyncio.run(core.resolve_confirmation(
        response.confirmation_id, approve=False, session_id="R64-3"
    ))
    assert denied.decision is PolicyDecision.BLOCK, denied.decision
    assert denied.actions == [], "a rejected action must not run"

    after = list(provider.list_events(start - timedelta(days=1), start + timedelta(days=1)))
    assert len(after) == len(before), "a rejected event was created anyway"


# ---------------------------------------------------------------------------
# 2. The result is verified where possible, and uncertainty is honest
# ---------------------------------------------------------------------------
def test_the_created_event_is_confirmed_by_reading_the_calendar_back(core):
    """The confirmation is an observation, not the provider's own word.

    The tool's result carries the id the calendar gave it. The event is then
    looked up by that id. This is the difference between "the provider said it
    created it" and "it is in the calendar".
    """
    from jarvis.integrations.calendar_provider import get_calendar_provider
    from jarvis.config import get_settings
    from datetime import datetime, timedelta

    provider = get_calendar_provider(get_settings())
    start = datetime.now().astimezone().replace(microsecond=0) + timedelta(days=4)

    response = asyncio.run(core.handle(CommandRequest(
        text=f"create a calendar event called Verified on {start:%Y-%m-%d %H:%M}",
        source="r64",
        session_id="R64-4",
    )))
    approved = asyncio.run(core.resolve_confirmation(
        response.confirmation_id, approve=True, session_id="R64-4"
    ))
    result = approved.actions[0]
    assert result.verification is Verification.SUCCESS, result.verification_note

    uid = (result.output.get("event") or {}).get("uid")
    assert uid, f"the result should name the event it created: {result.output}"
    window = provider.list_events(start - timedelta(days=1), start + timedelta(days=1))
    assert any(e.uid == uid for e in window), "the confirmed event is not in the calendar"


def test_a_calendar_that_writes_but_cannot_be_read_back_is_unverified_not_successful(monkeypatch):
    """A read-back that fails is uncertainty, and it is reported as uncertainty.

    The provider accepted the write, and then the confirmation read broke. The
    honest answer is "I could not confirm it" — not "created", and not
    "failed", because the write may well have happened.
    """
    from jarvis.tools import calendar as cal
    from jarvis.integrations.calendar_provider import CalendarEvent

    class _Provider:
        name = "local_calendar"

        def create(self, event: CalendarEvent):
            event.uid = "EVT-made-up"
            return {"ok": True, "provider": self.name, "uid": event.uid,
                    "event": event.as_dict()}

        def list_events(self, start, end, limit=50):
            raise RuntimeError("the calendar could not be read")

    monkeypatch.setattr(cal, "get_calendar_provider", lambda settings=None: _Provider())

    result = asyncio.run(cal.create_event(
        ToolContext(), title="Unconfirmed", start="2026-09-21 17:00"
    ))
    assert result.ok, "the write itself was accepted"
    assert result.verification is Verification.UNKNOWN, (
        f"an unreadable calendar must leave the outcome unestablished; got {result.verification}"
    )
    assert "could not confirm" in result.verification_note.lower(), result.verification_note


def test_an_accepted_event_that_is_not_there_is_not_a_success(monkeypatch):
    """A provider that claims a write it did not do is caught by the read-back."""
    from jarvis.tools import calendar as cal
    from jarvis.integrations.calendar_provider import (
        CalendarEvent,
        ProviderResults,
        ProviderStatus,
    )

    class _Provider:
        name = "local_calendar"

        def create(self, event: CalendarEvent):
            event.uid = "EVT-claimed"
            return {"ok": True, "provider": self.name, "uid": event.uid,
                    "event": event.as_dict()}

        def list_events(self, start, end, limit=50):
            # The write was accepted, but nothing is there.
            return ProviderResults([], ProviderStatus.REAL_EMPTY)

    monkeypatch.setattr(cal, "get_calendar_provider", lambda settings=None: _Provider())

    result = asyncio.run(cal.create_event(
        ToolContext(), title="Phantom", start="2026-09-21 17:00"
    ))
    assert result.verification is not Verification.SUCCESS, (
        "an event that is not in the calendar must not be reported as verified"
    )
    assert "cannot confirm" in result.verification_note.lower(), result.verification_note


# ---------------------------------------------------------------------------
# 3. Invalid input is a question, not a guess
# ---------------------------------------------------------------------------
def test_an_event_with_no_title_asks_rather_than_creating_one(core):
    """A missing required detail is a question, not a guess and not a wall.

    Two wrong answers are ruled out here:

    * inventing a title ("Meeting") and asking the user to approve an event
      nobody named — the approval prompt shows that invented name as if the
      person had chosen it;
    * blocking the request with "missing required parameter 'title'" — a
      parameter name, not a question the person can answer.

    The answer has to be a *question*: something the person can reply to.
    """
    response = asyncio.run(core.handle(CommandRequest(
        text="create a calendar event tomorrow",
        source="r64",
        session_id="R64-5",
    )))
    assert response.actions == [], "an event was created from an incomplete request"
    assert response.needs_confirmation is False, (
        "a missing detail is a question, not a pending approval"
    )
    reply = (response.reply or "").lower()
    # A question, in words a person can answer.
    assert "?" in reply, f"the reply should ask a question, got: {response.reply!r}"
    assert "call it" in reply or "what should" in reply, (
        f"the reply should ask what to call the event, got: {response.reply!r}"
    )
    # And certainly not a parameter name.
    assert "missing required parameter" not in reply, (
        f"the reply should be a question, not a parameter error: {response.reply!r}"
    )


def test_an_unreadable_start_time_is_reported_not_guessed(core):
    """A start time that cannot be understood must not become a guess."""
    from jarvis.tools.base import ToolContext
    from jarvis.tools import calendar as cal

    result = asyncio.run(cal.create_event(
        ToolContext(), title="Nonsense", start="not-a-time"
    ))
    assert not result.ok, "an unparseable time was accepted"
    assert "could not understand" in result.error.lower(), result.error


def test_an_end_before_the_start_is_refused(core):
    """An impossible event is refused, not created."""
    from jarvis.tools.base import ToolContext
    from jarvis.tools import calendar as cal

    result = asyncio.run(cal.create_event(
        ToolContext(), title="Backwards", start="2026-09-21 17:00", end="2026-09-21 16:00"
    ))
    assert not result.ok, "an event ending before it starts was accepted"
    assert "before the start" in result.error.lower(), result.error


# ---------------------------------------------------------------------------
# 4. Email: the same shape
# ---------------------------------------------------------------------------
def test_email_search_read_and_summarise_work_through_the_tools(ctx, mailbox):
    """Search finds it, read returns the body, summarise returns a summary."""
    from jarvis.tools.email import search_email, read_email, summarize_email

    found = asyncio.run(search_email(ctx, query="invoice"))
    assert found.ok, found.error
    results = found.output.get("results") or []
    assert results, found.output

    message_id = results[0].get("uid")
    assert message_id, found.output

    read = asyncio.run(read_email(ctx, uid=message_id))
    assert read.ok, read.error
    body = (read.output.get("email") or {}).get("body") or ""
    assert "invoice" in body.lower(), read.output

    summary = asyncio.run(summarize_email(ctx, query="invoice"))
    assert summary.ok, summary.error
    items = summary.output.get("items") or []
    assert items, summary.output
    assert any("invoice" in str(i.get("subject", "")).lower() for i in items), items


def test_a_draft_is_not_a_send(ctx, mailbox):
    """A draft is a draft. The distinction is the whole point of a draft."""
    from jarvis.tools.email import draft_email

    drafted = asyncio.run(draft_email(
        ctx, to="team@example.com", subject="Status", body="All good."
    ))
    assert drafted.ok, drafted.error
    assert drafted.output.get("draft_id") or drafted.output.get("id"), drafted.output
    # A draft does not put a message in the sent folder.
    assert not drafted.output.get("sent"), drafted.output
    assert drafted.output.get("sent") is not True, drafted.output


def test_a_send_is_a_send_and_is_reported_as_such(ctx, mailbox):
    """The contrast: a sent message is a sent message."""
    from jarvis.tools.email import draft_email, send_email

    drafted = asyncio.run(draft_email(
        ctx, to="team@example.com", subject="Status", body="All good."
    ))
    draft_id = drafted.output.get("draft_id") or drafted.output.get("id")
    sent = asyncio.run(send_email(ctx, draft_id=draft_id))
    assert sent.ok, sent.error
    assert sent.output.get("sent") is True, sent.output
