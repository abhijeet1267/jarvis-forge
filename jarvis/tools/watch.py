"""Watch tools (v1.8).

A smartwatch is a companion that calls in, so these tools mostly *stage*
things: the honest verb is "queued", and it becomes "delivered", "shown" or
"answered" only when the watch (or a configured bridge in front of it) says so.
Nothing here claims a wrist buzzed.

Pairing gates the whole surface: an unpaired watch has no key material, so there
is no channel to address and nothing is queued on its behalf.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..core.models import RiskLevel, ToolSpec
from ..watch.gateway import (
    VIBRATION_PATTERNS,
    WATCH_ROUTINES,
    WatchGateway,
    WatchItem,
    get_watch_gateway,
)
from .base import ToolContext, fail, ok

#: States a caller may filter the outbox by.
_OUTBOX_FILTERS = ("", "all", "queued", "delivered", "displayed", "replied", "failed", "awaiting")


def _unpaired(gw: WatchGateway, tool: str, device_id: str) -> Any:
    state = gw.device()["auth_state"]
    return ok(
        tool,
        f"Your watch is not paired yet (pairing state: {state}), so I have no channel "
        "to it and nothing was sent.",
        device=device_id,
        paired=False,
        auth_state=state,
        delivered=False,
        queued=False,
    )


def _delivery_words(item: WatchItem) -> str:
    """Describe one item without overstating what happened to it."""
    return item.outcome_word


def _answer_words(gw: WatchGateway, item: WatchItem) -> str:
    """What the wearer may do about one prompt, worded as truly as the guard is.

    "Answer it on the watch" is itself a claim about a device, so it is only ever
    said when the gateway would accept an answer: the sentence is taken from the
    same two predicates that gate :meth:`WatchGateway.reply` --
    :meth:`WatchGateway.reply_refusal` for whether an answer decides anything, and
    :meth:`WatchGateway.delivery_is_real` for whether a delivery happened at all.
    That coupling is the point: a promise the guard would refuse to honour cannot
    be worded back in by the tool layer.
    """
    if not item.choices:
        return ""
    refusal = gw.reply_refusal(item)
    if refusal:
        return f" {refusal}"
    if not gw.delivery_is_real(item):
        return (
            " Nothing has been shown on a real watch, so there is nothing to answer "
            "there yet; the prompt stays in the outbox."
        )
    return " Answer it on the watch, or approve it here."


def _recent_pending_confirmations(ctx: ToolContext, limit: int = 5) -> List[Dict[str, Any]]:
    """Pending confirmations from the database -- read-only, never decided here."""
    from ..db.database import get_db

    rows = get_db().query(
        """
        SELECT confirmation_id, tool, preview, risk, created_at FROM confirmations
        WHERE state = ? ORDER BY created_at DESC LIMIT ?
        """,
        ("pending", int(limit)),
    )
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

async def get_watch_status(ctx: ToolContext) -> Any:
    gw = get_watch_gateway()
    s = gw.status()
    if not s["paired"]:
        message = (f"Your watch is not paired yet (pairing state: {s['auth_state']}), so there is "
                   "nothing to report from it.")
    else:
        message = gw.summary_line()
    return ok("get_watch_status", message, device=ctx.device_id, **s)


async def get_watch_outbox(ctx: ToolContext, state: str = "") -> Any:
    gw = get_watch_gateway()
    wanted = (state or "").strip().lower()
    if wanted not in _OUTBOX_FILTERS:
        return fail(
            "get_watch_outbox",
            f"I do not know an outbox state called {wanted!r}.",
            device=ctx.device_id,
            filters=[f for f in _OUTBOX_FILTERS if f],
        )
    if wanted == "awaiting":
        items = gw.unanswered()
    elif wanted in ("", "all"):
        items = gw.items
    else:
        items = [i for i in gw.items if i.state == wanted]
    counts = gw.counts()
    if not items:
        scope = "yet" if wanted in ("", "all") else f"in state {wanted!r}"
        return ok(
            "get_watch_outbox",
            f"Nothing is {scope} for your watch.",
            device=ctx.device_id,
            items=[],
            counts=counts,
            mode=gw.mode,
        )
    return ok(
        "get_watch_outbox",
        f"{len(items)} watch item(s): " + "; ".join(f"{i.title} [{_delivery_words(i)}]" for i in items),
        device=ctx.device_id,
        items=[i.to_dict() for i in items],
        counts=counts,
        mode=gw.mode,
        awaiting_answer=len(gw.unanswered()),
    )


# ---------------------------------------------------------------------------
# Write (staged, never claimed)
# ---------------------------------------------------------------------------

async def send_watch_notification(
    ctx: ToolContext,
    title: str = "",
    body: str = "",
    priority: str = "normal",
) -> Any:
    gw = get_watch_gateway()
    if not (title or "").strip():
        return fail(
            "send_watch_notification",
            "What should the notification say?",
            device=ctx.device_id,
        )
    if not gw.paired:
        return _unpaired(gw, "send_watch_notification", ctx.device_id)
    priority = (priority or "normal").strip().lower()
    if priority not in ("normal", "high"):
        priority = "normal"
    item = gw.queue("notification", title.strip(), body=(body or "").strip(), priority=priority)
    return ok(
        "send_watch_notification",
        f"Notification \"{item.title}\" is {_delivery_words(item)}."
        + ("" if item.state != "queued" else " Nothing has reached your watch yet."),
        device=ctx.device_id,
        delivered=item.state in ("delivered", "displayed", "replied"),
        queued=item.state == "queued",
        state=item.state,
        mode=gw.mode,
        item=item.to_dict(),
    )


async def vibrate_watch(ctx: ToolContext, pattern: str = "short") -> Any:
    gw = get_watch_gateway()
    pattern = (pattern or "short").strip().lower()
    if pattern not in VIBRATION_PATTERNS:
        return fail(
            "vibrate_watch",
            f"I do not know a vibration pattern called {pattern!r}.",
            device=ctx.device_id,
            patterns=list(VIBRATION_PATTERNS),
        )
    if not gw.paired:
        return _unpaired(gw, "vibrate_watch", ctx.device_id)
    item = gw.queue("vibration", f"Vibrate: {pattern}", pattern=pattern)
    claim = (
        "the watch was told to buzz" if item.state in ("delivered", "displayed", "replied")
        else "nothing buzzed -- it is waiting in the outbox"
    )
    return ok(
        "vibrate_watch",
        f"Vibration \"{pattern}\": {claim}.",
        device=ctx.device_id,
        pattern=pattern,
        delivered=item.state in ("delivered", "displayed", "replied"),
        queued=item.state == "queued",
        state=item.state,
        mode=gw.mode,
        item=item.to_dict(),
    )


async def ask_watch_confirmation(
    ctx: ToolContext,
    question: str = "",
    confirmation_id: str = "",
) -> Any:
    """Put a pending confirmation on the wrist, per plan §25.

    This only *displays* a request that already exists. It cannot create one,
    and it can never answer one -- the wearer's decision arrives on the
    companion's call-in path, not through a tool.
    """
    gw = get_watch_gateway()
    if not gw.paired:
        return _unpaired(gw, "ask_watch_confirmation", ctx.device_id)

    pending = _recent_pending_confirmations(ctx, limit=10)
    target: Optional[Dict[str, Any]] = None
    if confirmation_id:
        target = next((p for p in pending if p["confirmation_id"] == confirmation_id), None)
        if target is None:
            return fail(
                "ask_watch_confirmation",
                f"There is no pending confirmation with id {confirmation_id}.",
                device=ctx.device_id,
                pending=[p["confirmation_id"] for p in pending],
            )
    elif question.strip():
        target = {"confirmation_id": "", "preview": question.strip(), "tool": "", "risk": 0}
    elif pending:
        target = pending[0]
    else:
        return ok(
            "ask_watch_confirmation",
            "Nothing is waiting for confirmation, so there is nothing to put on your watch.",
            device=ctx.device_id,
            queued=False,
            delivered=False,
            pending=[],
        )

    item = gw.queue(
        "confirmation",
        target["preview"] or f"Confirm {target['tool']}?",
        confirmation_id=target["confirmation_id"],
        body=target["tool"],
        routine="",
    )
    return ok(
        "ask_watch_confirmation",
        f"Confirmation prompt \"{item.title}\" is {_delivery_words(item)}."
        + _answer_words(gw, item),
        device=ctx.device_id,
        queued=item.state == "queued",
        delivered=item.state in ("delivered", "displayed", "replied"),
        state=item.state,
        mode=gw.mode,
        item=item.to_dict(),
    )


async def run_watch_routine(ctx: ToolContext, routine: str = "") -> Any:
    """Ask the wrist to start a routine; a CONFIRM there runs it here.

    The prompt is the product. Starting the routine itself happens only after
    the wearer answers, on the companion's call-in path -- so this tool never
    runs anything, however the wording is read.
    """
    gw = get_watch_gateway()
    name = (routine or "").strip().lower().replace(" ", "_")
    if name not in WATCH_ROUTINES:
        return fail(
            "run_watch_routine",
            f"I do not know a watch routine called {routine!r}.",
            device=ctx.device_id,
            available=sorted(WATCH_ROUTINES),
        )
    if not gw.paired:
        return _unpaired(gw, "run_watch_routine", ctx.device_id)

    spec = WATCH_ROUTINES[name]
    high = spec.get("priority") == "high"
    item = gw.queue(
        "confirmation",
        spec["title"],
        command=spec["command"],
        routine=name,
        priority="high" if high else "normal",
        pattern="sos" if high else "short",
    )
    # A CONFIRM on this prompt dispatches the command through the pipeline, so the
    # offer to answer it is only true while the delivery is. "Nothing has started
    # yet" stays in both branches, because it is the fact that matters here.
    if gw.delivery_is_real(item) and not gw.reply_refusal(item):
        outcome = (
            f"\"{spec['title']}\" is {_delivery_words(item)} -- it runs "
            f"{spec['command']!r} when you answer CONFIRM. Nothing has started yet."
        )
    else:
        outcome = (
            f"\"{spec['title']}\" is {_delivery_words(item)}. It would run "
            f"{spec['command']!r} on a CONFIRM from the wearer.{_answer_words(gw, item)} "
            "Nothing has started yet."
        )
    return ok(
        "run_watch_routine",
        outcome,
        device=ctx.device_id,
        routine=name,
        command=spec["command"],
        started=False,
        state=item.state,
        mode=gw.mode,
        item=item.to_dict(),
    )


_SPECS = [
    (
        ToolSpec(
            name="get_watch_status",
            description="Report the paired watch's state: pairing, capabilities, battery as last reported, outbox counts.",
            agent="watch",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={},
            tags=["watch", "read"],
        ),
        get_watch_status,
    ),
    (
        ToolSpec(
            name="get_watch_outbox",
            description="List watch prompts and alerts and how far each one got (queued, delivered, shown, answered).",
            agent="watch",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={"state": "string"},
            tags=["watch", "read"],
        ),
        get_watch_outbox,
    ),
    (
        ToolSpec(
            name="send_watch_notification",
            description="Queue a notification for the watch. Reported as queued until the watch takes it.",
            agent="watch",
            risk=RiskLevel.LOW,
            requires_confirmation=False,
            allowed_devices=["laptop_main"],
            parameters={"title": "string", "body": "string", "priority": "string"},
            required_parameters=["title"],
            tags=["watch", "notification"],
        ),
        send_watch_notification,
    ),
    (
        ToolSpec(
            name="vibrate_watch",
            description="Ask the watch to vibrate (short, long, alert or sos). Never claims the watch buzzed.",
            agent="watch",
            risk=RiskLevel.LOW,
            requires_confirmation=False,
            allowed_devices=["laptop_main"],
            parameters={"pattern": "string"},
            tags=["watch", "feedback"],
        ),
        vibrate_watch,
    ),
    (
        ToolSpec(
            name="ask_watch_confirmation",
            description="Show a pending confirmation on the watch with CONFIRM and CANCEL. Displays only; it cannot answer.",
            agent="watch",
            risk=RiskLevel.LOW,
            requires_confirmation=False,
            allowed_devices=["laptop_main"],
            parameters={"question": "string", "confirmation_id": "string"},
            tags=["watch", "confirmation"],
        ),
        ask_watch_confirmation,
    ),
    (
        ToolSpec(
            name="run_watch_routine",
            description="Put a quick-routine prompt (study, sleep, leave_home, arrive_home, emergency) on the watch to confirm.",
            agent="watch",
            risk=RiskLevel.LOW,
            requires_confirmation=False,
            allowed_devices=["laptop_main"],
            parameters={"routine": "string"},
            required_parameters=["routine"],
            tags=["watch", "routine"],
        ),
        run_watch_routine,
    ),
]

SPECS = _SPECS
