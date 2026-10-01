"""Home / IoT tools (v1.5).

Every tool reports its own reachability. When the home gateway is offline the
control tools do **not** claim success -- they say plainly that no device was
switched. Only a delivered command is reported as applied, and a command is
only reported as *confirmed* when the transport verified it.
"""

from __future__ import annotations

from typing import Any, Dict, List

from ..core.models import RiskLevel, ToolSpec, Verification
from ..home.gateway import ROUTINE_STEPS, HomeGateway, get_home_gateway
from .base import ToolContext, fail, ok


def _candidates(gw: HomeGateway) -> List[str]:
    return [d.name for d in gw.devices]


def _pct(value: float) -> float:
    return round(max(0.0, min(100.0, float(value))) / 100.0, 3)


def _fan_speed(value: float) -> int:
    v = float(value)
    if v > 3:  # a percentage like 50
        v = v / 100.0 * 3.0
    return int(min(3, max(1, round(v))))


def _value_text(target, kwargs: Dict[str, Any]) -> str:
    if "level" in kwargs:
        return f" {int(round(kwargs['level'] * 100))}%"
    if "speed" in kwargs:
        return f" level {kwargs['speed']}"
    if "setpoint" in kwargs:
        return f" {kwargs['setpoint']}\u00b0C"
    return ""


def _state_text(target) -> str:
    if not target.on:
        return "off"
    if target.level is not None:
        return f"on at {int(round(target.level * 100))}%"
    if target.speed is not None:
        return f"on at level {target.speed}"
    if target.setpoint is not None:
        return f"on, setpoint {target.setpoint}\u00b0C"
    return "on"


def _reading_dict(gw: HomeGateway, d) -> Dict[str, Any]:
    return {
        "device_id": d.device_id,
        "name": d.name,
        "room": d.room,
        "kind": d.kind,
        "reading": d.reading,
        "unit": d.unit or None,
        "source": "live" if gw.online else "last_known",
    }


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

async def get_home_status(ctx: ToolContext) -> Any:
    gw = get_home_gateway()
    s = gw.status()
    motion = "detected" if s["motion_detected"] else "none"
    door = "open" if s["door_open"] else "closed"
    if s["online"]:
        summary = (
            f"Your home gateway is online ({s['mode']}). "
            f"{s['on_count']} of {s['device_count']} devices are on. "
            f"Motion: {motion}. Front door: {door}."
        )
    else:
        summary = (
            "Your home gateway is offline, so I am showing last known state and "
            "not controlling anything. "
            f"{s['on_count']} of {s['device_count']} devices were on. "
            f"Motion: {motion}. Front door: {door}."
        )
    return ok("get_home_status", summary, device=ctx.device_id, **s)


async def list_home_devices(ctx: ToolContext, room: str = "", kind: str = "", on_only: bool = False) -> Any:
    gw = get_home_gateway()
    devices: List[Dict[str, Any]] = []
    room_l, kind_l = room.strip().lower(), kind.strip().lower()
    for d in gw.devices:
        if room_l and room_l not in d.room.lower():
            continue
        if kind_l and d.kind != kind_l:
            continue
        if on_only and not d.on:
            continue
        devices.append(d.to_dict())
    scope = room.strip() or "the whole house"
    return ok(
        "list_home_devices",
        f"{len(devices)} device(s) in {scope}.",
        device=ctx.device_id,
        online=gw.online,
        devices=devices,
    )


async def get_home_reading(ctx: ToolContext, device: str = "", room: str = "") -> Any:
    gw = get_home_gateway()
    room = room.strip()
    if room.lower() in ("house", "home", "inside", "the house"):
        room = ""
    source = "live" if gw.online else "last known state"
    if device:
        target = gw.resolve(device)
        if target is None:
            return fail(
                "get_home_reading",
                f"I could not find a sensor called {device!r}.",
                device=ctx.device_id,
                candidates=_candidates(gw),
            )
        if target.kind not in ("sensor", "door"):
            return fail(
                "get_home_reading",
                f"{target.name} is a {target.kind}, not a sensor, so it has no reading.",
                device=ctx.device_id,
            )
        reading = _reading_dict(gw, target)
        value = reading["reading"]
        unit = f" {reading['unit']}" if reading["unit"] else ""
        if target.kind == "door":
            value_text = "open" if reading["reading"] else "closed"
        elif target.kind == "sensor" and "motion" in target.name.lower():
            value_text = "motion" if reading["reading"] else "no motion"
        else:
            value_text = f"{value}{unit}"
        return ok(
            "get_home_reading",
            f"{target.name}: {value_text} ({source}).",
            device=ctx.device_id,
            reading=reading,
            source=source,
        )
    readings = gw.readings(room=room)
    if not readings:
        return fail("get_home_reading", f"No sensor readings for {room or 'that room'}.", device=ctx.device_id)
    lines = []
    for r in readings:
        unit = f" {r['unit']}" if r["unit"] else ""
        lines.append(f"{r['name']} {r['reading']}{unit}")
    return ok(
        "get_home_reading",
        f"Sensor readings ({source}): " + "; ".join(lines) + ".",
        device=ctx.device_id,
        source=source,
        readings=readings,
    )


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

async def control_home_device(
    ctx: ToolContext,
    device: str = "",
    action: str = "on",
    value: float = None,  # type: ignore[assignment]
) -> Any:
    gw = get_home_gateway()
    if not device:
        return fail("control_home_device", "Which device should I control?", device=ctx.device_id)
    # An actuation is resolved strictly, not by the closest match: see
    # ``HomeGateway.actuation_target``. "heater" used to reach Study AC through the
    # synonym table, and "the power" reached the kitchen fridge through a substring of
    # its name -- in both cases a device changed that nobody had named.
    target, described = gw.actuation_target(device)
    if target is None and described:
        return fail(
            "control_home_device",
            f"Which {device} did you mean -- "
            + ", ".join(d.name for d in described)
            + "? I will not switch one of them on a guess.",
            device=ctx.device_id,
            candidates=[d.name for d in described],
            ambiguous=True,
        )
    if target is None:
        return fail(
            "control_home_device",
            f"I could not find {device!r} in your home.",
            device=ctx.device_id,
            candidates=_candidates(gw),
        )
    if not target.controllable:
        return fail(
            "control_home_device",
            f"{target.name} is a sensor, so I cannot switch it.",
            device=ctx.device_id,
        )

    action = (action or "").strip().lower()
    kwargs: Dict[str, Any] = {}
    if action in ("on",):
        kwargs["on"] = True
    elif action in ("off",):
        kwargs["on"] = False
    elif action in ("set", "dim", "raise", "lower"):
        if value is None:
            return fail(
                "control_home_device",
                f"To {action} {target.name}, tell me how much "
                "(a percentage for a light, a level for a fan, a temperature for the AC).",
                device=ctx.device_id,
            )
        if target.kind == "light":
            kwargs = {"on": True, "level": _pct(value)}
        elif target.kind == "fan":
            kwargs = {"on": True, "speed": _fan_speed(value)}
        elif target.kind == "ac":
            kwargs = {"setpoint": float(value)}
        else:
            kwargs = {"on": True}
    else:
        return fail("control_home_device", f"I do not understand the action {action!r}.", device=ctx.device_id)

    outcome = gw.control(target, **kwargs)

    if outcome.offline:
        # "I could not on Kitchen light" was the sentence this branch produced: an
        # action token used where a verb belongs. And it reached the ledger as
        # `unverified` while the offline routine on the same gateway reported
        # `failure` -- one observed condition, two recorded ones.
        attempted = {
            "on": f"switch on {target.name}",
            "off": f"switch off {target.name}",
        }.get(action) or f"{action} {target.name}" + (f" to {value}" if value is not None else "")
        return ok(
            "control_home_device",
            f"Your home gateway is offline, so I could not {attempted}. No device was switched.",
            device=ctx.device_id,
            verification=Verification.FAILED,
            device_id=target.device_id,
            device_name=target.name,
            action=action,
            provider_status="unavailable",
            delivered=False,
            verified=False,
            offline=True,
            simulated=outcome.simulated,
        )

    if not outcome.delivered:
        return ok(
            "control_home_device",
            f"The gateway did not accept the change to {target.name} ({outcome.reason}). No device was switched.",
            device=ctx.device_id,
            device_id=target.device_id,
            device_name=target.name,
            action=action,
            delivered=False,
            verified=False,
            offline=False,
            simulated=outcome.simulated,
            reason=outcome.reason,
        )

    if outcome.simulated:
        message = f"Set {target.name} to {action}{_value_text(target, kwargs)} (simulated; no real device was reached)."
    elif outcome.verified:
        message = f"{target.name} is now {_state_text(target)} (confirmed by the gateway)."
    else:
        message = f"{target.name} set to {action} (sent to the gateway, not yet confirmed)."
    return ok(
        "control_home_device",
        message,
        device=ctx.device_id,
        # A simulator accepted the command; nothing in the house changed. This used
        # to record verification=SUCCESS -- and therefore "success" in the audit
        # chain -- underneath a reply saying "no real device was reached".
        verification=(Verification.UNKNOWN if outcome.simulated else None),
        device_id=target.device_id,
        device_name=target.name,
        action=action,
        delivered=True,
        verified=outcome.verified,
        simulated=outcome.simulated,
        state=target.to_dict(),
    )


async def run_home_routine(ctx: ToolContext, routine: str = "") -> Any:
    gw = get_home_gateway()
    if not routine:
        return fail(
            "run_home_routine",
            "Which routine should I run? (" + ", ".join(ROUTINE_STEPS) + ")",
            device=ctx.device_id,
            available=list(ROUTINE_STEPS),
        )
    result = gw.run_routine(routine)
    if not result.get("ok"):
        return fail(
            "run_home_routine",
            result.get("error", "unknown routine"),
            device=ctx.device_id,
            available=result.get("available"),
        )
    if result["offline"]:
        would = ", ".join(r["name"] for r in result["devices"])
        return ok(
            "run_home_routine",
            f"Your home gateway is offline, so I could not run {routine!r}. "
            f"It would affect: {would}. Nothing was changed.",
            device=ctx.device_id,
            # Nothing was actuated, so this cannot carry a verified verdict:
            # Phase 15's sweep found the dashboard showing VERIFIED directly under
            # "Nothing was changed".
            verification=Verification.FAILED,
            provider_status="unavailable",
            routine=routine,
            offline=True,
            simulated=result["simulated"],
            total=result["total"],
            applied=0,
            devices=result["devices"],
        )
    if result["simulated"]:
        message = (
            f"Ran {routine!r} (simulated): {result['applied']} of {result['total']} "
            "device changes applied. No real device was reached."
        )
    else:
        confirmed = all(r["verified"] for r in result["devices"])
        tail = "" if confirmed else ", some not yet confirmed"
        message = (
            f"Ran {routine!r}: {result['applied']} of {result['total']} "
            f"device change(s) applied{tail}."
        )
    return ok(
        "run_home_routine",
        message,
        device=ctx.device_id,
        # See control_home_device: a simulated routine changed no real device.
        verification=(Verification.UNKNOWN if result["simulated"] else None),
        routine=routine,
        offline=False,
        simulated=result["simulated"],
        total=result["total"],
        applied=result["applied"],
        devices=result["devices"],
    )


_SPECS = [
    (
        ToolSpec(
            name="get_home_status",
            description="Report home gateway state, device counts, temperature, motion and door status.",
            agent="home",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={},
            tags=["home", "read"],
        ),
        get_home_status,
    ),
    (
        ToolSpec(
            name="list_home_devices",
            description="List home devices, optionally filtered by room, kind or on-state.",
            agent="home",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={"room": "string", "kind": "string", "on_only": "bool"},
            tags=["home", "read"],
        ),
        list_home_devices,
    ),
    (
        ToolSpec(
            name="get_home_reading",
            description="Read a home sensor (temperature, motion, door, energy).",
            agent="home",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={"device": "string", "room": "string"},
            tags=["home", "read"],
        ),
        get_home_reading,
    ),
    (
        ToolSpec(
            name="control_home_device",
            description="Turn a home device on or off, or dim/set it. Reports delivery honestly.",
            agent="home",
            # The tier says what kind of action this is -- switching a lamp is not
            # deleting a file. The confirmation says that a *physical* thing changes,
            # which no tier below PHYSICAL should be trusted to skip: measured, this
            # was LOW with no confirmation, so "Jarvis, turn on heater." was approved
            # for execution and only the gateway being offline stopped an appliance
            # change (the plan's §45 has printed FAIL for it since Phase 22, 22-G).
            # A standing grant still makes repeats quiet -- see the policy's step 5.
            risk=RiskLevel.LOW,
            requires_confirmation=True,
            reversible=True,
            allowed_devices=["laptop_main"],
            parameters={"device": "string", "action": "string", "value": "number"},
            required_parameters=["device", "action"],
            tags=["home", "control"],
        ),
        control_home_device,
    ),
    (
        ToolSpec(
            name="run_home_routine",
            description="Run a home routine (study, sleep, movie, leaving, project, emergency).",
            agent="home",
            risk=RiskLevel.LOW,
            reversible=True,
            allowed_devices=["laptop_main"],
            parameters={"routine": "string"},
            required_parameters=["routine"],
            tags=["home", "routine"],
        ),
        run_home_routine,
    ),
]

SPECS = _SPECS
