"""An email request is not a file operation.

**"Archive that email" was planned as ``compress_path(path="that email")``.**

Measured on the live tree:

    "archive that email"        -> compress_path(path="that email")
    "compress that email"       -> compress_path(path="that email")
    "move my invoice email to the trash" -> move_file(source="my invoice email", ...)
    "copy that email to notes"  -> copy_file(source="that email", ...)
    "delete my invoice email"   -> delete_file(path="my invoice email")

A request to refile a mail message was read as a *file* operation, so a person
approving "move my invoice email to the trash" would have been moving a file.
The mail providers implement no organize operation (search / read / summarise /
draft / send only), so the honest answer is a decline that says what the
backend actually does — not a file mutation on a path that happens to be the
message's name.

This is the same cross-domain misroute the calendar deferral guards
(``test_r90``): the file routes now defer when the subject names an email
message (and is not itself a file), mirroring the calendar-subject guard.
"""

from __future__ import annotations


def _tools(plan) -> list[str]:
    return [a.tool for a in plan.actions]


# ---------------------------------------------------------------------------
# 1. the file-mutating routes must not treat a mail message as a file
# ---------------------------------------------------------------------------

def test_archive_an_email_is_not_compressing_a_file(core):
    plan = core.planner.parse("archive that email", {})
    assert "compress_path" not in _tools(plan), plan.actions
    assert "zip" not in plan.intent and "compress" not in plan.intent
    # It is answered as an email thing, honestly, not as a file op.
    assert plan.intent == "email", plan


def test_compress_zip_an_email_declines_instead_of_compressing(core):
    for text in ("compress that email", "zip that email", "archive the invoice email"):
        plan = core.planner.parse(text, {})
        assert "compress_path" not in _tools(plan), f"{text!r} -> {plan.actions}"


def test_move_an_email_is_not_a_file_move(core):
    plan = core.planner.parse("move my invoice email to the trash", {})
    assert "move_file" not in _tools(plan), plan.actions
    assert "rename" not in plan.intent


def test_copy_an_email_is_not_a_file_copy(core):
    plan = core.planner.parse("copy that email to notes", {})
    assert "copy_file" not in _tools(plan), plan.actions


def test_delete_an_email_is_not_a_file_delete(core):
    plan = core.planner.parse("delete my invoice email", {})
    assert "delete_file" not in _tools(plan), plan.actions


# ---------------------------------------------------------------------------
# 2. the decline is honest: it says the backend has no such operation
# ---------------------------------------------------------------------------

def test_the_decline_says_what_the_backend_can_and_cannot_do(core):
    plan = core.planner.parse("archive that email", {})
    reply = (plan.direct_reply or "").lower()
    # It must not pretend the operation happened, and it must not name a file
    # tool. It should point at what the mail backend actually supports.
    assert "can't" in reply or "cannot" in reply, reply
    # It should steer toward the operations that exist.
    assert "read" in reply or "search" in reply or "draft" in reply, reply
    # ...and it must not leak an internal file-tool name as if it were the target.
    assert "compress_path" not in reply, reply


def test_the_decline_does_not_run_any_action(core):
    for text in ("archive that email", "move that email to work", "delete that email"):
        plan = core.planner.parse(text, {})
        assert not plan.actions, f"{text!r} still runs an action: {plan.actions}"


# ---------------------------------------------------------------------------
# 3. real files are NOT affected — the guard is on the subject's kind
# ---------------------------------------------------------------------------

def test_a_real_file_still_compresses(core):
    for text in ("archive that folder", "compress the project folder", "zip the logs"):
        plan = core.planner.parse(text, {})
        assert "compress_path" in _tools(plan), f"{text!r} no longer compresses: {plan.actions}"


def test_a_real_file_still_moves_and_copies(core):
    plan = core.planner.parse("move notes.txt to /tmp", {})
    assert "move_file" in _tools(plan), plan.actions
    plan = core.planner.parse("move that file to work", {})
    assert "move_file" in _tools(plan), plan.actions


def test_a_folder_called_emails_still_moves(core):
    """A directory named "emails" is a file, not a mail message.

    The subject names a container ("folder"), so it is a directory the person
    wants to move, and must not be read as a request about mail.
    """
    for text in ("move the emails folder to backup", "move my email folder to backup"):
        plan = core.planner.parse(text, {})
        assert "move_file" in _tools(plan), f"{text!r} should still move the folder: {plan.actions}"


def test_a_file_named_email_log_still_moves(core):
    """A file whose name contains an email word is a file (extension wins)."""
    plan = core.planner.parse("move email-log.txt to backup", {})
    assert "move_file" in _tools(plan), plan.actions


def test_a_real_file_still_deletes(core):
    plan = core.planner.parse("delete old.txt", {})
    assert "delete_file" in _tools(plan), plan.actions


# ---------------------------------------------------------------------------
# 4. a mark-state request is not a read or a search
# ---------------------------------------------------------------------------
# "mark that email as read" was stolen by the read-referent branch (the "read"
# in "as read"), which READ the email and reported its contents as if the mark
# had happened. "mark that email as unread" was stolen by the unread-search
# branch, which returned a search result that sounded like the mark took effect.
# Both are organize operations the mail backend has no method for, so they must
# get the same honest decline as archive/move/delete -- never a read, never a
# search that fakes the outcome.

def test_mark_an_email_as_read_is_not_a_read(core):
    plan = core.planner.parse("mark that email as read", {})
    assert "read_email" not in _tools(plan), plan.actions
    assert plan.intent == "email", plan
    assert not plan.actions, f"mark-state must not run an action: {plan.actions}"


def test_mark_an_email_as_unread_is_not_a_search(core):
    plan = core.planner.parse("mark that email as unread", {})
    assert "search_email" not in _tools(plan), plan.actions
    assert plan.intent == "email", plan
    # The old defect: this returned a search result ("I found no unread
    # messages") that read as if the mark had happened.
    reply = (plan.direct_reply or "").lower()
    assert "found" not in reply, f"mark-state must not return a search result: {reply}"
    assert "can't" in reply or "cannot" in reply, reply


def test_mark_a_named_email_as_read_is_not_a_read(core):
    plan = core.planner.parse("mark the invoice email as read", {})
    assert "read_email" not in _tools(plan), plan.actions
    assert plan.intent == "email", plan


# Regression: the words read/unread on a real request must still do their job.

def test_a_genuine_read_still_reads(core):
    for text in ("read that email", "read the invoice email", "read my invoice email"):
        plan = core.planner.parse(text, {})
        # "read that email" resolves through the referent (no action at plan
        # time); "read the invoice email" is a named read (clarification).
        # Either way it must not be a mark-state decline.
        assert plan.intent in ("read_email", "email")
        reply = (plan.direct_reply or "")
        assert "can't mark" not in reply.lower(), f"genuine read misrouted: {text!r} -> {reply}"


def test_a_genuine_unread_search_still_searches(core):
    for text in ("show me my unread emails", "what unread emails do I have"):
        plan = core.planner.parse(text, {})
        assert "search_email" in _tools(plan), f"{text!r} should search: {plan.actions}"

