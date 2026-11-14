"""The email *find* step must search for the thing the person named.

**The search term was dropped, so a search returned whatever was recent.**

Measured on the live tree, in the running orchestrator against a seeded
local maildir:

    "find emails about the invoice"     -> search_email(query="")
    "search my email for invoice"       -> search_email(query="")
    "find the email about the Q3 report" -> search_email(query="")

``query=""`` means *recent messages* to the provider, so the first step of the
email workflow -- finding the message the person asked for -- answered with
the last few messages whatever they were about. Only an end-anchored
``from <name>`` and the word ``unread`` survived; every "about X" / "for X"
phrase was read as a request to list the mailbox.

The rules pinned here:

* a search phrase carries the thing it searches for, in the words the person
  used, so the mailbox is searched rather than listed;
* a request that names no thing still lists the mailbox -- the honest default
  for "check my email" and "what new emails did I get" is unchanged;
* a message named in a *read* request is resolved by the identifier lookup
  the calendar cancel path already uses: exactly one match is read, several
  matches are asked about, and a miss is reported -- never a guess is read;
* the reply names the message(s) it found, so the person can identify the
  right one from the transcript, not just from a count.
"""

from __future__ import annotations

import asyncio
from email.message import EmailMessage as StdEmailMessage
from pathlib import Path

import pytest

from jarvis.core.models import CommandRequest, PolicyDecision
from jarvis.core.planner import RulePlanner
from jarvis.integrations.email_provider import LocalMailProvider


def _run(core, text: str, session_id: str = "r92"):
    return asyncio.run(core.handle(CommandRequest(
        text=text, source="r92", session_id=session_id)))


def _parse(core, text: str):
    return core.planner.parse(text)


@pytest.fixture()
def mailbox(tmp_area: Path, monkeypatch):
    """A local maildir seeded with a few messages.

    Two messages share a word ("update") so the several-match case is real,
    and the subjects are distinctive so a unique match identifies one.
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
    add("Product update", "pm@example.com", "The product update shipped on Friday.")
    add("System update", "ops@example.com", "A system update is required by Monday.")
    add("Build failed", "ci@builds.example", "The nightly build failed.", unread=False)

    monkeypatch.setattr(
        "jarvis.tools.email.get_email_provider", lambda settings=None: provider
    )
    return provider


# ---------------------------------------------------------------------------
# 1. the search term is carried, not dropped
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,query", [
    ("find emails about the invoice", "invoice"),
    ("search my email for invoice", "invoice"),
    ("search email for the budget", "budget"),
    ("find emails about project proposal", "project proposal"),
    ("show me emails from the billing team", "billing team"),
    ("find my email about the Q3 report", "q3 report"),
])
def test_a_search_phrase_searches_for_the_thing_named(core, text, query):
    """The words that say *what* is the query, not a request to list the inbox."""
    plan = _parse(core, text)
    actions = [a for a in plan.actions if a.tool == "search_email"]
    assert actions, f"{text!r} did not plan a search: {[a.tool for a in plan.actions]}"
    # The provider lowercases the needle (local maildir: `needle.lower()`;
    # IMAP SEARCH matches case-insensitively), so the contract is the *words*,
    # not their case.
    assert actions[0].params.get("query", "").lower() == query, (
        f"{text!r} searched for {actions[0].params.get('query')!r}, expected {query!r}"
    )


def test_a_search_for_a_thing_is_not_a_file_search(core):
    """Carrying a term must not hand the sentence to search_files."""
    plan = _parse(core, "find emails about the invoice")
    assert "search_files" not in [a.tool for a in plan.actions], plan.actions


def test_a_search_that_names_no_thing_still_lists_the_mailbox(core):
    """"check my email" names no term; the honest default is a broad search."""
    plan = _parse(core, "check my email")
    action = plan.actions[0]
    assert action.tool == "search_email"
    assert action.params.get("query") == "", action.params


def test_the_unread_route_is_unchanged(core):
    """Recency wording still narrows to unread, and still names no term."""
    for text in ("check my unread emails", "what new emails did I get"):
        plan = _parse(core, text)
        action = plan.actions[0]
        assert action.tool == "search_email", (text, plan.actions)
        assert action.params.get("unread_only") is True, (text, action.params)
        assert action.params.get("query") == "", (text, action.params)


def test_read_my_email_from_a_person_still_searches(core):
    """The pinned collision matrix: from-<name> is a search, not a read."""
    plan = _parse(core, "read my email from Alice")
    action = plan.actions[0]
    assert action.tool == "search_email", plan.actions
    assert action.params.get("query") == "Alice", action.params


# ---------------------------------------------------------------------------
# 2. the reply names the message(s) it found
# ---------------------------------------------------------------------------

def test_a_search_reply_names_the_message_it_found(core, mailbox):
    """A count alone does not let the person pick the right message."""
    response = _run(core, "find emails about the invoice")
    assert response.actions, response.reply
    assert "Invoice 2026-09" in response.reply, response.reply
    assert "billing@example.com" in response.reply, response.reply


def test_a_broad_search_reply_names_the_recent_messages(core, mailbox):
    response = _run(core, "check my email")
    assert response.actions, response.reply
    # At least one of the seeded subjects is named, so the list is usable.
    assert any(s in response.reply for s in ("Invoice 2026-09", "Product update",
                                             "System update", "Build failed")), response.reply


def test_a_summarise_reply_names_the_messages(core, mailbox):
    response = _run(core, "summarize my inbox")
    assert response.actions, response.reply
    assert any(s in response.reply for s in ("Invoice 2026-09", "Product update",
                                             "System update", "Build failed")), response.reply


# ---------------------------------------------------------------------------
# 3. a message named in a read request is resolved, not guessed
# ---------------------------------------------------------------------------

def test_reading_a_unique_named_message_reads_that_message(core, mailbox):
    """One match: the person's words identify it, so it is read end to end."""
    response = _run(core, "read the invoice email")
    actions = [a for a in response.plan.actions if a.tool == "read_email"]
    assert actions, f"no read was planned: {[(a.tool, a.params) for a in response.plan.actions]}"
    assert actions[0].params.get("uid"), "the read was not given the message's id"
    assert response.actions and response.actions[0].ok is True, response.reply
    assert "Invoice 2026-09" in response.reply, response.reply


def test_reading_a_message_that_matches_several_asks(core, mailbox):
    """Two messages carry the word "update"; neither is identified by it."""
    response = _run(core, "read the update email")
    assert response.plan.reply_kind == "clarification", response.reply
    assert not any(a.tool == "read_email" for a in response.plan.actions), response.plan
    # Both candidates are named, so the person can pick.
    assert "Product update" in response.reply, response.reply
    assert "System update" in response.reply, response.reply


def test_reading_a_named_message_that_matches_nothing_says_so(core, mailbox):
    """A miss is reported; a message is not read on a guess."""
    response = _run(core, "read the zebra email")
    assert response.plan.reply_kind == "clarification", response.reply
    assert not any(a.tool == "read_email" for a in response.plan.actions), response.plan
    assert "nothing of that kind" in response.reply, response.reply
    assert response.plan.resolved_identifier == {}, response.plan


def test_reading_with_no_name_is_not_an_invented_read(core, mailbox):
    """"read my email" names no message; the mailbox is listed, not a read."""
    response = _run(core, "read my email")
    actions = [a.tool for a in response.plan.actions]
    assert "read_email" not in actions, response.plan
    assert "search_email" in actions, response.plan
