"""A follow-up points at the message the conversation just dealt with.

**"Read that one" had nothing to point at.**

The email workflow could find a message and read it by name, but a follow-up
-- "summarize that one", "read the second one", "what did they say in that
message?" -- named nothing, and the pipeline had no memory of what the
previous turn had just shown. The sentence either fell to "I understood the
words but not the goal" or, worse, to a fresh search of nothing.

The fix keeps a **per-session selection** -- the message a conversation just
dealt with -- in `email_selections`, keyed by the same `session_id` the
messages, confirmations and audit rows use. A demonstrative is carried by the
planner as a `referent` and resolved by the orchestrator against the
session's own row:

* a search that found one message selects it;
* a successful read selects what it actually read;
* a search that found several keeps the list but selects none, so an ordinal
  picks from the list the reply showed and a bare "that one" asks which;
* a search that found nothing clears the selection;
* a fresh session has no row and gets an honest "there is no email to point
  at" -- never another session's message and never a guessed read.

Only a uid, a subject and a sender are stored; the body is always re-fetched
through the read path.
"""

from __future__ import annotations

import asyncio
from email.message import EmailMessage as StdEmailMessage
from pathlib import Path

import pytest

from jarvis.core.models import CommandRequest
from jarvis.integrations.email_provider import LocalMailProvider


def _run(core, text: str, session_id: str):
    return asyncio.run(core.handle(CommandRequest(
        text=text, source="r93", session_id=session_id)))


def _read_action(response):
    """The read_email action a response planned, if any."""
    for action in response.plan.actions:
        if action.tool == "read_email":
            return action
    return None


@pytest.fixture()
def mailbox(tmp_area: Path, monkeypatch):
    """A local maildir with distinct, identifiable messages.

    Two messages share a word ("update") so the several-match and ordinal
    cases are real; the rest are unique.
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
    add("Q3 report due", "boss@example.com", "Please send the Q3 report by Friday.")
    add("Product update", "pm@example.com", "The product update shipped on Friday.")
    add("System update", "ops@example.com", "A system update is required by Monday.")

    monkeypatch.setattr(
        "jarvis.tools.email.get_email_provider", lambda settings=None: provider
    )
    return provider


# ---------------------------------------------------------------------------
# 1. selection is established from the real results
# ---------------------------------------------------------------------------

def test_a_unique_search_selects_that_message(core, mailbox):
    response = _run(core, "find my email about the invoice", "r93-sel1")
    assert response.actions, response.reply
    selection = core.email_context.get("r93-sel1")
    assert selection is not None
    assert selection.uid, "a unique search must record the message id"
    assert "Invoice 2026-09" in selection.subject


def test_a_successful_read_selects_what_it_read(core, mailbox):
    _run(core, "find my email about the invoice", "r93-sel2")
    response = _run(core, "read that one", "r93-sel2")
    assert response.actions and response.actions[0].ok is True, response.reply
    selection = core.email_context.get("r93-sel2")
    assert selection.uid, "a read must leave the message it read selected"
    assert "Invoice 2026-09" in selection.subject


def test_a_several_match_search_keeps_the_list_but_selects_none(core, mailbox):
    response = _run(core, "find my emails about update", "r93-sel3")
    assert response.actions, response.reply
    selection = core.email_context.get("r93-sel3")
    assert selection is not None
    assert not selection.uid, "several matches must not pick one on their own"
    assert len(selection.candidates) == 2, selection.candidates


def test_an_empty_search_clears_the_selection(core, mailbox):
    _run(core, "find my email about the invoice", "r93-sel4")
    _run(core, "find my email about a zebra that is not here", "r93-sel4")
    selection = core.email_context.get("r93-sel4")
    assert selection is None or (not selection.uid and not selection.candidates), (
        "a search that matched nothing must not leave a stale pointer standing")


def test_a_failed_read_does_not_touch_the_selection(core, mailbox):
    _run(core, "find my email about the invoice", "r93-sel5")
    before = core.email_context.get("r93-sel5")
    _run(core, "read the zebra email", "r93-sel5")
    after = core.email_context.get("r93-sel5")
    assert after.uid == before.uid, "a failed lookup must not move the selection"


# ---------------------------------------------------------------------------
# 2. the follow-up resolves to the right message
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("followup", [
    "read that one",
    "summarize that one",
    "read that email",
    "what did they say in that message?",
    "show me that email",
])
def test_the_follow_up_reads_the_selected_message(core, mailbox, followup):
    _run(core, "find my email about the invoice", "r93-f1")
    selected = core.email_context.get("r93-f1").uid
    response = _run(core, followup, "r93-f1")
    action = _read_action(response)
    assert action is not None, f"{followup!r} planned no read: {response.reply}"
    assert action.params.get("uid") == selected, (
        f"{followup!r} read {action.params.get('uid')!r}, the selection was {selected!r}")
    assert response.actions and response.actions[0].ok is True, response.reply
    assert "Invoice 2026-09" in response.reply, response.reply


def test_the_follow_up_uses_the_real_uid_not_a_guess(core, mailbox):
    """The uid that reaches the tool is the one the mailbox issued."""
    _run(core, "find my email about the invoice", "r93-f2")
    selection = core.email_context.get("r93-f2")
    # The provider mints the uid from the file name; the tool must receive it
    # verbatim, not a re-derivation from the subject.
    response = _run(core, "read that one", "r93-f2")
    assert _read_action(response).params["uid"] == selection.uid


def test_an_ordinal_picks_from_the_list_the_reply_showed(core, mailbox):
    _run(core, "find my emails about update", "r93-f3")
    selection = core.email_context.get("r93-f3")
    second = selection.candidates[1]
    response = _run(core, "read the second one", "r93-f3")
    action = _read_action(response)
    assert action is not None, response.reply
    assert action.params["uid"] == second["uid"], (
        "the ordinal must resolve against the list the reply showed, in order")
    assert response.actions[0].ok is True, response.reply


def test_a_bare_referent_over_a_multi_match_asks_which_one(core, mailbox):
    """Two matches and a bare 'that one' is still ambiguous; asking is the fix."""
    _run(core, "find my emails about update", "r93-f4")
    response = _run(core, "read that one", "r93-f4")
    assert _read_action(response) is None, "an ambiguous referent must not read a message"
    assert "more than one" in response.reply, response.reply
    # Both candidates are named so the person can pick by number or name.
    assert "Product update" in response.reply, response.reply
    assert "System update" in response.reply, response.reply


def test_an_ordinal_past_the_list_is_reported(core, mailbox):
    _run(core, "find my emails about update", "r93-f5")
    response = _run(core, "read the fifth one", "r93-f5")
    assert _read_action(response) is None
    assert "no fifth one" in response.reply, response.reply


# ---------------------------------------------------------------------------
# 3. missing, stale and foreign context
# ---------------------------------------------------------------------------

def test_a_referent_with_no_selection_says_so_and_reads_nothing(core, mailbox):
    response = _run(core, "summarize that one", "r93-m1")
    assert _read_action(response) is None, "nothing was selected; nothing may be read"
    assert "no email in this conversation" in response.reply, response.reply


def test_a_fresh_session_does_not_inherit_another_sessions_message(core, mailbox):
    """The isolation rule: a new conversation starts with no selection."""
    _run(core, "find my email about the invoice", "r93-m2a")
    other = core.email_context.get("r93-m2a").uid
    response = _run(core, "read that one", "r93-m2b")
    action = _read_action(response)
    assert action is None or action.params.get("uid") != other, (
        "a fresh session must not resolve a referent against another session's selection")
    assert "no email in this conversation" in response.reply, response.reply


def test_interleaved_sessions_each_resolve_their_own_message(core, mailbox):
    """A and B select different messages; each follow-up stays in its session."""
    _run(core, "find my email about the invoice", "r93-m3a")
    _run(core, "find my email about the q3 report", "r93-m3b")
    uid_a = core.email_context.get("r93-m3a").uid
    uid_b = core.email_context.get("r93-m3b").uid
    assert uid_a != uid_b

    response_a = _run(core, "read that one", "r93-m3a")
    response_b = _run(core, "read that one", "r93-m3b")
    assert _read_action(response_a).params["uid"] == uid_a
    assert _read_action(response_b).params["uid"] == uid_b
    assert "Invoice 2026-09" in response_a.reply
    assert "Q3 report due" in response_b.reply


def test_a_referent_to_a_deleted_message_is_reported_not_invented(core, mailbox):
    """The message went away after the selection was recorded."""
    _run(core, "find my email about the q3 report", "r93-m4")
    uid = core.email_context.get("r93-m4").uid
    inbox = mailbox.folder("inbox")
    for path in inbox.glob("*.eml"):
        if path.stem == uid:
            path.unlink()
    response = _run(core, "read that one", "r93-m4")
    action = _read_action(response)
    assert action is not None and action.params["uid"] == uid, (
        "the selection still names the message; the read must be attempted with it")
    # The read failed because the message is gone; that failure is reported.
    assert response.actions and response.actions[0].ok is False, response.reply
    assert "could not find" in response.reply.lower(), response.reply


def test_a_bare_referent_with_only_a_list_left_asks(core, mailbox):
    """A read that moved the pointer away from the list cannot pick a number later
    on the lost list -- but here the list is still stored, so it resolves."""
    _run(core, "find my emails about update", "r93-m5")
    _run(core, "read the first one", "r93-m5")
    response = _run(core, "read the second one", "r93-m5")
    action = _read_action(response)
    assert action is not None, response.reply
    assert action.params["uid"], "the second candidate must resolve from the stored list"


# ---------------------------------------------------------------------------
# 5. a reply continues the workflow with the selected message
# ---------------------------------------------------------------------------

def test_a_reply_to_the_selected_message_drafts_to_the_sender(core, mailbox):
    """The recipient is the sender the selection recorded; the result is a draft."""
    _run(core, "find my email about the invoice", "r93-d1")
    response = _run(core, "reply to that one", "r93-d1")
    drafts = [a for a in response.plan.actions if a.tool == "draft_email"]
    assert drafts, f"no draft planned: {[(a.tool, a.params) for a in response.plan.actions]}"
    assert drafts[0].params["to"] == "billing@example.com", drafts[0].params
    # A draft is not a send.
    assert response.actions and response.actions[0].ok is True, response.reply
    assert response.actions[0].tool == "draft_email"
    assert response.actions[0].output.get("sent") is not True
    assert "Nothing has been sent" in response.reply, response.reply


def test_a_reply_uses_the_address_from_a_display_name_sender(core, mailbox):
    """'Billing Dept <billing@example.com>' names the address in the angle brackets."""
    from jarvis.core.orchestrator import JarvisCore

    assert JarvisCore._address_from("Billing Dept <billing@example.com>") == "billing@example.com"
    assert JarvisCore._address_from("plain@example.com") == "plain@example.com"
    assert JarvisCore._address_from("no address here") == ""


def test_a_reply_to_an_ordinal_uses_that_candidates_sender(core, mailbox):
    _run(core, "find my emails about update", "r93-d2")
    selection = core.email_context.get("r93-d2")
    second = selection.candidates[1]
    response = _run(core, "reply to the second one", "r93-d2")
    drafts = [a for a in response.plan.actions if a.tool == "draft_email"]
    assert drafts, response.reply
    assert drafts[0].params["to"] in (second.get("from") or ""), drafts[0].params


def test_a_reply_with_no_selection_says_so_and_drafts_nothing(core, mailbox):
    response = _run(core, "reply to that one", "r93-d3")
    assert not [a for a in response.plan.actions if a.tool == "draft_email"]
    assert "no email in this conversation" in response.reply, response.reply


def test_a_reply_over_a_multi_match_asks_which_one(core, mailbox):
    _run(core, "find my emails about update", "r93-d4")
    response = _run(core, "reply to that one", "r93-d4")
    assert not [a for a in response.plan.actions if a.tool == "draft_email"]
    assert "more than one" in response.reply, response.reply


def test_a_reply_to_a_sender_without_an_address_asks(core, mailbox):
    """A 'From' line with no address cannot be drafted to without a guess."""
    from jarvis.core.email_context import EmailSelection

    core.email_context.set(
        "r93-d5", EmailSelection(uid="noaddr", subject="Anonymous",
                                  sender="somebody", provider="local_maildir"))
    response = _run(core, "reply to that one", "r93-d5")
    assert not [a for a in response.plan.actions if a.tool == "draft_email"]
    assert "email address" in response.reply, response.reply


def test_a_reply_is_a_draft_not_a_send_even_when_a_draft_exists(core, mailbox):
    """'reply to that one' must not be mistaken for 'send the waiting draft'."""
    from jarvis.tools.email import draft_email, latest_draft
    from jarvis.tools.base import ToolContext

    asyncio.run(draft_email(ToolContext(), to="team@example.com",
                            subject="Status", body="All good."))
    pending = latest_draft()
    assert pending, "a draft must be waiting for this test to be meaningful"
    _run(core, "find my email about the invoice", "r93-d6")
    response = _run(core, "reply to that one", "r93-d6")
    tools = [a.tool for a in response.plan.actions]
    assert "draft_email" in tools, tools
    assert "send_email" not in tools, (
        "a reply draft must never become the send of an unrelated waiting draft")
    drafted = [a for a in response.plan.actions if a.tool == "draft_email"][0]
    assert drafted.params["to"] == "billing@example.com", drafted.params


def test_a_reply_carries_the_body_the_sentence_gave(core, mailbox):
    """'saying ...' is the body the person already wrote; draft it verbatim."""
    _run(core, "find my email about the invoice", "r93-d7")
    response = _run(core, "reply to that one saying thanks, that was fast", "r93-d7")
    drafts = [a for a in response.plan.actions if a.tool == "draft_email"]
    assert drafts, response.reply
    assert drafts[0].params["to"] == "billing@example.com", drafts[0].params
    assert drafts[0].params["body"] == "thanks, that was fast", drafts[0].params


def test_a_reply_without_a_body_drafts_an_empty_one(core, mailbox):
    """No 'saying ...' clause means an empty draft, not an invented body."""
    _run(core, "find my email about the invoice", "r93-d8")
    response = _run(core, "reply to that one", "r93-d8")
    drafts = [a for a in response.plan.actions if a.tool == "draft_email"]
    assert drafts, response.reply
    assert drafts[0].params["body"] == "", drafts[0].params


# ---------------------------------------------------------------------------
# 6. the rest of the email route is unchanged
# ---------------------------------------------------------------------------

def test_named_reads_searches_still_work(core, mailbox):
    response = _run(core, "read the invoice email", "r93-x1")
    assert response.actions and response.actions[0].ok is True, response.reply
    assert "Invoice 2026-09" in response.reply, response.reply


def test_a_search_with_no_term_still_lists(core, mailbox):
    response = _run(core, "check my email", "r93-x2")
    action = [a for a in response.plan.actions if a.tool == "search_email"][0]
    assert action.params.get("query") == "", action.params


def test_a_file_read_is_not_claimed_as_a_mail_referent(core, mailbox, tmp_area):
    (tmp_area / "notes.txt").write_text("heater coil arrived\n")
    core.planner  # touch to keep the fixture honest
    plan = core.planner.parse("read that file", {})
    tools = [a.tool for a in plan.actions]
    assert plan.referent is None or "read_file" in tools, (
        f"'read that file' was treated as a mail referent: {plan}")
