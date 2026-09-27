"""Watch companion gateway (v1.8).

A smartwatch is not a device JARVIS can dial. It is a companion that **calls
in**: the plan (§4.4) gives it a voice-command surface, notifications,
vibration, confirmation prompts, device status, an emergency command and quick
routines -- all of which are things a paired wearable does by talking to the
phone/daemon beside it. So this gateway owns an **outbox** the companion drains,
an **inbox** of what its wearer decided, and (optionally) a push bridge that
reaches out.

Three modes, the same discipline as home / robotics / car:

- **queue** (default, no bridge configured) -- nothing is delivered. Prompts and
  alerts sit in the outbox waiting for the companion to fetch them, and every
  reply says "queued", never "sent to your watch".
- **simulate** -- ``JARVIS_WATCH_SIMULATE=1`` attaches an auto-acknowledging
  companion, so the whole flow is demonstrable. It **delivers** items; it never
  *answers* one. A simulated wearer cannot approve a confirmation, because a
  runner that approves on the user's behalf is exactly what this system forbids.
  That is enforced, not just promised: :meth:`WatchGateway.reply_refusal` decides
  an answer's authority from the transport and the item's delivery provenance, so
  nothing a stand-in carried can resolve a confirmation or dispatch a routine.
- **bridge** -- ``JARVIS_WATCH_BRIDGE=https://host/path`` POSTs the item to a
  real push relay (ntfy-style). Only a ``{"ok": true}`` reply counts as
  delivered; an HTTP 200 that is not that answer is "accepted, unconfirmed". An
  unreachable bridge degrades to queue and keeps the reason.

Delivery is therefore **staged**, because the stages really are different:
``queued`` -> ``delivered`` (the companion took it / the bridge accepted it) ->
``displayed`` (the companion said the screen showed it) -> ``replied`` (the
wearer answered). Claiming the last from the first is the failure mode this file
exists to prevent -- which is why the stages are recorded for the outbox but are
never, on their own, the test for whether an answer counts.

Pairing truth comes from :class:`jarvis.registry.devices.DeviceRegistry`, never
from this gateway: an unpaired watch has no key material, so nothing here can
address it. The wearer's battery level is whatever the companion last reported
-- it is never estimated.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, Tuple

from ..core.models import new_id, utcnow
from ..logging_setup import get_logger
from ..registry.devices import get_device_registry

log = get_logger("watch.gateway")

DEVICE_ID = "watch_main"

#: Outbox states, in the order they can be reached.
STATES = ("queued", "delivered", "displayed", "replied", "failed")

KINDS = ("notification", "confirmation", "vibration")

#: What the wearer can press for each kind, per plan §4.4 ("[CONFIRM] [CANCEL]").
CHOICES: Dict[str, Tuple[str, ...]] = {
    "confirmation": ("CONFIRM", "CANCEL"),
    "notification": ("DISMISSED",),
    "vibration": (),
}

#: Haptics the companion understands. A vibration is a physical effect, so the
#: wording of every reply that mentions it stays conditional.
VIBRATION_PATTERNS = ("short", "long", "alert", "sos")

#: Quick routines (§4.4). Each one is a *confirmation prompt* on the wrist for a
#: capability JARVIS already has -- the watch is a remote with a pause before it,
#: not a second implementation of home or safety control.
WATCH_ROUTINES: Dict[str, Dict[str, str]] = {
    "study": {"title": "Start study mode?", "command": "run study mode"},
    "sleep": {"title": "Start sleep mode?", "command": "run sleep mode"},
    "leave_home": {"title": "Run leaving-home routine?", "command": "run leaving home routine"},
    "arrive_home": {"title": "Run arriving-home routine?", "command": "run arriving home routine"},
    "emergency": {
        "title": "EMERGENCY: stop all physical actions?",
        "command": "stop all physical actions",
        "priority": "high",
    },
}


@dataclass
class DeliveryResult:
    """What a transport actually achieved with one item."""

    delivered: bool
    confirmed: bool          # the far end said it showed / accepted explicitly
    via: str = ""
    detail: str = ""


class WatchTransport(Protocol):
    name: str

    def push(self, item: Dict[str, Any]) -> DeliveryResult:  # pragma: no cover
        ...


class QueueTransport:
    """No bridge configured. Holds everything; delivers nothing."""

    name = "queue"

    def push(self, item: Dict[str, Any]) -> DeliveryResult:
        return DeliveryResult(False, False, "queue", "no watch bridge is configured")


class SimulatedTransport:
    """An auto-acknowledging companion. Delivers; never answers.

    ``confirmed=True`` here is only about the *record*: it says the stand-in
    accepted the item, which is why the item is filed as ``displayed`` and why a
    notification reports itself delivered. It is not evidence that a wearer was
    shown anything, so it is deliberately not what :meth:`WatchGateway.reply`
    consults -- see :meth:`WatchGateway.reply_refusal`, which refuses to let
    anything this transport delivered decide a confirmation.
    """

    name = "simulated"

    def push(self, item: Dict[str, Any]) -> DeliveryResult:
        return DeliveryResult(True, True, "simulated", "delivered to the simulated companion")


class BridgeTransport:
    """A real HTTP push relay (ntfy / webhook to a companion bridge).

    The URL comes from configuration, never from a tool argument: no user text
    ever decides where a prompt is sent.
    """

    name = "bridge"

    def __init__(self, url: str, *, token: str = "", timeout: float = 3.0,
                 opener: Any = None) -> None:
        self.url = url
        self.token = token
        self.timeout = float(timeout)
        self._opener = opener or urllib.request.urlopen
        self.error: str = ""

    def probe(self) -> bool:
        """Reachable enough to use? Only the first push really tells."""
        return bool(self.url)

    def push(self, item: Dict[str, Any]) -> DeliveryResult:
        body = json.dumps(item).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        try:
            with self._opener(request, timeout=self.timeout) as response:
                status = getattr(response, "status", 200)
                payload = response.read()
        except (urllib.error.URLError, OSError) as exc:
            self.error = f"the watch bridge at {self.url} did not answer ({exc})"
            log.warning("watch bridge unreachable", extra_fields={"url": self.url, "error": str(exc)})
            return DeliveryResult(False, False, "bridge", self.error)
        if status >= 300:
            return DeliveryResult(False, False, "bridge", f"watch bridge replied HTTP {status}")
        try:
            accepted = bool(json.loads(payload.decode("utf-8")).get("ok"))
        except (ValueError, UnicodeDecodeError, AttributeError):
            accepted = False
        if accepted:
            return DeliveryResult(True, True, "bridge", f"accepted by {self.url}")
        return DeliveryResult(True, False, "bridge",
                              f"{self.url} returned HTTP {status} without confirming the delivery")


# ---------------------------------------------------------------------------
# Outbox item
# ---------------------------------------------------------------------------

@dataclass
class WatchItem:
    """One thing the watch should show, and what happened to it."""

    item_id: str
    kind: str
    title: str
    body: str = ""
    confirmation_id: str = ""
    choices: Tuple[str, ...] = ()
    priority: str = "normal"
    pattern: str = ""                  # vibration: short | long | alert | sos
    routine: str = ""                  # quick routine this prompt belongs to
    command: str = ""                  # what a CONFIRM should run
    state: str = "queued"
    created_at: Any = field(default_factory=utcnow)
    delivered_at: Any = None
    displayed_at: Any = None
    replied_at: Any = None
    delivered_via: str = ""
    reply: str = ""
    reply_note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "item_id": self.item_id,
            "kind": self.kind,
            "title": self.title,
            "body": self.body,
            "confirmation_id": self.confirmation_id,
            "choices": list(self.choices),
            "priority": self.priority,
            "pattern": self.pattern,
            "routine": self.routine,
            "state": self.state,
            "delivered_via": self.delivered_via,
            "reply": self.reply,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "delivered_at": self.delivered_at.isoformat() if self.delivered_at else None,
            "displayed_at": self.displayed_at.isoformat() if self.displayed_at else None,
            "replied_at": self.replied_at.isoformat() if self.replied_at else None,
        }

    def as_payload(self) -> Dict[str, Any]:
        """The wire shape a companion receives."""
        return {
            "item_id": self.item_id,
            "kind": self.kind,
            "title": self.title,
            "body": self.body,
            "confirmation_id": self.confirmation_id,
            "choices": list(self.choices),
            "priority": self.priority,
            "pattern": self.pattern,
            "routine": self.routine,
            "command": self.command,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    @property
    def awaiting_reply(self) -> bool:
        """A confirmation the wearer has been handed and has not answered.

        A notification can be dismissed, but nobody is *owed* an answer for it --
        counting those would make every alert look like an unanswered approval.
        """
        return (
            self.kind == "confirmation"
            and not self.reply
            and self.state in ("delivered", "displayed")
        )

    @property
    def outcome_word(self) -> str:
        """How to talk about this item without overstating it.

        Keyed on *provenance* as much as on state: ``displayed`` reached by the
        simulated companion is a record this process wrote about itself, while
        ``displayed`` reached by a calling-in companion or a configured relay is a
        claim about a device. Reading only the state is what let a stand-in report
        a prompt as shown on a watch nobody was wearing.
        """
        if self.state == "queued":
            return "queued on the laptop, not delivered"
        if self.state == "failed":
            return "not delivered"
        if self.state == "replied":
            return f"answered {self.reply}"
        if self.delivered_via == SimulatedTransport.name:
            return "handed to the simulated companion, not shown on a real watch"
        if self.state == "displayed":
            return "shown on the watch"
        return "handed to the watch, not yet shown"


# ---------------------------------------------------------------------------
# Gateway
# ---------------------------------------------------------------------------

class WatchGateway:
    """Owns the outbox, the wearer's replies, and the honest transport."""

    def __init__(
        self,
        transport: Optional[WatchTransport] = None,
        *,
        simulate: Optional[bool] = None,
        bridge_url: str = "",
        bridge_token: str = "",
        timeout: float = 3.0,
        opener: Any = None,
    ) -> None:
        self.items: List[WatchItem] = []
        self._simulate_override = simulate
        self._transport: Optional[WatchTransport] = transport
        self._bridge_url = bridge_url
        self._bridge_token = bridge_token
        self._timeout = timeout
        self._opener = opener
        self._bridge_error = ""
        #: Battery as last reported *by the companion*. None means it has never
        #: told us -- which is a different answer from "flat".
        self._battery: Optional[float] = None
        self._reported_at = None
        self._charging: Optional[bool] = None

    # -- configuration ------------------------------------------------------
    @property
    def simulate(self) -> bool:
        if self._simulate_override is not None:
            return bool(self._simulate_override)
        return bool(os.environ.get("JARVIS_WATCH_SIMULATE"))

    @property
    def transport(self) -> WatchTransport:
        if self._transport is not None:
            return self._transport
        if self.simulate:
            self._transport = SimulatedTransport()
            return self._transport
        url = self._bridge_url or os.environ.get("JARVIS_WATCH_BRIDGE", "").strip()
        if url:
            self._transport = BridgeTransport(
                url,
                token=self._bridge_token or os.environ.get("JARVIS_WATCH_BRIDGE_TOKEN", ""),
                timeout=self._timeout or float(os.environ.get("JARVIS_WATCH_TIMEOUT", 3.0)),
                opener=self._opener,
            )
            return self._transport
        self._transport = QueueTransport()
        return self._transport

    @property
    def mode(self) -> str:
        if isinstance(self.transport, SimulatedTransport):
            return "simulate"
        if isinstance(self.transport, BridgeTransport):
            return "bridge"
        return "queue"

    @property
    def bridge_error(self) -> str:
        if isinstance(self.transport, BridgeTransport):
            return self.transport.error or self._bridge_error
        return self._bridge_error

    # -- device truth -------------------------------------------------------
    def device(self) -> Dict[str, Any]:
        """What the device registry says about the watch. Nothing invented."""
        registry = get_device_registry()
        spec = registry.get(DEVICE_ID)
        if spec is None:
            return {"known": False, "paired": False, "auth_state": "unknown",
                    "capabilities": [], "fingerprint": "", "last_seen": None}
        return {
            "known": True,
            "paired": spec.is_paired,
            "auth_state": spec.auth_state,
            "revoked": spec.is_revoked,
            "capabilities": list(spec.capabilities),
            "fingerprint": spec.fingerprint,
            "last_seen": spec.last_seen.isoformat() if spec.last_seen else None,
        }

    @property
    def paired(self) -> bool:
        return bool(self.device()["paired"])

    def note_device_report(self, battery: Optional[float] = None,
                           charging: Optional[bool] = None) -> None:
        """Record what the companion just told us about itself."""
        if battery is not None:
            try:
                value = float(battery)
            except (TypeError, ValueError):
                value = None
            if value is not None and 0.0 <= value <= 100.0:
                self._battery = value
        if charging is not None:
            self._charging = bool(charging)
        if battery is not None or charging is not None:
            self._reported_at = utcnow()

    # -- outbox -------------------------------------------------------------
    def queue(self, kind: str, title: str, **fields: Any) -> WatchItem:
        if kind not in KINDS:
            raise ValueError(f"unknown watch item kind {kind!r}")
        item = WatchItem(
            item_id=new_id("WATCH"),
            kind=kind,
            title=title,
            choices=fields.pop("choices", CHOICES.get(kind, ())),
            **fields,
        )
        self.items.append(item)
        result = self.transport.push(item.as_payload())
        if result.delivered:
            item.delivered_at = utcnow()
            item.delivered_via = result.via
            # How far the *transport* says this got. A simulated transport
            # confirms, so a stand-in's item is filed as ``displayed``: that is
            # the outbox record of what our own code did with it, and it is why
            # ``state`` alone can never be the test of whether an answer counts.
            item.state = "displayed" if result.confirmed else "delivered"
        elif isinstance(self.transport, (QueueTransport, BridgeTransport)):
            # Still queued: the companion may well come and collect it later.
            item.state = "queued"
            if result.detail:
                self._bridge_error = result.detail
        item.reply_note = result.detail
        log.info("watch item queued", extra_fields={"item_id": item.item_id, "kind": kind,
                                                     "state": item.state})
        return item

    def pending(self) -> List[WatchItem]:
        """Items a companion can still take: queued, and prompts awaiting an answer."""
        return [i for i in self.items if i.state == "queued" or i.awaiting_reply]

    def take(self, limit: int = 10) -> List[WatchItem]:
        """Hand queued items to a companion that has called in.

        Taking is only *delivery* in the weak sense: the companion holds them now.
        """
        ready = [i for i in self.items if i.state == "queued"][:max(1, int(limit or 1))]
        for item in ready:
            item.state = "delivered"
            item.delivered_at = utcnow()
            item.delivered_via = "companion_poll"
        return ready

    def ack(self, item_id: str, *, displayed: bool = True) -> Optional[WatchItem]:
        """The companion reports whether the item actually reached the screen.

        ``displayed=False`` is a note, not an answer: a prompt the wearer never
        saw is not one they declined. Dismissing a notification is a reply
        (DISMISSED), which keeps "not shown" and "said no" as separate facts.
        """
        item = self.find(item_id)
        if item is None:
            return None
        if displayed:
            item.displayed_at = utcnow()
            if item.state in ("queued", "delivered", "failed"):
                item.state = "displayed"
        else:
            item.reply_note = "the companion reported it was not shown"
            if item.state == "queued":
                item.state = "delivered"
        return item

    # -- answering ----------------------------------------------------------
    def bears_authority(self, item: WatchItem) -> bool:
        """True when answering this item would decide something real.

        Either the wearer's button resolves a pending confirmation, or the prompt
        carries a routine command the call-in path feeds back into the pipeline --
        which includes "stop all physical actions". The shape mirrors what the
        companion's reply route acts on, rather than the item's label, so a
        mislabelled item cannot smuggle an answer past the guard. A notification
        or a buzz carries neither, so DISMISSING one stays a bookkeeping act.
        """
        return bool(item.confirmation_id or item.command)

    def delivery_is_real(self, item: WatchItem) -> bool:
        """Whether something outside this process actually took this item.

        Three independent facts, and no item-state string among them: the mode
        this gateway is in, the transport it resolves, and what the item says
        carried it. Fails closed on all three, so a prompt filed as ``displayed``
        by a stand-in, or one no transport ever took, is not a delivery.
        """
        if self.simulate or isinstance(self.transport, SimulatedTransport):
            return False
        if item.delivered_via == SimulatedTransport.name:
            return False
        return bool(item.delivered_via)

    def reply_refusal(self, item: WatchItem) -> str:
        """Why an answer to this item must not be recorded, or ``""`` if it may.

        Only authority-bearing items are gated, and the answer is a predicate the
        tools quote verbatim, so no message can promise what ``reply`` would
        refuse. An unpaired, pending or revoked watch, a delivery made by the
        simulated companion, and an item no transport ever took all say the same
        thing: nobody was shown this prompt, so an answer arriving for it was
        authored by something other than a wearer.
        """
        if not self.bears_authority(item):
            return ""
        if not self.paired:
            return (
                f"Your watch is not paired (pairing state: {self.device()['auth_state']}), so "
                "nothing arriving from it can authorise this prompt."
            )
        if not self.delivery_is_real(item):
            if self.simulate or isinstance(self.transport, SimulatedTransport) or (
                item.delivered_via == SimulatedTransport.name
            ):
                return (
                    "This prompt was carried by the simulated watch companion, not by a watch "
                    "anyone is wearing, so a simulated wearer cannot confirm or cancel it. "
                    "Answer it from the paired watch, or decide it here."
                )
            return (
                "No companion or bridge has taken this prompt, so nothing showed it on a watch "
                "and there is nothing to answer yet."
            )
        return ""

    def reply(self, item_id: str, decision: str) -> Tuple[Optional[WatchItem], str]:
        """Record the wearer's answer. Returns ``(item, error)``.

        Guards, in order, because each is a different way this could go wrong:
        unknown item; an item with no buttons; a prompt that never left the outbox;
        an unpaired or simulated surface trying to authorise a real decision; a
        choice the item never offered; and an answer to a prompt that has already
        been answered.
        """
        item = self.find(item_id)
        if item is None:
            return None, "I have no watch prompt with that id."
        if not item.choices:
            return item, "That watch item has nothing to answer."
        if item.state in ("queued", "failed"):
            # The wearer cannot have pressed a button the watch never showed.
            # Accepting it anyway would let a prompt be approved from the laptop
            # while pretending it came off the wrist.
            return item, "That prompt has not reached your watch, so there is nothing to answer yet."
        refusal = self.reply_refusal(item)
        if refusal:
            return item, refusal
        decision = (decision or "").strip().upper()
        if decision not in item.choices:
            return item, f"The watch offered {' or '.join(item.choices)}, not {decision!r}."
        if item.state == "replied":
            return item, f"Already answered {item.reply}."
        item.reply = decision
        item.state = "replied"
        item.replied_at = utcnow()
        item.displayed_at = item.displayed_at or utcnow()
        return item, ""

    def find(self, item_id: str) -> Optional[WatchItem]:
        for item in self.items:
            if item.item_id == item_id or (item.confirmation_id and item.confirmation_id == item_id):
                return item
        return None

    def unanswered(self) -> List[WatchItem]:
        return [i for i in self.items if i.choices and i.state != "replied"]

    # -- views --------------------------------------------------------------
    def counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {state: 0 for state in STATES}
        for item in self.items:
            out[item.state] = out.get(item.state, 0) + 1
        out["total"] = len(self.items)
        return out

    def status(self) -> Dict[str, Any]:
        device = self.device()
        # "Online" is only something JARVIS can claim when the watch (or a
        # bridge in front of it) has actually been heard from.
        contacted = self._reported_at is not None or any(
            i.delivered_via in ("bridge", "companion_poll", "simulated") for i in self.items
        )
        return {
            "device_id": DEVICE_ID,
            "mode": self.mode,
            "transport": self.transport.name,
            "paired": device["paired"],
            "auth_state": device["auth_state"],
            "capabilities": device["capabilities"],
            "fingerprint": device["fingerprint"],
            "online": bool(device["paired"] and contacted),
            "battery": self._battery,
            "battery_reported_by": "watch" if self._battery is not None else "never reported",
            "charging": self._charging,
            "last_seen": self._reported_at.isoformat() if self._reported_at else device["last_seen"],
            "counts": self.counts(),
            "awaiting_answer": [i.item_id for i in self.unanswered()],
            "bridge_error": self.bridge_error or None,
            "routines": sorted(WATCH_ROUTINES),
        }

    def summary_line(self) -> str:
        s = self.status()
        if not s["paired"]:
            return (f"Your watch is not paired yet (pairing state: {s['auth_state']}), "
                    "so I have nothing to show on it.")
        if s["battery"] is None:
            battery = "battery not reported by the watch"
        else:
            battery = f"battery {s['battery']:g}% (reported by the watch)"
        if s["mode"] == "queue":
            via = "no watch bridge is configured, so prompts wait in the outbox"
        elif s["mode"] == "simulate":
            via = "a simulated companion is attached"
        else:
            via = "prompts are pushed to the configured watch bridge"
        waiting = s["counts"]["queued"]
        tail = f"; {waiting} item(s) still undelivered" if waiting else ""
        return f"Watch {s['auth_state']}, {battery}; {via}{tail}."


# ---------------------------------------------------------------------------
# Process-wide gateway
# ---------------------------------------------------------------------------

_gateway: Optional[WatchGateway] = None


def get_watch_gateway() -> WatchGateway:
    global _gateway
    if _gateway is None:
        _gateway = WatchGateway()
    return _gateway


def reset_watch_gateway(gateway: Optional[WatchGateway] = None) -> None:
    global _gateway
    _gateway = gateway
