"""Home automation gateway (v1.5).

JARVIS talks to a local home gateway over MQTT (or another local protocol).
The gateway is **offline by default**: without a broker configured, JARVIS
keeps an in-memory *last known state* and is honest that it cannot reach any
device. It never claims a switch happened when it did not.

Three transport modes, selected automatically:

* ``offline``  -- the default. No broker configured. Nothing is sent and
  control commands report that *no device was switched*.
* ``simulate`` -- opt-in via ``JARVIS_HOME_SIMULATE=1``. Commands update the
  in-memory state so the whole flow can be exercised without hardware. Every
  result is explicitly labelled ``simulated`` so it is never mistaken for a
  real physical confirmation.
* ``mqtt``     -- opt-in via ``JARVIS_HOME_MQTT_BROKER``. Commands are
  published to the broker. Delivery is reported only when the broker accepts
  the publish; because we do not subscribe for a state echo, a publish is
  reported as *delivered* but **not verified**.

The ``home_gateway`` device in the global registry is the source of truth for
"online" when no explicit override is given.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol

from ..core.models import utcnow
from ..logging_setup import get_logger
from ..registry.devices import get_device_registry

log = get_logger("home.gateway")


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

#: Kinds of home device JARVIS understands.
KINDS = ("light", "fan", "plug", "ac", "sensor", "door", "camera")

#: Words the resolver accepts as each kind, so "the desk lamp" and "the
#: desk light" both resolve to a light.
#:
#: ``heater`` and ``hvac`` used to be here, under ``ac``. Measured consequence: "turn
#: on the heater" resolved to Study AC -- an appliance the user never named, through a
#: table that files heating under cooling because both move air. This house has no
#: heater, so the honest answer was always "there is no heater here", and the synonym
#: was not a convenience but a redirection.
KIND_SYNONYMS: Dict[str, set] = {
    "light": {"light", "lights", "lamp", "lamps", "ceiling", "bulb"},
    "fan": {"fan", "fans"},
    "plug": {"plug", "plugs", "power", "outlet"},
    "ac": {"ac", "aircon", "air", "conditioner"},
    "sensor": {"sensor", "sensors", "temperature", "motion", "energy"},
    "door": {"door", "doors"},
    "camera": {"camera", "cameras", "cam"},
}

#: Appliance words a house plausibly has that *this* inventory does not. Not a kind, so
#: nothing resolves from it -- the point is that the request is still recognised as a home
#: request and refused honestly ("there is no home device matching 'heater'") instead of
#: being answered "I understood the words but not the goal".
#:
#: This lives beside the synonym table rather than in the planner because it is the same
#: thing: the vocabulary the router is allowed to use. Phase 32 found the planner keeping
#: its own eleven-word list, which is how nine words the gateway understood became nine
#: capabilities a user could not ask for.
ABSENT_APPLIANCE_WORDS = frozenset({
    "heater", "radiator", "boiler", "furnace", "thermostat", "kettle", "toaster",
    "oven", "washing machine", "dryer", "dishwasher", "humidifier", "dehumidifier",
})


def device_vocabulary() -> List[str]:
    """Every word a home device may be called, longest first.

    The planner builds its home-control pattern from this, and only from this. Ordered by
    length so "air conditioner" and "washing machine" win over the "air" and "washer"
    fragments inside them.
    """
    words = set(ABSENT_APPLIANCE_WORDS)
    for kind, synonyms in KIND_SYNONYMS.items():
        words.add(kind)
        words.update(synonyms)
    return sorted(words, key=lambda w: (-len(w), w))


# ---------------------------------------------------------------------------
# State model
# ---------------------------------------------------------------------------

@dataclass
class ZoneDevice:
    """One device in the house (a "zone" item)."""

    device_id: str
    room: str
    kind: str
    name: str
    controllable: bool = True
    on: bool = False
    level: Optional[float] = None        # 0..1 for dimmable lights
    speed: Optional[int] = None          # 1..3 for fans
    setpoint: Optional[float] = None     # celsius for AC
    reading: Optional[float] = None      # current value for sensors/doors
    unit: str = ""
    last_changed: Optional[Any] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "device_id": self.device_id,
            "room": self.room,
            "kind": self.kind,
            "name": self.name,
            "controllable": self.controllable,
            "on": self.on,
            "level": self.level,
            "speed": self.speed,
            "setpoint": self.setpoint,
            "reading": self.reading,
            "unit": self.unit or None,
            "last_changed": self.last_changed.isoformat() if self.last_changed else None,
        }


def default_inventory() -> List[ZoneDevice]:
    """A sensible default set of devices across the house."""
    inv: List[ZoneDevice] = []

    def dev(device_id: str, room: str, kind: str, name: str, **kw: Any) -> ZoneDevice:
        item = ZoneDevice(device_id=device_id, room=room, kind=kind, name=name, **kw)
        inv.append(item)
        return item

    # Study
    dev("study.desk_lamp", "study", "light", "Study desk lamp", on=True, level=1.0)
    dev("study.desk", "study", "plug", "Study standing desk")
    dev("study.ac", "study", "ac", "Study AC", setpoint=24.0)
    dev("study.temp", "study", "sensor", "Study temperature sensor",
        controllable=False, reading=23.0, unit="°C")
    # Living room
    dev("living.ceiling", "living room", "light", "Living room ceiling light", level=0.5)
    dev("living.media", "living room", "plug", "Living room media power")
    dev("living.fan", "living room", "fan", "Living room fan")
    dev("living.temp", "living room", "sensor", "Living room temperature sensor",
        controllable=False, reading=24.0, unit="°C")
    # Kitchen
    dev("kitchen.light", "kitchen", "light", "Kitchen light")
    dev("kitchen.fridge", "kitchen", "plug", "Kitchen fridge power", on=True)
    # Bedroom
    dev("bedroom.ceiling", "bedroom", "light", "Bedroom ceiling light")
    dev("bedroom.alarm", "bedroom", "plug", "Bedroom alarm clock")
    # Hallway
    dev("hallway.light", "hallway", "light", "Hallway light")
    dev("hallway.door", "hallway", "door", "Front door sensor", controllable=False)
    dev("hallway.motion", "hallway", "sensor", "Hallway motion sensor",
        controllable=False, reading=0.0, unit="event")
    # Utility + garden
    dev("utility.energy", "utility", "sensor", "Energy monitor",
        controllable=False, reading=0.4, unit="kW")
    dev("garden.camera", "garden", "camera", "Garden camera", on=True)

    return inv


# ---------------------------------------------------------------------------
# Routines
# ---------------------------------------------------------------------------

#: device_id -> target changes. Each step is replayed through the same honest
#: control path, so an offline gateway reports "not applied" for every step.
ROUTINE_STEPS: Dict[str, List[tuple]] = {
    "study": [
        ("study.desk_lamp", {"on": True, "level": 1.0}),
        ("study.ac", {"on": False}),
        ("living.media", {"on": False}),
        ("study.desk", {"on": True}),
    ],
    "sleep": [
        ("study.desk_lamp", {"on": False}),
        ("living.ceiling", {"on": False}),
        ("kitchen.light", {"on": False}),
        ("hallway.light", {"on": False}),
        ("bedroom.ceiling", {"on": False}),
        ("garden.camera", {"on": True}),
        ("bedroom.alarm", {"on": True}),
    ],
    "movie": [
        ("living.ceiling", {"on": True, "level": 0.1}),
        ("living.media", {"on": True}),
    ],
    "leaving": [
        ("study.desk_lamp", {"on": False}),
        ("living.ceiling", {"on": False}),
        ("kitchen.light", {"on": False}),
        ("hallway.light", {"on": False}),
        ("bedroom.ceiling", {"on": False}),
        ("living.media", {"on": False}),
        ("study.desk", {"on": False}),
        ("study.ac", {"on": False}),
        ("garden.camera", {"on": True}),
    ],
    "project": [
        ("study.desk_lamp", {"on": True, "level": 1.0}),
        ("study.desk", {"on": True}),
        ("study.ac", {"on": True}),
    ],
    "emergency": [
        ("study.desk_lamp", {"on": True, "level": 1.0}),
        ("living.ceiling", {"on": True, "level": 1.0}),
        ("kitchen.light", {"on": True, "level": 1.0}),
        ("hallway.light", {"on": True, "level": 1.0}),
        ("bedroom.ceiling", {"on": True, "level": 1.0}),
        ("garden.camera", {"on": True}),
    ],
}


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------

@dataclass
class TransportResult:
    delivered: bool
    verified: bool
    detail: str = ""


class HomeTransport(Protocol):
    def send(self, topic: str, payload: Dict[str, Any]) -> TransportResult:  # pragma: no cover
        ...


class OfflineTransport:
    """Never reaches anything. Present so the code path is explicit."""

    def send(self, topic: str, payload: Dict[str, Any]) -> TransportResult:
        return TransportResult(False, False, "gateway offline")


class SimulatedTransport:
    """Pretends to deliver so the flow can be tested without hardware.

    It records what it would have sent. Results are always labelled simulated
    by the gateway, never as a real confirmation.
    """

    def __init__(self) -> None:
        self.sent: List[tuple] = []

    def send(self, topic: str, payload: Dict[str, Any]) -> TransportResult:
        self.sent.append((topic, dict(payload)))
        return TransportResult(True, True, "simulated")


class MqttTransport:
    """Minimal, defensive MQTT transport.

    Only used when ``JARVIS_HOME_MQTT_BROKER`` is set. If ``paho-mqtt`` is not
    installed or the broker is unreachable the gateway degrades to offline and
    reports the reason honestly.
    """

    def __init__(self, broker: str, username: str = "", password: str = "") -> None:
        self.broker = broker
        self.username = username
        self.password = password
        self._client: Any = None
        self._error: str = ""
        self._connect()

    def _connect(self) -> None:
        try:
            import paho.mqtt.client as mqtt  # type: ignore
        except Exception as exc:  # dependency missing
            self._error = f"paho-mqtt is not installed: {exc}"
            return
        try:
            client = mqtt.Client()
            if self.username:
                client.username_pw_set(self.username, self.password)
            host, sep, port = self.broker.rpartition(":")
            host = host or "127.0.0.1"
            port = int(port) if sep and port else 1883
            client.connect(host, port, keepalive=30)
            client.loop_start()
            self._client = client
        except Exception as exc:
            self._error = f"could not connect to {self.broker}: {exc}"

    @property
    def connected(self) -> bool:
        return self._client is not None and not self._error

    def send(self, topic: str, payload: Dict[str, Any]) -> TransportResult:
        if not self.connected:
            return TransportResult(False, False, self._error or "not connected")
        try:
            info = self._client.publish(topic, json.dumps(payload), qos=1)
        except Exception as exc:
            return TransportResult(False, False, str(exc))
        delivered = getattr(info, "rc", 1) == 0
        # A QoS1 publish is acknowledged by the broker, but we do not subscribe
        # for a state echo, so we report delivery -- not verification.
        return TransportResult(delivered, False, "published to broker; state echo not confirmed")


# ---------------------------------------------------------------------------
# Outcomes + helpers
# ---------------------------------------------------------------------------

@dataclass
class ControlOutcome:
    device: ZoneDevice
    changes: Dict[str, Any]
    delivered: bool
    verified: bool
    simulated: bool
    offline: bool
    reason: str = ""


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _topic_for(device: ZoneDevice) -> str:
    return f"jarvis/home/{device.room.replace(' ', '_')}/{device.device_id.split('.')[-1]}/set"


def _changes_from(target: Dict[str, Any]) -> Dict[str, Any]:
    changes: Dict[str, Any] = {}
    if target.get("on") is not None:
        changes["state"] = "on" if target["on"] else "off"
    if target.get("level") is not None:
        changes["brightness"] = round(float(target["level"]), 3)
    if target.get("speed") is not None:
        changes["speed"] = int(target["speed"])
    if target.get("setpoint") is not None:
        changes["setpoint_celsius"] = float(target["setpoint"])
    return changes


def _apply(device: ZoneDevice, changes: Dict[str, Any]) -> None:
    if "state" in changes:
        device.on = changes["state"] == "on"
    if "brightness" in changes:
        device.level = changes["brightness"]
    if "speed" in changes:
        device.speed = changes["speed"]
    if "setpoint_celsius" in changes:
        device.setpoint = changes["setpoint_celsius"]


_ARTICLES = ("the ", "a ", "an ")


def _normalise(query: str) -> str:
    q = query.strip().lower()
    for a in _ARTICLES:
        if q.startswith(a):
            q = q[len(a):]
    q = q.strip(" .,!?'")
    return " ".join(q.split())


# ---------------------------------------------------------------------------
# Gateway
# ---------------------------------------------------------------------------

class HomeGateway:
    """In-memory home state plus an honest command transport."""

    def __init__(
        self,
        devices: Optional[List[ZoneDevice]] = None,
        transport: Optional[HomeTransport] = None,
        *,
        online: Optional[bool] = None,
        simulate: Optional[bool] = None,
    ) -> None:
        self.devices: List[ZoneDevice] = list(devices) if devices is not None else default_inventory()
        self._by_id: Dict[str, ZoneDevice] = {d.device_id: d for d in self.devices}
        self.simulate: bool = bool(simulate if simulate is not None else _env_flag("JARVIS_HOME_SIMULATE"))
        self._online_override: Optional[bool] = online
        self._mqtt_error: str = ""
        self._transport: HomeTransport = transport if transport is not None else self._default_transport()

    # -- setup -------------------------------------------------------------

    def _default_transport(self) -> HomeTransport:
        if self.simulate:
            return SimulatedTransport()
        broker = os.environ.get("JARVIS_HOME_MQTT_BROKER", "").strip()
        if broker:
            transport = MqttTransport(
                broker,
                os.environ.get("JARVIS_HOME_MQTT_USER", ""),
                os.environ.get("JARVIS_HOME_MQTT_PASS", ""),
            )
            if transport.connected:
                return transport
            self._mqtt_error = transport._error
            return OfflineTransport()
        return OfflineTransport()

    @property
    def transport(self) -> HomeTransport:
        return self._transport

    @property
    def online(self) -> bool:
        if self._online_override is not None:
            return bool(self._online_override)
        if self.simulate:
            return True
        spec = get_device_registry().get("home_gateway")
        return bool(spec is not None and spec.online)

    @property
    def mode(self) -> str:
        if self.simulate:
            return "simulate"
        if isinstance(self._transport, MqttTransport):
            return "mqtt"
        return "offline"

    # -- lookup ------------------------------------------------------------

    def ambiguous_matches(self, query: str) -> List[ZoneDevice]:
        """Devices that tie for a request the user did not disambiguate.

        ``resolve`` has to answer *something* for its read-side callers, and its
        first-wins tie-break is how "turn on the light" picked the kitchen light
        out of four candidates when no room was named. Anything about to change a
        device's state has to ask this first: an actuation the user cannot point at
        in the answer is not a successful action, it is a guess that got executed.
        """
        q = _normalise(query)
        if not q:
            return []
        # A name or id the user actually said is never ambiguous.
        if q in self._by_id or any(q == device.name.lower() for device in self.devices):
            return []

        best, tied = 0, []
        for device in self.devices:
            score = self._score(device, q)
            if score > best:
                best, tied = score, [device]
            elif score == best and score:
                tied.append(device)
        return tied if len(tied) > 1 else []

    def resolve(self, query: str) -> Optional[ZoneDevice]:
        q = _normalise(query)
        if not q:
            return None
        if q in self._by_id:
            return self._by_id[q]
        for device_id, device in self._by_id.items():
            if q == device_id or q == device_id.split(".")[-1]:
                return device
        for device in self.devices:
            if q == device.name.lower():
                return device
        best, best_score = None, 0
        for device in self.devices:
            score = self._score(device, q)
            if score > best_score:
                best, best_score = device, score
        return best

    def actuation_target(self, query: str) -> "tuple[Optional[ZoneDevice], List[ZoneDevice]]":
        """The device a request to *change* something names, or the devices it could mean.

        ``resolve`` is a read-side lookup: answering with the closest thing it knows is
        acceptable when the worst case is reading the wrong sensor. An actuation gets no
        such latitude, and neither does ``ambiguous_matches``, which only asks whether
        several devices tie -- "heater" tied exactly one, because the vocabulary filed it
        under the air conditioner, and a wrong answer that stands alone looks identical
        to a right one.

        So the rule is inverted for anything that changes the world: **every word the
        user typed has to describe the device** -- be its id, a word in its name or room,
        or a word that genuinely names its kind. One device described acts; several ask;
        none says so and lists what does exist. This also retires the substring bonus,
        which is how "the power" reached the kitchen fridge: "power" is in that plug's
        name, so it out-scored the three other plugs and switched one of them.
        """
        q = _normalise(query)
        if not q:
            return None, []
        words = set(q.split())
        for device_id, device in self._by_id.items():
            # The whole id or the whole name, never one segment of it: "ceiling" is the
            # last component of ``living.ceiling``, and treating that as an exact
            # reference is how a category word silently picks one of four lights.
            if q == device_id or q == device.name.lower():
                return device, [device]
        described: List[ZoneDevice] = []
        for device in self.devices:
            kind_words = KIND_SYNONYMS.get(device.kind, set()) | {device.kind}
            own_words = set(device.name.lower().split()) | set(device.room.lower().split())
            if words <= (kind_words | own_words) and (words & kind_words or words & own_words):
                described.append(device)
        if len(described) == 1:
            return described[0], described
        return None, described

    @staticmethod
    def _score(device: ZoneDevice, q: str) -> int:
        name = device.name.lower()
        score = 0
        if q and q in name:
            score += 4
        if q and name in q:
            score += 3
        tokens = set(q.split())
        if tokens & KIND_SYNONYMS.get(device.kind, {device.kind}):
            score += 2
        if tokens & set(device.room.lower().split()):
            score += 2
        return score

    # -- read --------------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        by_kind: Dict[str, int] = {}
        on_count = 0
        for d in self.devices:
            by_kind[d.kind] = by_kind.get(d.kind, 0) + 1
            if d.on:
                on_count += 1
        temps = [
            {"name": d.name, "room": d.room, "reading": d.reading, "unit": d.unit}
            for d in self.devices
            if d.kind == "sensor" and d.unit == "°C"
        ]
        motion = [d for d in self.devices if d.kind == "sensor" and "motion" in d.name.lower()]
        doors = [d for d in self.devices if d.kind == "door"]
        return {
            "online": self.online,
            "mode": self.mode,
            "simulated": self.simulate,
            "device_count": len(self.devices),
            "on_count": on_count,
            "by_kind": by_kind,
            "rooms": sorted({d.room for d in self.devices}),
            "temperature": temps,
            "motion_detected": any(bool(d.reading) for d in motion),
            "door_open": any(bool(d.reading) for d in doors),
        }

    def readings(self, room: str = "") -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        room_l = room.strip().lower()
        for d in self.devices:
            if d.kind not in ("sensor", "door"):
                continue
            if room_l and room_l not in d.room.lower():
                continue
            out.append({
                "device_id": d.device_id,
                "name": d.name,
                "room": d.room,
                "kind": d.kind,
                "reading": d.reading,
                "unit": d.unit or None,
                "source": "live" if self.online else "last_known",
            })
        return out

    # -- write -------------------------------------------------------------

    def control(self, device: ZoneDevice, **target: Any) -> ControlOutcome:
        changes = _changes_from(target)
        offline = not self.online
        if not changes:
            return ControlOutcome(device, {}, False, False, self.simulate, offline, "no change requested")
        if not device.controllable:
            return ControlOutcome(device, changes, False, False, self.simulate, offline, "not controllable")
        if offline:
            # Nothing is sent and local state is left exactly as it was.
            return ControlOutcome(device, changes, False, False, self.simulate, True, "gateway offline")
        result = self._transport.send(_topic_for(device), changes)
        if result.delivered:
            _apply(device, changes)
            device.last_changed = utcnow()
        return ControlOutcome(
            device,
            changes,
            result.delivered,
            bool(result.verified and result.delivered),
            self.simulate,
            False,
            result.detail,
        )

    def run_routine(self, name: str) -> Dict[str, Any]:
        steps = ROUTINE_STEPS.get(name)
        if steps is None:
            return {"ok": False, "error": f"unknown routine {name!r}", "available": list(ROUTINE_STEPS)}
        offline = not self.online
        results: List[Dict[str, Any]] = []
        for device_id, changes in steps:
            device = self._by_id.get(device_id)
            if device is None:
                continue
            outcome = self.control(device, **changes)
            results.append({
                "device_id": device_id,
                "name": device.name,
                "changes": outcome.changes,
                "delivered": outcome.delivered,
                "verified": outcome.verified,
            })
        applied = sum(1 for r in results if r["delivered"])
        return {
            "ok": True,
            "routine": name,
            "offline": offline,
            "simulated": self.simulate,
            "total": len(results),
            "applied": applied,
            "devices": results,
        }


# ---------------------------------------------------------------------------
# Process-wide singleton (so tools share one in-memory state)
# ---------------------------------------------------------------------------

_gateway: Optional[HomeGateway] = None


def get_home_gateway() -> HomeGateway:
    global _gateway
    if _gateway is None:
        _gateway = HomeGateway()
    return _gateway


def reset_home_gateway(gateway: Optional[HomeGateway] = None) -> None:
    global _gateway
    _gateway = gateway
