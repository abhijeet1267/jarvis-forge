"""Car gateway (v1.7) -- a read-only view of one vehicle.

Mirrors :mod:`jarvis.home.gateway` and :mod:`jarvis.robotics.gateway`: the
JARVIS process holds a view of the car and, when an adapter or vehicle API is
configured, a real transport to it.

By default there is **no** adapter, so the gateway is *offline*. Every reading
then comes from the last time the car was actually reached, labelled as such,
and a metric that was never read is reported as unavailable rather than as a
number. Per plan §18, "do not claim a value is available until the
vehicle/adapter actually exposes it", the set of reportable metrics is decided
by the source, not by the metric catalogue:

- **offline** -- nothing connected. Last known values only, plus the reason.
- **simulate** -- ``JARVIS_CAR_SIMULATE=1``. A deterministic, clearly labelled
  vehicle for demos and tests.
- **obd2** -- ``JARVIS_CAR_OBD=host:port[@protocol]``. A real ELM327 over a
  serial-over-TCP bridge; support comes from the car's own PID bitmasks.
- **manufacturer_api** -- ``JARVIS_CAR_API_URL=...``. A real HTTPS backend;
  support comes from the fields its JSON actually carries.

This agent is deliberately **read-only** (plan Phase 10: "start with a
read-only prototype"). There is no unlock, no horn, no remote start and no
ignition control -- nothing here can make the vehicle do anything, so no
physical risk class is reachable from the car surface. Actuation belongs to a
later, separately confirmed command set.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Set

from ..core.models import utcnow
from ..logging_setup import get_logger
from .metrics import METRICS, STATUS_ORDER, Metric
from .readers import (
    ApiReader,
    CarReader,
    Elm327Reader,
    OfflineReader,
    ReaderConfig,
    SimulatedReader,
    build_reader,
)

log = get_logger("car.gateway")

#: Every metric key the catalogue knows about, in catalogue order.
ALL_METRICS = tuple(METRICS)

#: What a service summary is made of, and the short words used to speak it.
SERVICE_KEYS = ("odometer", "coolant_temp", "oil_temp", "battery_voltage")
SERVICE_LABELS = {
    "odometer": "Odometer",
    "coolant_temp": "Coolant",
    "oil_temp": "Oil",
    "battery_voltage": "Battery",
}


@dataclass
class Reading:
    """One metric, and how much confidence the source deserves."""

    key: str
    label: str
    unit: str
    value: Optional[float]
    supported: bool
    source: str                     # live | last_known | unavailable | simulated
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "unit": self.unit,
            "value": self.value,
            "supported": self.supported,
            "source": self.source,
            "detail": self.detail,
        }

    @property
    def has_value(self) -> bool:
        return self.value is not None


def format_value(metric: Metric, value: Optional[float]) -> str:
    if value is None:
        return "not available"
    if metric.unit == "°C":
        return f"{value:.0f} °C"
    if metric.unit == "V":
        return f"{value:.2f} V"
    if metric.unit in ("rpm", "km", "km/h"):
        return f"{value:,.0f} {metric.unit}"
    return f"{value:g} {metric.unit}"


# ---------------------------------------------------------------------------
# Gateway
# ---------------------------------------------------------------------------

class CarGateway:
    """Owns the vehicle view and the honest transport to it."""

    def __init__(
        self,
        reader: Optional[CarReader] = None,
        *,
        config: Optional[ReaderConfig] = None,
        reader_factory: Optional[Callable[[ReaderConfig], Any]] = None,
    ) -> None:
        self._config = config
        self._reader: Optional[CarReader] = reader
        self._adapter_error = ""
        #: An injected reader is already resolved; otherwise the mode comes
        #: from the environment on first use.
        self._resolved = reader is not None
        #: Values the car actually reported the last time it was reached.
        self._last_known: Dict[str, float] = {}
        self._last_seen = None
        self._last_vin: Optional[str] = None
        self._last_dtcs: Optional[List[str]] = None
        self._last_mil: Optional[bool] = None
        self._reader_factory = reader_factory or build_reader

    # -- connectivity ------------------------------------------------------
    @property
    def config(self) -> ReaderConfig:
        return self._config or ReaderConfig.from_env()

    @property
    def reader(self) -> CarReader:
        if self._reader is None:
            if not self._resolved:
                self._reader, error = self._reader_factory(self.config)
                self._adapter_error = error or self._adapter_error
            if self._reader is None:
                self._reader = OfflineReader()
            self._resolved = True
            if isinstance(self._reader, OfflineReader):
                self._adapter_error = self._adapter_error or self._reader.reason
        return self._reader

    @property
    def offline(self) -> bool:
        return isinstance(self.reader, OfflineReader)

    @property
    def mode(self) -> str:
        """How the car is being reached, or why it is not."""
        reader = self.reader
        if isinstance(reader, OfflineReader):
            return "offline"
        if isinstance(reader, SimulatedReader):
            return "simulate"
        if isinstance(reader, Elm327Reader):
            return "obd2"
        if isinstance(reader, ApiReader):
            return "manufacturer_api"
        return "online"

    @property
    def source(self) -> str:
        return self.reader.source

    @property
    def connected(self) -> bool:
        if self.offline:
            return False
        return bool(self.reader.reachable())

    @property
    def adapter_error(self) -> str:
        """Why the car is not being read live, or "" when it is.

        A reader that stopped answering records its own reason, so a car that
        went silent mid-session is reported with that reason rather than as a
        set of numbers that happens not to have updated.
        """
        reader = self.reader
        return self._adapter_error or str(getattr(reader, "error", "") or "")

    # -- capability discovery ---------------------------------------------
    def supported(self) -> Set[str]:
        """Metric keys the vehicle source really exposes."""
        if self.offline:
            # Nothing live: whatever was once read is still knowable, but every
            # such value surfaces labelled as last_known.
            return set(self._last_known)
        try:
            return set(self.reader.supported())
        except Exception as exc:  # pragma: no cover - transport-specific
            log.warning("car support probe failed", extra_fields={"error": str(exc)})
            self._adapter_error = f"the vehicle source stopped answering ({exc})"
            return set(self._last_known)

    def unsupported(self) -> List[str]:
        """Catalogue metrics this source cannot provide, in catalogue order."""
        live = self.supported()
        return [key for key in ALL_METRICS if key not in live]

    def posture(self) -> Dict[str, Any]:
        """A cheap, non-probing view of the car link, for /health and the dashboard.

        :meth:`status` reads metrics, which for a live OBD-II adapter means real
        serial traffic and for a vehicle API means an HTTP request -- and /health
        is polled every few seconds, so "how is the car" must never become "hammer
        the car". This answers purely from configuration and whatever an earlier
        call already established; it deliberately never resolves the reader.

        It also refuses to overstate the other way: an adapter that is configured
        but has never been reached reports ``checked: false`` rather than
        "not answering".
        """
        config = self.config
        configured = bool(config.simulate or config.obd_host or config.api_url)
        via = ("simulate" if config.simulate else
               "manufacturer_api" if config.api_url else
               "obd2" if config.obd_host else "not configured")
        reader = self._reader
        # Only the transport knows whether it has already asked the car what it
        # supports, and only some transports remember. Report that honestly
        # rather than implying the gateway has a cache it does not have.
        cached = getattr(reader, "_supported", None)
        return {
            "configured": configured,
            "via": via,
            "adapter": (config.api_url or f"{config.obd_host}:{config.obd_port}"
                        if (config.api_url or config.obd_host) else ""),
            "checked": reader is not None,
            "connected": reader is not None and not isinstance(reader, OfflineReader),
            "offline": reader is None or isinstance(reader, OfflineReader),
            "adapter_error": self._adapter_error,
            "probed": cached is not None,
            "supported_metrics": len(cached) if cached else None,
        }

    # -- reads -------------------------------------------------------------
    def read(self, key: str) -> Reading:
        """One metric: asked live first, remembered second, never invented."""
        metric = METRICS.get(key)
        if metric is None:
            return Reading(key, key, "", None, False, "unavailable", "unknown metric")

        live: Set[str] = set() if self.offline else self.supported()
        value: Optional[float] = None
        detail = ""

        if self.offline:
            detail = self.adapter_error or "no vehicle source is connected"
        elif key not in live:
            detail = self.adapter_error or self._why_missing(metric)
        else:
            try:
                value = self.reader.read(key)
            except Exception as exc:  # pragma: no cover - transport-specific
                value = None
                detail = f"the read failed ({exc})"
                self._adapter_error = detail
            if value is None and not detail:
                detail = "the vehicle did not answer this request"

        if value is not None:
            self._last_known[key] = value
            self._last_seen = utcnow()
            return Reading(key, metric.label, metric.unit, value, True, self._source_label(), "")

        remembered = self._last_known.get(key)
        exposed = (key in live) if not self.offline else (remembered is not None)
        if remembered is not None:
            return Reading(key, metric.label, metric.unit, remembered, True, "last_known", detail)
        return Reading(key, metric.label, metric.unit, None, exposed, "unavailable", detail)

    def _why_missing(self, metric: Metric) -> str:
        """Say which door is closed, in terms of the source actually in use."""
        if self.mode == "obd2" and metric.pid:
            return f"this vehicle does not implement PID {metric.service} {metric.pid}"
        if metric.pid and metric.api_key:
            return "this vehicle does not report it over the connected source"
        if metric.pid:
            return f"this vehicle does not implement PID {metric.service} {metric.pid}"
        return "only a manufacturer vehicle API reports this; an OBD-II adapter does not"

    def _source_label(self) -> str:
        return "simulated" if self.mode == "simulate" else "live"

    def read_many(self, keys: List[str]) -> List[Reading]:
        return [self.read(key) for key in keys]

    # -- summaries ---------------------------------------------------------
    def status(self) -> Dict[str, Any]:
        readings = self.read_many(list(STATUS_ORDER))
        return {
            "connected": self.connected,
            "mode": self.mode,
            "source": self.source,
            "offline": self.offline,
            "simulated": self.mode == "simulate",
            "adapter_error": self.adapter_error or None,
            "supported_metrics": sorted(self.supported()),
            "unsupported_metrics": self.unsupported(),
            "readings": [r.to_dict() for r in readings],
            "available_count": sum(1 for r in readings if r.has_value),
            "last_seen": self._last_seen.isoformat() if self._last_seen else None,
            "vin": self.vin(),
            "diagnostics": self.diagnostics(),
        }

    def telemetry(self, keys: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        return [r.to_dict() for r in self.read_many(keys or list(STATUS_ORDER))]

    def capabilities(self) -> Dict[str, Any]:
        live = self.supported()
        return {
            "source": self.source,
            "mode": self.mode,
            "connected": self.connected,
            "supported": [
                {"key": key, "label": METRICS[key].label, "unit": METRICS[key].unit}
                for key in ALL_METRICS if key in live
            ],
            "not_reported": [
                {"key": key, "label": METRICS[key].label, "reason": self._why_missing(METRICS[key])}
                for key in ALL_METRICS if key not in live
            ],
            "adapter_error": self.adapter_error or None,
        }

    def diagnostics(self) -> Dict[str, Any]:
        """Stored fault codes -- keeping "unknown" distinct from "none"."""
        if self.offline:
            codes = self._last_dtcs
            return {
                "available": codes is not None,
                "source": "last_known" if codes is not None else "unavailable",
                "codes": list(codes or []),
                "mil": self._last_mil,
                "count": len(codes or []),
                "reason": self.adapter_error or "no vehicle source is connected",
            }
        try:
            codes = self.reader.dtcs()
            mil = self.reader.mil()
        except Exception as exc:  # pragma: no cover - transport-specific
            codes, mil = None, None
            self._adapter_error = f"the diagnostic read failed ({exc})"
        if codes is None:
            return {
                "available": False,
                "source": "unavailable",
                "codes": list(self._last_dtcs or []),
                "mil": mil if mil is not None else self._last_mil,
                "count": len(self._last_dtcs or []),
                "reason": self.adapter_error or "this vehicle source does not report trouble codes",
                "from_last_known": bool(self._last_dtcs),
            }
        self._last_dtcs = list(codes)
        self._last_mil = mil
        self._last_seen = utcnow()
        return {
            "available": True,
            "source": self._source_label(),
            "codes": list(codes),
            "mil": mil,
            "count": len(codes),
            "reason": "",
        }

    def service_status(self) -> Dict[str, Any]:
        """Maintenance-relevant readings: telemetry, not an inspection."""
        readings = {key: self.read(key) for key in SERVICE_KEYS}
        diag = self.diagnostics()
        return {
            **{key: readings[key].to_dict() for key in SERVICE_KEYS},
            "fault_codes": diag["count"] if diag["available"] else None,
            "mil_on": diag.get("mil"),
            "missing": [key for key in SERVICE_KEYS if not readings[key].has_value],
            "note": "Telemetry only -- it does not replace a qualified mechanical inspection.",
        }

    def vin(self) -> Optional[str]:
        if self.offline:
            return self._last_vin
        try:
            value = self.reader.vin()
        except Exception:  # pragma: no cover - transport-specific
            return self._last_vin
        if value:
            self._last_vin = value
        return value or self._last_vin

    def summary_line(self) -> str:
        """One sentence a voice reply can read out."""
        parts: List[str] = []
        for reading in self.telemetry():
            value = reading["value"]
            if value is None:
                continue
            metric = METRICS[reading["key"]]
            parts.append(f"{metric.label}: {format_value(metric, value)}")
        diag = self.diagnostics()
        if diag["available"] and diag["count"]:
            parts.append(f"Stored diagnostic codes: {diag['count']}")
        if not parts:
            reason = self.adapter_error or "no vehicle source is connected"
            return f"I have no car telemetry to report ({reason})."
        if self.mode == "simulate":
            tail = " [simulated vehicle]"
        elif self.offline:
            tail = " [last known state]"
        else:
            tail = ""
        return "; ".join(parts) + tail


# ---------------------------------------------------------------------------
# Process-wide gateway
# ---------------------------------------------------------------------------

_gateway: Optional[CarGateway] = None


def get_car_gateway() -> CarGateway:
    global _gateway
    if _gateway is None:
        _gateway = CarGateway()
    return _gateway


def reset_car_gateway(gateway: Optional[CarGateway] = None) -> None:
    """Install (or drop) the shared gateway.

    Tests swap this out so a simulate flag set by one test cannot leak into the
    next -- the same singleton discipline the home and robotics gateways use.
    """
    global _gateway
    if _gateway is not None:
        try:
            _gateway.reader.close()
        except (AttributeError, OSError):
            pass
    _gateway = gateway
