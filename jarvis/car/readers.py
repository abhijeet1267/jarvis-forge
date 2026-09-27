"""Vehicle telemetry readers -- the ways JARVIS can reach a car (v1.7).

A reader answers exactly three questions about a vehicle, and answers "I
cannot know" rather than guessing:

* which metrics does this source actually expose?
* what is the current value of one of them?
* what faults has the vehicle stored?

The honesty rule from plan §18 -- "do not claim a value is available until the
vehicle/adapter actually exposes it" -- lives here: :meth:`supported` is the
only thing that decides whether a metric can be reported, and every reader
derives it from a real probe (the OBD-II PID-support bitmask, or the keys the
manufacturer API actually returned) instead of from the metric catalogue.

``None`` and an empty list mean different things throughout: ``None`` is "the
source could not tell me", ``[]`` is "the source answered and the answer is
nothing". Conflating them would let "no faults stored" be reported while the
adapter is unplugged.
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol, Set, Tuple

from ..logging_setup import get_logger
from .metrics import (
    METRICS,
    Metric,
    decode_dtc_payload,
    decode_supported_pids,
    dtc_count_from_monitor_status,
    mil_from_monitor_status,
)

log = get_logger("car.readers")


# ---------------------------------------------------------------------------
# Reader interface
# ---------------------------------------------------------------------------

class CarReader(Protocol):
    """One way of reaching one vehicle."""

    source: str

    def reachable(self) -> bool:
        """False when the source is configured but not actually answering."""

    def supported(self) -> Set[str]:
        """Metric keys this vehicle/adapter really exposes."""

    def read(self, key: str) -> Optional[float]:
        """Current value, or None if it cannot be known right now."""

    def dtcs(self) -> Optional[List[str]]:
        """Stored diagnostic trouble codes, or None if unknown."""

    def mil(self) -> Optional[bool]:
        """Check-engine lamp state, or None if unknown."""

    def vin(self) -> Optional[str]:
        """Vehicle identification number, or None if not exposed."""

    def close(self) -> None:
        """Release any socket/connection."""


def _metric(key: str) -> Optional[Metric]:
    return METRICS.get(key)


# ---------------------------------------------------------------------------
# Offline
# ---------------------------------------------------------------------------

class OfflineReader:
    """Nothing is connected. Answers nothing, and says so."""

    source = "offline"

    def __init__(self, reason: str = "no car adapter or vehicle API is configured") -> None:
        self.reason = reason

    def reachable(self) -> bool:
        return False

    def supported(self) -> Set[str]:
        return set()

    def read(self, key: str) -> Optional[float]:
        return None

    def dtcs(self) -> Optional[List[str]]:
        return None

    def mil(self) -> Optional[bool]:
        return None

    def vin(self) -> Optional[str]:
        return None

    def close(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Simulated vehicle
# ---------------------------------------------------------------------------

#: What ``JARVIS_CAR_SIMULATE=1`` pretends the car exposes. Deliberately a
#: subset of the catalogue: a metric that is absent here must still be reported
#: as unavailable, so simulate mode can never become a way to invent a reading.
SIMULATED_METRICS: Dict[str, float] = {
    "fuel_level": 42.0,
    "range": 210.0,
    "odometer": 61420.0,
    "coolant_temp": 88.0,
    "engine_rpm": 850.0,
    "vehicle_speed": 0.0,
    "battery_voltage": 12.61,
    "throttle": 0.0,
}

SIMULATED_DTCS: List[str] = ["P0420"]


class SimulatedReader:
    """A deterministic vehicle for demos and tests -- labelled as one."""

    source = "simulated"

    def __init__(
        self,
        metrics: Optional[Dict[str, float]] = None,
        dtcs: Optional[List[str]] = None,
        mil: Optional[bool] = None,
    ) -> None:
        self._metrics = dict(SIMULATED_METRICS if metrics is None else metrics)
        self._dtcs = list(SIMULATED_DTCS if dtcs is None else dtcs)
        self._mil = bool(self._dtcs) if mil is None else mil

    def reachable(self) -> bool:
        return True

    def supported(self) -> Set[str]:
        return set(self._metrics)

    def read(self, key: str) -> Optional[float]:
        return self._metrics.get(key)

    def dtcs(self) -> Optional[List[str]]:
        return list(self._dtcs)

    def mil(self) -> Optional[bool]:
        return self._mil

    def vin(self) -> Optional[str]:
        return None

    def close(self) -> None:
        return None


# ---------------------------------------------------------------------------
# ELM327 wire helpers
# ---------------------------------------------------------------------------

def parse_hex_line(line: str) -> List[int]:
    """Split an ELM327 payload line into byte values, ignoring prose.

    Anything that is not a byte pair ("BUS ERROR", "NO DATA", a lone "?") is
    dropped here; callers decide what to conclude when too few bytes remain.
    """
    values: List[int] = []
    for token in line.replace(":", " ").split():
        if len(token) > 2:
            continue
        try:
            value = int(token, 16)
        except ValueError:
            continue
        values.append(value)
    return values


def reassemble_frames(lines: List[str]) -> Optional[List[int]]:
    """Reassemble raw ISO-15765 frames into one payload.

    Only a fallback: an ELM327 does its own transport-layer reassembly and
    prints a de-framed payload (wrapped over several lines when long), which is
    the shape :meth:`Elm327Reader._request` tries first. A tool that forwards
    CAN frames verbatim produces this shape instead. Returns None when frames
    are missing or out of order -- a partial payload would be a wrong answer,
    so it is reported as no answer.
    """
    if not lines:
        return None
    first = parse_hex_line(lines[0])
    if not first:
        return None
    pci = first[0] >> 4
    if pci == 0:                                  # single frame: length in the low nibble
        return first[1:1 + (first[0] & 0x0F)]
    if pci != 1:                                  # not a first frame
        return None
    if len(first) < 2:
        return None
    total = ((first[0] & 0x0F) << 8) | first[1]
    payload = list(first[2:])
    expected_seq = 1
    for line in lines[1:]:
        frame = parse_hex_line(line)
        if len(frame) < 2 or (frame[0] >> 4) != 2 or (frame[0] & 0x0F) != expected_seq:
            return None
        payload.extend(frame[1:])
        expected_seq = (expected_seq + 1) & 0x0F
        if len(payload) >= total:
            break
    if total and len(payload) < total:
        return None
    return payload[:total] if total else payload


class _LineBuffer:
    """Turns a socket byte stream into ELM327 text lines."""

    def __init__(self) -> None:
        self._raw = ""

    def feed(self, chunk: bytes) -> None:
        self._raw += chunk.decode("latin-1", errors="replace")

    def lines(self) -> List[str]:
        out: List[str] = []
        while True:
            for sep in ("\r\n", "\n", "\r"):
                idx = self._raw.find(sep)
                if idx != -1:
                    line, self._raw = self._raw[:idx], self._raw[idx + len(sep):]
                    line = line.strip().strip(">").strip()
                    if line:
                        out.append(line)
                    break
            else:
                return out


# ---------------------------------------------------------------------------
# Mode B -- OBD-II through an ELM327 serial-over-TCP bridge
# ---------------------------------------------------------------------------

class Elm327Reader:
    """A real ELM327 adapter reachable over TCP (Wi-Fi / RFCOMM bridge).

    Reads are standard SAE J1979 requests. Support discovery uses the car's own
    PID 00 bitmasks at bases 0x00, 0x20 and 0x40, so a metric is only ever
    offered when the vehicle said it implements the PID behind it.
    """

    source = "obd2"
    SUPPORT_BASES = (0x00, 0x20, 0x40)
    #: After the first line arrives, keep reading quietly for this long so
    #: multi-frame responses are not cut off mid-transfer.
    QUIET_WINDOW = 0.15

    def __init__(
        self,
        host: str,
        port: int = 35145,
        *,
        protocol: str = "6",
        timeout: float = 3.0,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.protocol = (protocol or "6").strip() or "6"
        self.timeout = float(timeout)
        self._sock: Optional[socket.socket] = None
        self._buf = _LineBuffer()
        self._initialised = False
        self._supported: Optional[Set[str]] = None
        self.error: str = ""

    # -- link --------------------------------------------------------------
    def _open(self) -> bool:
        if self._sock is not None:
            return True
        try:
            self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            self._sock.settimeout(self.timeout)
        except OSError as exc:
            self._sock = None
            self.error = f"cannot reach the OBD-II adapter at {self.host}:{self.port} ({exc})"
            log.warning("obd2 connect failed", extra_fields={"host": self.host, "port": self.port, "error": str(exc)})
            return False
        self._buf = _LineBuffer()
        return True

    def _recv(self, wait: float) -> List[str]:
        """Collect adapter output for up to ``wait`` seconds.

        Stops at the ELM327 prompt (``>``) when one arrives, otherwise once a
        line has been seen and the link has been quiet for ``QUIET_WINDOW`` --
        that is what lets a single-frame read cost one round trip while a VIN
        still gets all of its frames.
        """
        lines: List[str] = []
        if self._sock is None:
            return lines
        deadline = time.monotonic() + wait
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if lines:
                remaining = min(remaining, self.QUIET_WINDOW)
            try:
                self._sock.settimeout(remaining)
                chunk = self._sock.recv(4096)
            except socket.timeout:
                if lines:
                    break
                continue
            except OSError as exc:
                self.error = f"adapter link failed: {exc}"
                self.close()
                break
            if not chunk:
                break
            self._buf.feed(chunk)
            lines.extend(self._buf.lines())
            if b">" in chunk:
                break
        return lines

    def _send(self, command: str) -> bool:
        if self._sock is None and not self._open():
            return False
        sock = self._sock
        if sock is None:
            return False
        try:
            sock.sendall((command + "\r").encode("ascii"))
        except OSError as exc:
            self.error = f"could not write to the adapter: {exc}"
            self.close()
            return False
        return True

    def _init(self) -> bool:
        """Bring the adapter up. Idempotent; retried after a link failure."""
        if self._initialised:
            return True
        if not self._open():
            return False
        for command in ("ATZ", "ATE0", "ATL0", f"ATSP{self.protocol}"):
            if not self._send(command):
                return False
            self._recv(1.0)
        self._initialised = True
        return True

    def reachable(self) -> bool:
        return self._init() and self._probe_support()

    # -- J1979 -------------------------------------------------------------
    def _request(self, message: str, expected_head: List[int]) -> Optional[List[int]]:
        """Send one request and return the payload after its reply header.

        ``message`` is always a hard-coded hex string built by this class -- no
        caller-supplied text ever reaches the wire. A reply that does not start
        with the header this request should produce is treated as no answer, so
        a stale or foreign line can never be decoded as a measurement.
        """
        if not self._init():
            return None
        if not self._send(message):
            return None
        received = self._recv(self.timeout + 0.5)
        if not received:
            return None
        flat: List[int] = []
        for line in received:
            flat.extend(parse_hex_line(line))
        deframed = reassemble_frames(received) or []
        for candidate in (flat, deframed):
            if candidate[: len(expected_head)] == expected_head:
                return list(candidate[len(expected_head):])
        return None

    def _query(self, service: str, pid: str) -> Optional[List[int]]:
        head = [0x40 + int(service, 16), int(pid, 16)]
        return self._request(f"{service}{pid}", head)

    def _mode_only(self, mode: str) -> Optional[List[int]]:
        """Modes 03 / 0A carry no PID, and their reply echoes only the mode."""
        head = [0x40 + int(mode, 16)]
        return self._request(mode, head)

    def _probe_support(self) -> bool:
        if self._supported is not None:
            return True
        raw: Set[str] = set()
        for base in self.SUPPORT_BASES:
            payload = self._query("01", f"{base:02X}")
            if payload:
                raw |= decode_supported_pids(payload, base)
        if not raw:
            self.error = self.error or "the adapter did not answer the PID-support probe"
            return False
        self._supported = {
            key for key, metric in METRICS.items()
            if metric.pid and metric.pid.upper() in raw
        }
        return True

    def supported(self) -> Set[str]:
        if self._supported is None and not self._probe_support():
            return set()
        return set(self._supported or set())

    def read(self, key: str) -> Optional[float]:
        metric = _metric(key)
        if metric is None or not metric.pid or metric.decode is None:
            return None
        if key not in self.supported():
            return None
        payload = self._query(metric.service, metric.pid)
        if not payload:
            return None
        try:
            value = metric.decode(payload)
        except (TypeError, ValueError, IndexError):
            return None
        return value if metric.in_range(value) else None

    def dtcs(self) -> Optional[List[str]]:
        payload = self._mode_only("03")
        return decode_dtc_payload(payload) if payload is not None else None

    def mil(self) -> Optional[bool]:
        payload = self._query("01", "01")
        return mil_from_monitor_status(payload) if payload else None

    def dtc_count(self) -> Optional[int]:
        payload = self._query("01", "01")
        return dtc_count_from_monitor_status(payload) if payload else None

    def vin(self) -> Optional[str]:
        payload = self._query("09", "02")
        if not payload:
            return None
        # Byte 3 of the reply is the format counter, not a VIN character.
        chars = "".join(chr(b) for b in payload if 32 <= b < 127).strip()
        # Anything but 17 characters is a partial read, and a partial VIN is
        # worse than no VIN at all.
        return chars if len(chars) == 17 else None

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None
        self._initialised = False
        self._supported = None


# ---------------------------------------------------------------------------
# Mode A -- manufacturer / fleet API
# ---------------------------------------------------------------------------

class ApiReader:
    """A vehicle backend reached over HTTPS (plan §18 Mode A).

    The JSON body decides what exists: a field that is absent or null is not
    exposed, so an EV reporting state of charge but no coolant temperature
    never gets a coolant number invented for it.
    """

    source = "manufacturer_api"
    #: Re-use one request's answer for a moment, so a status call asking for
    #: five metrics is one HTTP round trip rather than five.
    TTL = 2.0

    def __init__(
        self,
        base_url: str,
        *,
        token: str = "",
        path: str = "/telemetry",
        timeout: float = 5.0,
        opener: Optional[Any] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.path = path if path.startswith("/") else "/" + path
        self.timeout = float(timeout)
        self._opener = opener or urllib.request.urlopen
        self._payload: Dict[str, Any] = {}
        self._fetched_at = 0.0
        self.error: str = ""

    def reachable(self) -> bool:
        return bool(self._fetch())

    def _fetch(self) -> Dict[str, Any]:
        now = time.monotonic()
        if self._payload and (now - self._fetched_at) < self.TTL:
            return self._payload
        url = self.base_url + self.path
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        try:
            with self._opener(request, timeout=self.timeout) as response:
                body = response.read()
        except (urllib.error.URLError, OSError, ValueError) as exc:
            self.error = f"the vehicle API at {url} did not answer ({exc})"
            log.warning("car api unreachable", extra_fields={"url": url, "error": str(exc)})
            self._payload = {}
            return self._payload
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            self.error = f"the vehicle API returned something that is not JSON ({exc})"
            self._payload = {}
            return self._payload
        if not isinstance(parsed, dict):
            self.error = "the vehicle API returned a JSON value that is not an object"
            self._payload = {}
            return self._payload
        self._payload = parsed
        self._fetched_at = now
        return parsed

    @staticmethod
    def _number(value: Any) -> Optional[float]:
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                return None
        return None

    def supported(self) -> Set[str]:
        payload = self._fetch()
        return {
            key for key, metric in METRICS.items()
            if metric.api_key and self._number(payload.get(metric.api_key)) is not None
        }

    def read(self, key: str) -> Optional[float]:
        metric = _metric(key)
        if metric is None or not metric.api_key:
            return None
        value = self._number(self._fetch().get(metric.api_key))
        return value if metric.in_range(value) else None

    def dtcs(self) -> Optional[List[str]]:
        payload = self._fetch()
        raw = payload.get("dtcs", payload.get("diagnostics"))
        if raw is None:
            return None
        if isinstance(raw, dict):
            raw = raw.get("codes", raw.get("stored", []))
        if not isinstance(raw, list):
            return None
        codes: List[str] = []
        for item in raw:
            if isinstance(item, dict):
                code = item.get("code") or item.get("dtc")
                if code:
                    codes.append(str(code).strip().upper())
            elif isinstance(item, str):
                codes.append(item.strip().upper())
        return codes

    def mil(self) -> Optional[bool]:
        payload = self._fetch()
        for key in ("mil", "check_engine", "malfunction_indicator"):
            if key in payload:
                return bool(payload[key])
        return None

    def vin(self) -> Optional[str]:
        value = self._fetch().get("vin")
        text = str(value).strip().upper() if value else ""
        return text if len(text) == 17 else None

    def close(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Configuration and factory
# ---------------------------------------------------------------------------

@dataclass
class ReaderConfig:
    """Where the car is, from the environment -- never from model output."""

    simulate: bool = False
    obd_host: str = ""
    obd_port: int = 35145
    obd_protocol: str = "6"
    api_url: str = ""
    api_token: str = ""
    api_path: str = "/telemetry"
    timeout: float = 3.0

    @classmethod
    def from_env(cls, environ: Optional[Dict[str, str]] = None) -> "ReaderConfig":
        env = environ if environ is not None else os.environ
        bridge = (env.get("JARVIS_CAR_OBD") or "").strip()
        host, port, protocol = "", 35145, "6"
        if bridge:
            host, _, rest = bridge.partition(":")
            port_text, _, protocol = rest.partition("@")
            try:
                port = int(port_text) if port_text else 35145
            except ValueError:
                port = 35145
        try:
            timeout = float(env.get("JARVIS_CAR_TIMEOUT") or 3.0)
        except ValueError:
            timeout = 3.0
        return cls(
            simulate=bool((env.get("JARVIS_CAR_SIMULATE") or "").strip()),
            obd_host=host.strip(),
            obd_port=port,
            obd_protocol=(protocol.strip() or "6"),
            api_url=(env.get("JARVIS_CAR_API_URL") or "").strip(),
            api_token=(env.get("JARVIS_CAR_API_TOKEN") or "").strip(),
            api_path=(env.get("JARVIS_CAR_API_PATH") or "/telemetry").strip() or "/telemetry",
            timeout=timeout,
        )


def build_reader(config: Optional[ReaderConfig] = None) -> Tuple[CarReader, str]:
    """Pick the reader the environment asks for, or an honest offline one.

    Returns ``(reader, adapter_error)``: when a configured source cannot be
    reached the gateway degrades to offline but keeps the reason, so a status
    line can name the host that did not answer instead of implying no car
    exists.
    """
    cfg = config or ReaderConfig.from_env()
    if cfg.simulate:
        return SimulatedReader(), ""
    if cfg.api_url:
        reader = ApiReader(cfg.api_url, token=cfg.api_token, path=cfg.api_path, timeout=cfg.timeout)
        if reader.reachable():
            return reader, ""
        return OfflineReader(reader.error), reader.error
    if cfg.obd_host:
        reader = Elm327Reader(cfg.obd_host, cfg.obd_port, protocol=cfg.obd_protocol, timeout=cfg.timeout)
        if reader.reachable():
            return reader, ""
        return OfflineReader(reader.error), reader.error
    return OfflineReader(), ""
