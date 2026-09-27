"""Car tools (v1.7) -- read-only vehicle telemetry.

Plan §18 lists two ways to reach a car: a manufacturer vehicle API (Mode A)
and an OBD-II adapter (Mode B). Both are wired for real here, and both are
absent by default, which makes the gateway offline.

What every tool below guarantees is the §18 rule: a value is reported only when
the connected source actually exposes it. When it does not, the answer names
the gap -- "the adapter is not connected", "this vehicle does not implement
PID 01 2F", "only a manufacturer vehicle API reports range" -- instead of a
plausible number. Nothing in this module can actuate the vehicle; there is no
unlock, horn, start or stall path, so the whole surface stays in the READ risk
tier and needs no confirmation.
"""

from __future__ import annotations

from typing import Any, List

from ..car.gateway import (
    SERVICE_KEYS,
    SERVICE_LABELS,
    CarGateway,
    Reading,
    format_value,
    get_car_gateway,
)
from ..car.metrics import METRICS, STATUS_ORDER, resolve_metric
from ..core.models import RiskLevel, ToolSpec, Verification
from .base import ToolContext, fail, ok


def _reading_line(reading: Reading) -> str:
    """A reading rendered for speech, with its provenance attached."""
    metric = METRICS.get(reading.key)
    value = format_value(metric, reading.value) if metric else str(reading.value)
    if reading.source == "last_known":
        return f"{reading.label}: {value} (last known)"
    if reading.source == "simulated":
        return f"{reading.label}: {value} (simulated)"
    return f"{reading.label}: {value}"


def _unavailable_reason(gw: CarGateway) -> str:
    return gw.adapter_error or "no car adapter or vehicle API is connected"


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

async def get_car_status(ctx: ToolContext, metric: str = "all") -> Any:
    """Report what the car can currently tell us, and what it cannot."""
    gw = get_car_gateway()
    wanted = (metric or "all").strip().lower()

    if wanted in ("diagnostics", "diagnostic", "codes", "fault", "faults"):
        return await get_car_diagnostics(ctx)

    if wanted not in ("all", "any", ""):
        key = resolve_metric(wanted)
        if key is None:
            return fail(
                "get_car_status",
                f"I do not know a car reading called {wanted!r}.",
                device=ctx.device_id,
                known=sorted(METRICS),
            )
        return await get_car_telemetry(ctx, metric=key)

    readings = gw.read_many(list(STATUS_ORDER))
    diag = gw.diagnostics()
    available = [r for r in readings if r.has_value]

    if not available:
        reason = _unavailable_reason(gw)
        return ok(
            "get_car_status",
            f"Your car adapter is not connected, so I cannot read live telemetry yet ({reason}).",
            device=ctx.device_id,
            connected=False,
            offline=gw.offline,
            mode=gw.mode,
            reason=reason,
            supported=sorted(gw.supported()),
            metrics=[r.to_dict() for r in readings],
            diagnostics=diag,
        )

    lines = [_reading_line(r) for r in available]
    if diag["available"] and diag["count"]:
        lines.append(f"Stored diagnostic codes: {diag['count']}")
    message = "Car: " + "; ".join(lines) + "."
    if not gw.connected:
        message += " This is the last state the car reported, not a live read."
    elif gw.mode == "simulate":
        message += " This is a simulated vehicle, not a real car."
    return ok(
        "get_car_status",
        message,
        device=ctx.device_id,
        connected=gw.connected,
        offline=gw.offline,
        mode=gw.mode,
        source=gw.source,
        metrics=[r.to_dict() for r in readings],
        missing=[r.key for r in readings if not r.has_value],
        diagnostics=diag,
        vin=gw.vin(),
    )


async def get_car_telemetry(ctx: ToolContext, metric: str = "", metrics: str = "") -> Any:
    """Read one or more named metrics, each with its own provenance."""
    gw = get_car_gateway()
    requested = [part for part in (metric or "").replace(";", ",").split(",") if part.strip()]
    requested += [part for part in (metrics or "").replace(";", ",").split(",") if part.strip()]
    keys: List[str] = []
    unknown: List[str] = []
    for word in requested:
        key = resolve_metric(word)
        if key is None:
            unknown.append(word.strip())
        elif key not in keys:
            keys.append(key)
    if unknown:
        return fail(
            "get_car_telemetry",
            f"I do not know a car reading called {', '.join(repr(u) for u in unknown)}.",
            device=ctx.device_id,
            known=sorted(METRICS),
        )
    if not keys:
        keys = list(STATUS_ORDER)

    readings = gw.read_many(keys)
    available = [r for r in readings if r.has_value]
    if not available:
        reason = _unavailable_reason(gw) if gw.offline else "the car does not expose any of those readings"
        # "the call answered" is not "the car was read". Phase 15's acceptance
        # sweep measured this recording verification=success while saying "I
        # could not read those car values", which rendered a VERIFIED chip over
        # the words "could not read". `.ok` stays true -- the handler did
        # complete and report honestly -- the verification does not.
        return ok(
            "get_car_telemetry",
            f"I could not read those car values ({reason}).",
            device=ctx.device_id,
            verification=Verification.FAILED,
            provider_status="unavailable",
            connected=gw.connected,
            mode=gw.mode,
            source="unavailable",
            readings=[r.to_dict() for r in readings],
        )
    message = "; ".join(_reading_line(r) for r in available) + "."
    if gw.offline:
        message += " From the last state the car reported, not a live read."
    return ok(
        "get_car_telemetry",
        message,
        device=ctx.device_id,
        connected=gw.connected,
        mode=gw.mode,
        source=available[0].source,
        readings=[r.to_dict() for r in readings],
        missing=[r.key for r in readings if not r.has_value],
    )


async def list_car_metrics(ctx: ToolContext) -> Any:
    """What this car will and will not tell us, and why."""
    gw = get_car_gateway()
    caps = gw.capabilities()
    supported = caps["supported"]
    if not supported:
        reason = caps["adapter_error"] or "no car adapter or vehicle API is connected"
        return ok(
            "list_car_metrics",
            f"No car telemetry is available right now ({reason}). "
            "Connect an OBD-II adapter or a manufacturer vehicle API to see more.",
            device=ctx.device_id,
            **caps,
        )
    return ok(
        "list_car_metrics",
        f"This car reports {len(supported)} reading(s) over {caps['source']}: "
        + ", ".join(m["label"].lower() for m in supported)
        + f". {len(caps['not_reported'])} are not available from it.",
        device=ctx.device_id,
        **caps,
    )


async def get_car_diagnostics(ctx: ToolContext) -> Any:
    """Stored fault codes -- and an explicit difference between none and unknown."""
    gw = get_car_gateway()
    diag = gw.diagnostics()
    if not diag["available"]:
        reason = diag["reason"] or "this vehicle source does not report trouble codes"
        return ok(
            "get_car_diagnostics",
            f"I cannot read the car's diagnostic codes ({reason}).",
            device=ctx.device_id,
            connected=gw.connected,
            mode=gw.mode,
            **diag,
        )
    if diag["count"] == 0:
        message = "The car reports no stored diagnostic trouble codes."
    else:
        message = (
            f"The car has {diag['count']} stored diagnostic trouble code(s): "
            + ", ".join(diag["codes"])
            + "."
        )
    if diag.get("mil") is True:
        message += " The malfunction indicator lamp is on."
    elif diag.get("mil") is False:
        message += " The malfunction indicator lamp is off."
    message += " I report telemetry; this is not a mechanical inspection."
    return ok(
        "get_car_diagnostics",
        message,
        device=ctx.device_id,
        connected=gw.connected,
        **diag,
    )


async def get_car_service_status(ctx: ToolContext) -> Any:
    """Maintenance-relevant readings, honestly partial when they are partial."""
    gw = get_car_gateway()
    service = gw.service_status()
    parts: List[str] = []
    for key in SERVICE_KEYS:
        reading = service[key]
        if reading["value"] is None:
            continue
        parts.append(f"{SERVICE_LABELS[key]}: {format_value(METRICS[key], reading['value'])}")
    if service["fault_codes"] is not None:
        parts.append(f"{service['fault_codes']} stored fault code(s)")

    if not parts:
        reason = _unavailable_reason(gw)
        return ok(
            "get_car_service_status",
            f"I cannot give you a car service summary ({reason}).",
            device=ctx.device_id,
            connected=gw.connected,
            mode=gw.mode,
            source="unavailable",
            **service,
        )
    message = "Car service: " + "; ".join(parts) + ". " + service["note"]
    if service["missing"]:
        message += f" Not reported by this vehicle: {', '.join(service['missing'])}."
    return ok(
        "get_car_service_status",
        message,
        device=ctx.device_id,
        connected=gw.connected,
        mode=gw.mode,
        **service,
    )


_SPECS = [
    (
        ToolSpec(
            name="get_car_status",
            description="Report the car's available telemetry (fuel, range, voltage, temperature) with how current it is.",
            agent="car",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={"metric": "string"},
            tags=["car", "read"],
        ),
        get_car_status,
    ),
    (
        ToolSpec(
            name="get_car_telemetry",
            description="Read named vehicle metrics such as fuel_level, engine_rpm, coolant_temp or battery_voltage.",
            agent="car",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={"metric": "string", "metrics": "string"},
            tags=["car", "read"],
        ),
        get_car_telemetry,
    ),
    (
        ToolSpec(
            name="list_car_metrics",
            description="List which vehicle readings this car or adapter exposes, and why the rest are unavailable.",
            agent="car",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={},
            tags=["car", "read"],
        ),
        list_car_metrics,
    ),
    (
        ToolSpec(
            name="get_car_diagnostics",
            description="Read the car's stored diagnostic trouble codes and malfunction indicator lamp state.",
            agent="car",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={},
            tags=["car", "read", "diagnostics"],
        ),
        get_car_diagnostics,
    ),
    (
        ToolSpec(
            name="get_car_service_status",
            description="Summarise maintenance-relevant vehicle readings: odometer, temperatures, battery voltage, fault count.",
            agent="car",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={},
            tags=["car", "read", "service"],
        ),
        get_car_service_status,
    ),
]

SPECS = _SPECS
