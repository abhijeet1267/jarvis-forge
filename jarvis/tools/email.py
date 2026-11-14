"""Email tools (v0.7).

The rule the plan states plainly: *never send an email merely because the model
inferred that the user wanted it*. So sending is a Level-3 destructive action
that always requires confirmation, and an ambiguous recipient is refused rather
than guessed at.

The second rule this module obeys since phase 10: an unreadable mailbox is never
reported as an empty one. "I found no messages matching 'invoice'" is only said
when the mailbox answered; every other outcome is a refusal naming what failed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core.models import RiskLevel, ToolSpec, new_id, new_secret_id, utcnow
from ..db.database import get_db
from ..integrations.email_provider import (
    EmailDraft,
    MailReadError,
    ProviderStatus,
    get_email_provider,
    looks_like_address,
    normalise_addresses,
    read_status,
    status_token,
)
from ..logging_setup import get_logger
from ..security.paths import protection_reason
from .base import ToolContext, fail, ok

log = get_logger("tools.email")

MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024

#: One sentence per way a mailbox read can fail, each naming the action that
#: actually follows. "Check your mail password" and "your mail server is not
#: reachable" must never be interchangeable, and neither may sound like an
#: inbox with nothing in it.
FAILURE_LINES = {
    ProviderStatus.UNAVAILABLE: (
        "I could not reach your mail server at {where} ({detail}). Nothing was read, so this "
        "is not an empty inbox — check that the IMAP host and port are right and running."
    ),
    ProviderStatus.AUTH_ERROR: (
        "Your mail server rejected the saved credentials ({detail}). Nothing was read — check "
        "your mail password; most providers require an app-specific password here."
    ),
    ProviderStatus.NETWORK_ERROR: (
        "The connection to your mail server at {where} broke before it answered ({detail}). "
        "Nothing was read, so this is not an empty inbox."
    ),
    ProviderStatus.PROVIDER_ERROR: (
        "Your mail server answered but could not carry out the request ({detail}). Nothing was "
        "read, so this is not an empty inbox."
    ),
}

#: A state this layer has no sentence for is still a refusal, never an empty inbox.
_FALLBACK_LINE = (
    "I could not read your mailbox ({detail}). Nothing was read, so this is not an empty inbox."
)


def _read_problem(
    tool: str,
    ctx: ToolContext,
    outcome: Any,
    *,
    because: str = "",
    **output: Any,
) -> Any:
    """Refuse, in the right words, when the mailbox could not be read.

    ``outcome`` is a labelled search result, a :class:`MailReadError`, or a
    provider dict from a send/draft.
    """
    if isinstance(outcome, dict):
        status = status_token(outcome.get("status")) or ProviderStatus.PROVIDER_ERROR
        detail = str(outcome.get("detail") or outcome.get("error") or "")
        endpoint = str(outcome.get("endpoint") or "")
    elif isinstance(outcome, BaseException):
        status = status_token(getattr(outcome, "status", None)) or ProviderStatus.PROVIDER_ERROR
        detail = str(getattr(outcome, "detail", "") or outcome or "")
        endpoint = str(getattr(outcome, "endpoint", "") or "")
    else:
        status = read_status(outcome)
        detail = str(getattr(outcome, "detail", "") or "")
        endpoint = str(getattr(outcome, "endpoint", "") or "")

    line = FAILURE_LINES.get(status, _FALLBACK_LINE)
    message = line.format(where=endpoint or "the configured address", detail=detail or status.value)
    if because:
        message = f"{message} {because}"
    return fail(
        tool,
        message,
        device=ctx.device_id,
        provider_status=status.value,
        provider_detail=detail,
        provider_endpoint=endpoint,
        **output,
    )


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------
def add_contact(name: str, address: str, aliases: Optional[List[str]] = None) -> str:
    """Register a contact so a name can be resolved to an address."""
    contact_id = new_id("CONTACT")
    get_db().execute(
        "INSERT INTO contacts (contact_id, name, address, aliases) VALUES (?, ?, ?, ?)",
        (contact_id, name.strip(), address.strip(), json.dumps(aliases or [])),
    )
    return contact_id


def resolve_recipient(target: str) -> Dict[str, Any]:
    """Turn a name or address into exactly one address.

    Returns ``{"ok": True, "address": ...}`` or ``{"ok": False, "reason": ...}``
    with ``candidates`` when the name is ambiguous. Never guesses.
    """
    value = (target or "").strip()
    if not value:
        return {"ok": False, "reason": "no recipient given"}

    if looks_like_address(value):
        return {"ok": True, "address": value}

    # "my teammate" names the contact "teammate". Strip possessives so a
    # natural phrase still resolves.
    value = re.sub(r"^(?:my|our|the)\s+", "", value, flags=re.I).strip()
    needle = value.lower()
    rows = get_db().query("SELECT * FROM contacts")
    matches: List[Dict[str, str]] = []
    for row in rows:
        aliases = json.loads(row["aliases"] or "[]")
        haystack = {row["name"].lower(), *[a.lower() for a in aliases]}
        if needle in haystack or any(needle in h for h in haystack):
            matches.append({"name": row["name"], "address": row["address"]})

    if not matches:
        return {
            "ok": False,
            "reason": (
                f"I do not have a contact called {target!r}. Give me the full email "
                "address, or add them as a contact first."
            ),
        }
    if len(matches) > 1:
        return {
            "ok": False,
            "reason": f"{target!r} matches more than one contact. Which one?",
            "candidates": matches,
        }
    return {"ok": True, "address": matches[0]["address"], "name": matches[0]["name"]}


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------
async def search_email(
    ctx: ToolContext,
    query: str = "",
    unread_only: bool = False,
    limit: int = 10,
) -> Any:
    """Find messages in the inbox."""
    provider = get_email_provider(ctx.settings)
    try:
        messages = provider.search(query=query, limit=max(1, min(int(limit), 50)), unread_only=unread_only)
    except Exception as exc:
        return fail("search_email", f"Could not read the mailbox: {exc}", device=ctx.device_id)

    status = read_status(messages)
    if not status.read_succeeded:
        return _read_problem("search_email", ctx, messages)

    if status is ProviderStatus.REAL_EMPTY:
        scope = "unread messages" if unread_only else "messages"
        return ok(
            "search_email",
            f"I found no {scope}" + (f" matching {query!r}." if query else "."),
            device=ctx.device_id,
            verified=True,
            provider=provider.name,
            provider_status=status.value,
            results=[],
        )

    shown = "; ".join(
        f"{m.subject or '(no subject)'} from {m.sender or 'unknown sender'}"
        for m in messages[:3]
    )
    more = f" (+{len(messages) - 3} more)" if len(messages) > 3 else ""
    return ok(
        "search_email",
        f"I found {len(messages)} message(s)" + (f" matching {query!r}." if query else ".")
        + f" {shown}{more}.",
        device=ctx.device_id,
        verified=True,
        provider=provider.name,
        provider_status=status.value,
        results=[m.as_dict() for m in messages],
    )


async def read_email(ctx: ToolContext, uid: str = "") -> Any:
    """Read one message in full."""
    if not uid:
        return fail("read_email", "Which message should I read?", device=ctx.device_id)

    provider = get_email_provider(ctx.settings)
    try:
        message = provider.read(uid)
    except MailReadError as exc:
        # The provider could not look. Distinct from "there is no such message".
        return _read_problem("read_email", ctx, exc, because=f"I could not check message {uid!r}.")
    except Exception as exc:
        return fail("read_email", f"Could not read that message: {exc}", device=ctx.device_id)

    if message is None:
        return fail(
            "read_email",
            f"I could not find message {uid!r}.",
            device=ctx.device_id,
            provider_status=ProviderStatus.REAL_EMPTY.value,
        )

    body = (message.get("body") or "").strip()
    if body:
        excerpt = " ".join(body.split())
        if len(excerpt) > 300:
            excerpt = excerpt[:300] + "…"
        detail = f" \"{excerpt}\""
    else:
        detail = ""
    return ok(
        "read_email",
        f"{message['subject'] or '(no subject)'} — from {message['from']}.{detail}",
        device=ctx.device_id,
        verified=True,
        provider=provider.name,
        provider_status=ProviderStatus.HAS_RESULTS.value,
        # NB: not `message=` -- that collides with ok()'s own parameter.
        email=message,
    )


async def summarize_email(ctx: ToolContext, query: str = "", limit: int = 5) -> Any:
    """Summarise recent or matching messages.

    Summaries are extractive: subjects, senders and opening lines. JARVIS does
    not invent what a message says -- and since phase 10 it does not invent an
    empty mailbox either.
    """
    provider = get_email_provider(ctx.settings)
    try:
        messages = provider.search(query=query, limit=max(1, min(int(limit), 25)))
    except Exception as exc:
        return fail("summarize_email", f"Could not read the mailbox: {exc}", device=ctx.device_id)

    status = read_status(messages)
    if not status.read_succeeded:
        return _read_problem("summarize_email", ctx, messages, because="There is nothing I can summarise.")

    if status is ProviderStatus.REAL_EMPTY:
        return ok(
            "summarize_email",
            "There is nothing to summarise.",
            device=ctx.device_id,
            verified=True,
            provider=provider.name,
            provider_status=status.value,
            items=[],
        )

    items = [
        {
            "from": m.sender,
            "subject": m.subject,
            "date": m.date,
            "unread": m.unread,
            "opening": m.snippet,
        }
        for m in messages
    ]
    unread = sum(1 for m in messages if m.unread)
    shown = "; ".join(
        f"{m.subject or '(no subject)'} from {m.sender or 'unknown sender'}"
        for m in messages[:3]
    )
    more = f" (+{len(messages) - 3} more)" if len(messages) > 3 else ""
    return ok(
        "summarize_email",
        f"{len(items)} message(s), {unread} unread. {shown}{more}.",
        device=ctx.device_id,
        verified=True,
        provider=provider.name,
        provider_status=status.value,
        items=items,
    )


# ---------------------------------------------------------------------------
# Draft
# ---------------------------------------------------------------------------
def _validate_attachments(paths: List[str]) -> tuple[List[Path], Optional[str]]:
    resolved: List[Path] = []
    for raw in paths:
        path = Path(raw).expanduser()
        reason = protection_reason(str(path))
        if reason:
            return [], reason
        if not path.is_file():
            return [], f"I could not find the attachment {path}."
        if path.stat().st_size > MAX_ATTACHMENT_BYTES:
            return [], f"{path.name} is larger than the 25 MB attachment limit."
        resolved.append(path)
    return resolved, None


async def draft_email(
    ctx: ToolContext,
    to: str = "",
    subject: str = "",
    body: str = "",
    attachments: str = "",
) -> Any:
    """Compose a message and save it as a draft. Sends nothing."""
    if not to:
        return fail("draft_email", "Who should the email go to?", device=ctx.device_id)

    resolution = resolve_recipient(to)
    if not resolution.get("ok"):
        candidates = resolution.get("candidates")
        detail = ""
        if candidates:
            detail = " Candidates: " + ", ".join(f"{c['name']} <{c['address']}>" for c in candidates)
        return fail("draft_email", resolution["reason"] + detail, device=ctx.device_id)

    attachment_paths, problem = _validate_attachments(
        [p.strip() for p in attachments.split(",") if p.strip()]
    )
    if problem:
        return fail("draft_email", problem, device=ctx.device_id)

    draft = EmailDraft(
        to=[resolution["address"]],
        subject=subject or "(no subject)",
        body=body,
        attachments=attachment_paths,
    )

    draft_id = new_secret_id("DRAFT")
    get_db().execute(
        """
        INSERT INTO email_drafts
            (draft_id, to_addresses, subject, body, attachments, state, created_at)
        VALUES (?, ?, ?, ?, ?, 'draft', ?)
        """,
        (
            draft_id,
            json.dumps(draft.to),
            draft.subject,
            draft.body,
            json.dumps([str(p) for p in draft.attachments]),
            utcnow().isoformat(),
        ),
    )

    return ok(
        "draft_email",
        f"Drafted an email to {draft.to[0]}. Nothing has been sent.",
        device=ctx.device_id,
        # The row was just written by this call; the insert is the confirmation,
        # the same rule create_event follows for the calendar read-back.
        verified=True,
        draft_id=draft_id,
        preview=draft.preview(),
        to=draft.to,
        subject=draft.subject,
    )


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------
def _load_draft(draft_id: str) -> Optional[Dict[str, Any]]:
    rows = get_db().query("SELECT * FROM email_drafts WHERE draft_id = ?", (draft_id,))
    return dict(rows[0]) if rows else None


def latest_draft() -> Optional[Dict[str, Any]]:
    """The most recent unsent draft, so 'send it' resolves sensibly."""
    rows = get_db().query(
        "SELECT * FROM email_drafts WHERE state = 'draft' ORDER BY created_at DESC LIMIT 1"
    )
    return dict(rows[0]) if rows else None


async def task_from_email(ctx: ToolContext, uid: str = "", title: str = "",
                          due: str = "", priority: str = "normal") -> Any:
    """Turn a message into a planner task that links back to it.

    The task's title comes from the person, or from the message's subject — never
    from the body, which would file a paragraph as a task title. The message's uid
    and sender are recorded in the task's detail, so the work traces back to what
    prompted it. Nothing is sent and the message is not altered.
    """
    from .planner import add_task as _add_task

    if not str(uid).strip():
        return fail("task_from_email", "Which message should the task come from?",
                    device=ctx.device_id)

    provider = get_email_provider(ctx.settings)
    message = provider.read(uid)
    if message is None:
        return fail("task_from_email", f"No message {uid!r} in this mailbox.",
                    device=ctx.device_id)

    subject = str(message.get("subject") or "").strip()
    sender = str(message.get("from") or "").strip()
    task_title = str(title).strip() or (f"Follow up: {subject}" if subject else
                                        "Follow up on an email")
    reference = f"From an email: {subject or '(no subject)'}"
    if sender:
        reference += f" — {sender}"
    reference += f" [uid {uid}]"

    result = await _add_task(ctx, title=task_title, due=due, priority=priority)
    if result.ok:
        # Record the link without changing the task's visible title.
        from ..core.orchestrator import JarvisCore

        created = (result.output or {}).get("task")
        ref = (created or {}).get("task_ref") if isinstance(created, dict) else None
        if ref:
            updated = JarvisCore().planner_service.update_task(ref, detail=reference)
            # Refresh the copy in the response so the caller sees the link it just
            # recorded. Returning the pre-update task meant the API answered with a
            # task whose detail did not yet mention the email, which reads as "the
            # link was not made" to anyone checking.
            if updated is not None:
                result.output["task"] = updated.as_dict()
        result.output["email"] = {"uid": uid, "subject": subject, "from": sender}
        result.message = (f"Added “{task_title}” from the email "
                          f"“{subject or '(no subject)'}”.")
    return result

async def task_from_email_named(ctx: ToolContext, query: str = "") -> Any:
    """Find the message a query names, then file a task linked to it.

    Refuses rather than guessing when the query matches more than one message: a
    task attached to the wrong email is worse than being asked which one.
    """
    query = str(query).strip()
    if not query:
        return fail("task_from_email_named", "Which email should the task come from?",
                    device=ctx.device_id)
    provider = get_email_provider(ctx.settings)
    found = provider.search(query=query, limit=5)
    if not read_status(found).read_succeeded:
        return fail("task_from_email_named", "I could not read the mailbox.",
                    device=ctx.device_id)
    matches = list(found)
    if not matches:
        return fail("task_from_email_named",
                    f"No email matched {query!r}.", device=ctx.device_id)
    if len(matches) > 1:
        names = "; ".join(f"{m.subject or '(no subject)'} from {m.sender}" for m in matches[:3])
        return fail("task_from_email_named",
                    f"{len(matches)} messages match {query!r}: {names}. Which one?",
                    device=ctx.device_id)
    return await task_from_email(ctx, uid=matches[0].uid)

async def send_email(
    ctx: ToolContext,
    draft_id: str = "",
    to: str = "",
    subject: str = "",
    body: str = "",
) -> Any:
    """Send a drafted or inline message.

    Reaching this handler already means the user confirmed: the tool is
    Level-3 destructive, so the permission engine gates it. It still refuses
    when the recipient cannot be resolved to exactly one address.
    """
    provider = get_email_provider(ctx.settings)

    if draft_id:
        record = _load_draft(draft_id)
        if record is None:
            return fail("send_email", f"No draft {draft_id!r}.", device=ctx.device_id)
        if record["state"] != "draft":
            return fail(
                "send_email", f"That draft is already {record['state']}.", device=ctx.device_id
            )
        recipients = normalise_addresses(json.loads(record["to_addresses"] or "[]"))
        draft = EmailDraft(
            to=recipients,
            subject=record["subject"] or "(no subject)",
            body=record["body"] or "",
            attachments=[Path(p) for p in json.loads(record["attachments"] or "[]")],
        )
    else:
        if not to:
            return fail("send_email", "Who should the email go to?", device=ctx.device_id)
        resolution = resolve_recipient(to)
        if not resolution.get("ok"):
            return fail("send_email", resolution["reason"], device=ctx.device_id)
        draft = EmailDraft(
            to=[resolution["address"]],
            subject=subject or "(no subject)",
            body=body,
        )

    # Final safety check: an ambiguous or empty recipient never gets sent.
    if len(draft.to) != 1 or not looks_like_address(draft.to[0]):
        return fail(
            "send_email",
            "I could not resolve that to exactly one valid address, so I did not send it.",
            device=ctx.device_id,
        )

    try:
        result = provider.send(draft)
    except Exception as exc:
        return fail("send_email", f"Send failed: {exc}", device=ctx.device_id)

    if not result.get("ok"):
        if status_token(result.get("status")) is not None:
            # A rejected login and an unreachable SMTP server are different
            # things to fix; neither is "it sent".
            return _read_problem(
                "send_email", ctx, result, because=f"Nothing was sent to {', '.join(draft.to)}."
            )
        return fail("send_email", result.get("error", "send failed"), device=ctx.device_id)

    if draft_id:
        get_db().execute("UPDATE email_drafts SET state = 'sent' WHERE draft_id = ?", (draft_id,))

    log.info(
        "email sent",
        extra_fields={"provider": provider.name, "recipients": len(draft.to)},
    )
    return ok(
        "send_email",
        result.get("message", f"Sent to {', '.join(draft.to)}."),
        device=ctx.device_id,
        provider=provider.name,
        sent=True,
        to=draft.to,
        subject=draft.subject,
    )


# ---------------------------------------------------------------------------
# Specs
# ---------------------------------------------------------------------------
SPECS = [
    (
        ToolSpec(
            name="search_email",
            description="Search the mailbox, optionally only unread messages.",
            agent="email",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={"query": "string", "unread_only": "bool", "limit": "int"},
            tags=["email", "read"],
            # The `lookup` key says which parameter carries the name the person
            # called the message. A search tool already takes a query, so the
            # lookup turns "which message did you mean" into a search rather
            # than a listing of whatever the default window holds.
            provides_identifiers={"uid": {"items": "results", "id": "uid",
                                          "label": "subject",
                                          "lookup": "query"}},
        ),
        search_email,
    ),
    (
        ToolSpec(
            name="task_from_email_named",
            description="Find a message by a search phrase and file a task linked to it.",
            agent="email",
            risk=RiskLevel.LOW,
            requires_confirmation=False,
            reversible=True,
            parameters={"query": "string"},
            required_parameters=["query"],
            tags=["email", "planner"],
        ),
        task_from_email_named,
    ),
    (
        ToolSpec(
            name="task_from_email",
            description=("Create a planner task from a message, linked back to it. "
                         "Sends nothing and does not change the message."),
            agent="email",
            risk=RiskLevel.LOW,
            requires_confirmation=False,
            reversible=True,
            parameters={"uid": "string", "title": "string", "due": "string",
                        "priority": "string"},
            required_parameters=["uid"],
            tags=["email", "planner"],
        ),
        task_from_email,
    ),
    (
        ToolSpec(
            name="read_email",
            description="Read one message in full.",
            agent="email",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={"uid": "string"},
            required_parameters=["uid"],
            tags=["email", "read"],
        ),
        read_email,
    ),
    (
        ToolSpec(
            name="summarize_email",
            description="Summarise recent or matching messages.",
            agent="email",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={"query": "string", "limit": "int"},
            tags=["email", "read"],
        ),
        summarize_email,
    ),
    (
        ToolSpec(
            name="draft_email",
            description="Compose an email and save it as a draft. Sends nothing.",
            agent="email",
            risk=RiskLevel.LOW,
            reversible=True,
            allowed_devices=["laptop_main"],
            parameters={"to": "string", "subject": "string", "body": "string", "attachments": "string"},
            required_parameters=["to"],
            tags=["email", "write"],
        ),
        draft_email,
    ),
    (
        ToolSpec(
            name="send_email",
            description="Send an email. Always requires confirmation.",
            agent="email",
            risk=RiskLevel.DESTRUCTIVE,
            requires_confirmation=True,
            reversible=False,
            allowed_devices=["laptop_main"],
            parameters={"draft_id": "string", "to": "string", "subject": "string", "body": "string"},
            tags=["email", "external", "destructive"],
        ),
        send_email,
    ),
]
