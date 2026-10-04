"""Car / vehicle agent tests (v1.7).

Two guarantees dominate this file.

1. **The decoders are real.** Every J1979 formula and DTC bit layout is checked
   against a hand-computed vector, because a wrong decoding is worse than no
   decoding -- it produces a plausible number that happens to be false.
2. **Nothing is claimed that the source does not expose.** Offline must not say
   "no faults stored"; an OBD-II adapter must not be made to report a range
   figure; a vehicle that stops answering falls back to labelled last-known
   state instead of a live reading.

Mode B is exercised against a socket server that speaks the ELM327 wire
protocol, so the framing, prompt handling and PID-support probe are tested for
real rather than mocked into submission.
"""

from __future__ import annotations

import contextlib
import io
import json
import socket
import threading

import pytest

from jarvis.car import (
    METRICS,
    ApiReader,
    CarGateway,
    Elm327Reader,
    OfflineReader,
    ReaderConfig,
    SimulatedReader,
    build_reader,
    decode_dtc,
    decode_dtc_payload,
    decode_supported_pids,
    get_car_gateway,
    parse_hex_line,
    reassemble_frames,
    reset_car_gateway,
    resolve_metric,
)
from jarvis.car.metrics import (
    dtc_count_from_monitor_status,
    mil_from_monitor_status,
)
from jarvis.core.orchestrator import JarvisCore
from jarvis.core.planner import RulePlanner
from jarvis.tools import build_default_registry
from jarvis.tools.base import ToolContext
from jarvis.tools.car import (
    get_car_diagnostics,
    get_car_service_status,
    get_car_status,
    get_car_telemetry,
    list_car_metrics,
)

ctx = ToolContext()


@pytest.fixture(autouse=True)
def _fresh_gateway(monkeypatch: pytest.MonkeyPatch):
    """No leaked simulate/adapter flags, and one clean gateway per test."""
    for var in ("JARVIS_CAR_SIMULATE", "JARVIS_CAR_OBD", "JARVIS_CAR_API_URL", "JARVIS_CAR_API_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    reset_car_gateway()
    yield
    reset_car_gateway()


@pytest.fixture()
def planner(core: JarvisCore) -> RulePlanner:
    return core.planner


# ---------------------------------------------------------------------------
# An ELM327 adapter on a socket
# ---------------------------------------------------------------------------

#: VIN as bytes, split across two printed lines the way an ELM327 wraps a
#: long payload (the leading 01 is the response counter, not a VIN character).
_VIN_CHARS = "WVUZ123456789ABCD"
_VIN_BYTES = [ord(c) for c in _VIN_CHARS]

#: The vehicle the simulator implements: 04/05/0C/0D/0F under PID 00 byte A+B,
#: 2F under the 0x20 page, 42 under the 0x40 page. 5C (oil) is deliberately
#: absent so a live OBD connection still has an honest gap to report.
_SUPPORT_00 = [0x18, 0x1A, 0x00, 0x00]
_SUPPORT_20 = [0x00, 0x02, 0x00, 0x00]
_SUPPORT_40 = [0x40, 0x00, 0x00, 0x00]

_RESPONSES = {
    "0100": "41 00 " + " ".join(f"{b:02X}" for b in _SUPPORT_00),
    "0120": "41 20 " + " ".join(f"{b:02X}" for b in _SUPPORT_20),
    "0140": "41 40 " + " ".join(f"{b:02X}" for b in _SUPPORT_40),
    "0101": "41 01 81 02",                 # MIL on, 6 stored codes by the counter
    "0104": "41 04 46",                    # 70 -> 27.5 %
    "0105": "41 05 78",                    # 120 -> 80 degC
    "010C": "41 0C 1A F4",                 # 6900 / 4 -> 1725 rpm
    "010D": "41 0D 3C",                    # 60 km/h
    "010F": "BUS ERROR:",                  # advertised but unreadable right now
    "012F": "41 2F AA",                    # 170 * 100 / 255 -> 66.7 %
    "0142": "41 42 31 17",                 # 12567 / 1000 -> 12.567 V
    "03": "43 01 04 04 20 00 00",          # P0104, P0420
    "0902": None,                          # handled separately: two wrapped lines
}


class _ElmSimulator(threading.Thread):
    """A minimal ELM327-over-TCP adapter for exercising the real reader."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self._server = socket.socket()
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(1)
        self.port = self._server.getsockname()[1]
        self.requests: list[str] = []
        self._shutdown = threading.Event()

    def run(self) -> None:
        try:
            conn, _ = self._server.accept()
        except OSError:
            return
        self._server.close()
        buffer = b""
        with contextlib.suppress(OSError):
            while not self._shutdown.is_set():
                chunk = conn.recv(1024)
                if not chunk:
                    break
                buffer += chunk
                while b"\r" in buffer:
                    line, buffer = buffer.split(b"\r", 1)
                    command = line.decode("ascii", errors="replace").strip()
                    if not command:
                        continue
                    self.requests.append(command)
                    conn.sendall(self._reply(command))
        with contextlib.suppress(OSError):
            conn.close()

    def _reply(self, command: str) -> bytes:
        if command.startswith("AT"):
            return b"\rOK\r>"
        if command == "0902":
            tokens = [f"{b:02X}" for b in _VIN_BYTES]
            line_one = " ".join(["49", "02", "01"] + tokens[:9])
            line_two = " ".join(tokens[9:])
            return f"{line_one}\r\n{line_two}\r\n>".encode("ascii")
        body = _RESPONSES.get(command)
        if body is None:
            return b"? >"
        return f"{body}\r\n>".encode("ascii")

    def stop(self) -> None:
        self._shutdown.set()
        with contextlib.suppress(OSError):
            socket.create_connection(("127.0.0.1", self.port), timeout=0.5).close()


@pytest.fixture()
def obd_adapter():
    sim = _ElmSimulator()
    sim.start()
    try:
        yield sim
    finally:
        sim.stop()
        sim.join(timeout=2)


def _dead_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


class _FakeResponse(io.BytesIO):
    """Enough of a urlopen() result for ApiReader: a body and a context manager."""

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class _FakeApi:
    """A manufacturer-API backend whose answers a test controls."""

    def __init__(self, payload: dict | None = None, fail: bool = False) -> None:
        self.payload = payload or {}
        self.fail = fail
        self.calls = 0

    def __call__(self, request, timeout: float = 0):  # noqa: ARG002
        self.calls += 1
        if self.fail:
            raise OSError("connection refused")
        return _FakeResponse(json.dumps(self.payload).encode("utf-8"))


# ---------------------------------------------------------------------------
# Decoders (SAE J1979)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "key,payload,expected",
    [
        ("engine_rpm", [0x1A, 0xF4], 1725.0),
        ("coolant_temp", [0x78], 80.0),
        ("coolant_temp", [0x00], -40.0),
        ("intake_temp", [0x5A], 50.0),
        ("fuel_level", [0xAA], 66.7),
        ("fuel_level", [0xFF], 100.0),
        ("engine_load", [0x46], 27.5),
        ("throttle", [0x00], 0.0),
        ("vehicle_speed", [0x3C], 60.0),
        ("manifold_pressure", [0x64], 100.0),
        ("maf_rate", [0x03, 0xE8], 10.0),
        ("battery_voltage", [0x31, 0x17], 12.567),
        ("barometric_pressure", [0x64], 100.0),
        ("ambient_temp", [0x44], 28.0),
        ("oil_temp", [0x7D], 85.0),
        ("distance_since_mil", [0x14], 20.0),
        ("run_time", [0x03, 0xE8], 1.0),
    ],
)
def test_j1979_decoders(key: str, payload: list[int], expected: float) -> None:
    decode = METRICS[key].decode
    assert decode is not None
    assert decode(payload) == expected


def test_decoder_range_rejects_implausible_values() -> None:
    """A read outside the physical range is no reading at all."""
    metric = METRICS["fuel_level"]
    assert metric.in_range(metric.decode([0x80]))
    assert not metric.in_range(140.0)
    assert not METRICS["battery_voltage"].in_range(4000.0)


@pytest.mark.parametrize(
    "high,low,expected",
    [
        (0x01, 0x04, "P0104"),
        (0x04, 0x20, "P0420"),
        (0x52, 0x34, "C1234"),
        (0xD1, 0x01, "U1101"),
        (0x91, 0x0A, "B110A"),
    ],
)
def test_dtc_decode(high: int, low: int, expected: str) -> None:
    assert decode_dtc(high, low) == expected


def test_dtc_payload_skips_empty_slots_and_stops_at_the_marker() -> None:
    # 0xFF means "this slot is unused"; 0x00 0x00 ends the list. Reading either
    # as a code would invent faults the car never stored.
    assert decode_dtc_payload([0x01, 0x04, 0xFF, 0xFF, 0x04, 0x20, 0x00, 0x00]) == ["P0104", "P0420"]
    assert decode_dtc_payload([0x00, 0x00]) == []
    assert decode_dtc_payload([]) == []


def test_monitor_status_mil_and_count() -> None:
    assert mil_from_monitor_status([0x81, 0x02]) is True
    assert mil_from_monitor_status([0x00, 0x00]) is False
    assert dtc_count_from_monitor_status([0x81, 0x02]) == 6
    assert dtc_count_from_monitor_status([0xFF, 0xFF]) is None


def test_supported_pid_bitmask() -> None:
    # Byte A covers 01-08, byte B covers 09-16, MSB first.
    assert decode_supported_pids([0xFF, 0x00, 0x00, 0x00]) == {f"{n:02X}" for n in range(1, 9)}
    assert decode_supported_pids([0x00, 0x90, 0x00, 0x00]) == {"09", "0C"}
    assert decode_supported_pids([0x00, 0x02, 0x00, 0x00], base=0x20) == {"2F"}


def test_resolve_metric_aliases() -> None:
    assert resolve_metric("fuel") == "fuel_level"
    assert resolve_metric("Petrol") == "fuel_level"
    assert resolve_metric("engine_rpm") == "engine_rpm"
    assert resolve_metric(" mileage ") == "odometer"
    assert resolve_metric("warp-drive") is None
    assert resolve_metric("") is None


# ---------------------------------------------------------------------------
# ELM327 wire helpers
# ---------------------------------------------------------------------------

def test_parse_hex_line_ignores_prose() -> None:
    assert parse_hex_line("41 0C 1A F4") == [0x41, 0x0C, 0x1A, 0xF4]
    assert parse_hex_line("BUS ERROR:") == []
    assert parse_hex_line("NO DATA") == []
    assert parse_hex_line("?") == []


def test_reassemble_frames_handles_single_and_multi() -> None:
    # An ISO-15765 single frame carries its length in the low nibble of byte 0.
    assert reassemble_frames(["03 41 05 78"]) == [0x41, 0x05, 0x78]
    # A 20-byte first frame (0x10 0x14) followed by two consecutive frames.
    joined = reassemble_frames([
        "10 14 49 02 01 57 56 55",
        "21 5A 31 32 33 34 35 36",
        "22 37 38 39 41 42 43 44",
    ])
    assert joined == [0x49, 0x02, 0x01] + _VIN_BYTES
    assert reassemble_frames(["10 14 49 02 01", "22 37 38"]) is None   # out of order
    assert reassemble_frames(["10 14 49 02 01 57"]) is None            # truncated
    assert reassemble_frames([]) is None


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------

def test_offline_reader_claims_nothing() -> None:
    reader = OfflineReader()
    assert reader.reachable() is False
    assert reader.supported() == set()
    assert reader.read("fuel_level") is None
    # None, not [] -- "I do not know" must not become "no faults".
    assert reader.dtcs() is None
    assert reader.mil() is None
    assert reader.vin() is None


def test_simulated_reader_is_labelled_and_partial() -> None:
    reader = SimulatedReader()
    assert reader.reachable() is True
    assert reader.read("fuel_level") == 42.0
    assert "oil_temp" not in reader.supported()
    assert reader.read("oil_temp") is None


def test_obd_reader_against_a_real_socket(obd_adapter: _ElmSimulator) -> None:
    reader = Elm327Reader("127.0.0.1", obd_adapter.port, timeout=2.0)
    try:
        assert reader.reachable() is True
        supported = reader.supported()
        assert {"fuel_level", "coolant_temp", "engine_rpm", "battery_voltage"} <= supported
        assert "oil_temp" not in supported, "the car never advertised PID 5C"
        assert reader.read("fuel_level") == 66.7
        assert reader.read("engine_rpm") == 1725.0
        assert reader.read("coolant_temp") == 80.0
        assert reader.read("battery_voltage") == 12.567
        assert reader.read("oil_temp") is None, "never advertised, never asked for"
        # 0F is advertised but the bus refused it: an honest None, not a guess.
        assert reader.read("intake_temp") is None
        assert reader.dtcs() == ["P0104", "P0420"]
        assert reader.mil() is True
        assert reader.vin() == _VIN_CHARS
    finally:
        reader.close()
    assert "ATZ" in obd_adapter.requests
    assert "012F" in obd_adapter.requests


def test_obd_reader_unreachable_reports_why() -> None:
    reader = Elm327Reader("127.0.0.1", _dead_port(), timeout=0.5)
    assert reader.reachable() is False
    assert "cannot reach the OBD-II adapter" in reader.error


def test_api_reader_supports_only_what_the_json_carries() -> None:
    api = _FakeApi({"fuel_level": 55, "range": 240, "odometer": "12340"})
    reader = ApiReader("https://car.example.test", token="t", opener=api)
    assert reader.reachable() is True
    assert reader.supported() == {"fuel_level", "range", "odometer"}
    assert reader.read("fuel_level") == 55.0
    assert reader.read("odometer") == 12340.0, "a numeric string is still a reading"
    # An EV backend with no coolant field must not get one invented.
    assert "coolant_temp" not in reader.supported()
    assert reader.read("coolant_temp") is None
    assert reader.dtcs() is None, "no diagnostics field means unknown"


def test_api_reader_reads_diagnostics_and_mil() -> None:
    api = _FakeApi({"fuel_level": 30, "dtcs": [{"code": "p0420"}, "P0104"], "mil": True, "vin": "WVUZ123456789ABCD"})
    reader = ApiReader("https://car.example.test", opener=api)
    assert reader.dtcs() == ["P0420", "P0104"]
    assert reader.mil() is True
    assert reader.vin() == "WVUZ123456789ABCD"


def test_api_reader_rejects_garbage() -> None:
    broken = _FakeApi("not-json")
    broken.payload = None  # type: ignore[assignment]

    def _garbage(request, timeout: float = 0):  # noqa: ARG001
        return _FakeResponse(b"<html>login required</html>")

    reader = ApiReader("https://car.example.test", opener=_garbage)
    assert reader.reachable() is False
    assert "not JSON" in reader.error

    def _not_an_object(request, timeout: float = 0):  # noqa: ARG001
        return _FakeResponse(b"[1, 2, 3]")

    reader = ApiReader("https://car.example.test", opener=_not_an_object)
    assert reader.reachable() is False
    assert "not an object" in reader.error


def test_api_reader_failure_falls_back_to_no_data() -> None:
    api = _FakeApi({"fuel_level": 42}, fail=True)
    reader = ApiReader("https://car.example.test", opener=api)
    assert reader.reachable() is False
    assert reader.supported() == set()
    assert "did not answer" in reader.error


def test_reader_config_parses_the_adapter_spec() -> None:
    cfg = ReaderConfig.from_env({"JARVIS_CAR_OBD": "192.168.1.9:35145@7", "JARVIS_CAR_TIMEOUT": "1.5"})
    assert (cfg.obd_host, cfg.obd_port, cfg.obd_protocol) == ("192.168.1.9", 35145, "7")
    assert cfg.timeout == 1.5
    blank = ReaderConfig.from_env({"JARVIS_CAR_OBD": "carbridge.local"})
    assert (blank.obd_host, blank.obd_port, blank.obd_protocol) == ("carbridge.local", 35145, "6")
    assert ReaderConfig.from_env({"JARVIS_CAR_TIMEOUT": "soon"}).timeout == 3.0


def test_build_reader_prefers_the_api_then_obd_then_offline() -> None:
    api = _FakeApi({"fuel_level": 10})
    reader, error = build_reader(ReaderConfig(api_url="https://car.example.test"), )
    assert isinstance(reader, OfflineReader) and error
    reader, error = build_reader(ReaderConfig(simulate=True))
    assert isinstance(reader, SimulatedReader) and error == ""
    reader, error = build_reader(ReaderConfig(obd_host="127.0.0.1", obd_port=_dead_port(), timeout=0.5))
    assert isinstance(reader, OfflineReader)
    assert "cannot reach the OBD-II adapter" in error
    del api


def test_build_reader_reaches_a_live_obd_adapter(obd_adapter: _ElmSimulator) -> None:
    reader, error = build_reader(ReaderConfig(obd_host="127.0.0.1", obd_port=obd_adapter.port, timeout=2.0))
    assert not error
    assert isinstance(reader, Elm327Reader)
    reader.close()


# ---------------------------------------------------------------------------
# Gateway
# ---------------------------------------------------------------------------

def test_gateway_is_offline_by_default() -> None:
    gw = get_car_gateway()
    assert gw.offline is True
    assert gw.mode == "offline"
    assert gw.connected is False
    assert gw.supported() == set()
    assert "range" in gw.unsupported()


def test_offline_gateway_invents_nothing() -> None:
    gw = get_car_gateway()
    reading = gw.read("fuel_level")
    assert reading.value is None
    assert reading.source == "unavailable"
    assert "no car adapter" in reading.detail
    assert gw.summary_line().startswith("I have no car telemetry")


def test_last_known_state_is_labelled_not_presented_as_live() -> None:
    """A car that stops answering must degrade to labelled memory, not a live read."""
    api = _FakeApi({"fuel_level": 42.0, "range": 210.0})
    reader = ApiReader("https://car.example.test", opener=api)
    gw = CarGateway(reader)
    assert gw.read("fuel_level").source == "live"
    assert gw.read("range").source == "live"

    api.fail = True                       # the garage door closed
    api.payload = {}
    reader._fetched_at = 0.0              # let the cached answer expire
    assert gw.connected is False

    reading = gw.read("fuel_level")
    assert reading.value == 42.0
    assert reading.source == "last_known", "a remembered value must never say 'live'"
    assert "did not answer" in (gw.status()["adapter_error"] or "")


async def test_status_labels_a_remembered_vehicle_as_not_live() -> None:
    api = _FakeApi({"fuel_level": 42.0})
    reader = ApiReader("https://car.example.test", opener=api)
    reset_car_gateway(CarGateway(reader))
    result = await get_car_status(ctx)
    assert "42 %" in result.message

    api.fail = True
    api.payload = {}
    reader._fetched_at = 0.0
    again = await get_car_status(ctx)
    assert again.output["connected"] is False
    assert "last state the car reported" in again.message


def test_simulate_mode_reports_a_subset_honestly() -> None:
    gw = CarGateway(SimulatedReader())
    assert gw.mode == "simulate"
    assert gw.read("fuel_level").source == "simulated"
    oil = gw.read("oil_temp")
    assert oil.value is None
    assert "does not report it" in oil.detail
    assert "simulated" in gw.summary_line()


def test_range_never_comes_from_an_obd_adapter(obd_adapter: _ElmSimulator) -> None:
    """Estimated range is a manufacturer-API figure; a PID read must not fake one."""
    gw = CarGateway(Elm327Reader("127.0.0.1", obd_adapter.port, timeout=2.0))
    reading = gw.read("range")
    assert reading.value is None
    assert "manufacturer vehicle API" in reading.detail
    gw.reader.close()


def test_capabilities_explains_every_gap(obd_adapter: _ElmSimulator) -> None:
    gw = CarGateway(Elm327Reader("127.0.0.1", obd_adapter.port, timeout=2.0))
    caps = gw.capabilities()
    keys = {m["key"] for m in caps["supported"]}
    assert {"fuel_level", "engine_rpm", "coolant_temp", "battery_voltage"} <= keys
    gaps = {m["key"]: m["reason"] for m in caps["not_reported"]}
    assert "oil_temp" in gaps and "PID 01 5C" in gaps["oil_temp"]
    assert "odometer" in gaps
    gw.reader.close()


def test_diagnostics_keeps_unknown_apart_from_none() -> None:
    assert get_car_gateway().diagnostics()["available"] is False
    gw = CarGateway(SimulatedReader(dtcs=[], mil=False))
    diag = gw.diagnostics()
    assert diag["available"] is True and diag["count"] == 0


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

async def test_get_car_status_offline_is_honest() -> None:
    result = await get_car_status(ctx)
    assert result.ok is True                      # the call worked...
    assert result.output["connected"] is False    # ...the reading did not exist
    assert "not connected" in result.message.lower()
    assert result.output["mode"] == "offline"


async def test_get_car_status_with_adapter(obd_adapter: _ElmSimulator) -> None:
    reset_car_gateway(CarGateway(Elm327Reader("127.0.0.1", obd_adapter.port, timeout=2.0)))
    result = await get_car_status(ctx)
    assert "66.7 %" in result.message
    assert "1,725 rpm" in result.message
    assert result.output["connected"] is True
    assert result.output["vin"] == _VIN_CHARS
    assert result.output["diagnostics"]["codes"] == ["P0104", "P0420"]
    assert "not a mechanical inspection" not in result.message
    get_car_gateway().reader.close()


async def test_get_car_status_metric_routing() -> None:
    result = await get_car_status(ctx, metric="fuel")
    assert result.tool == "get_car_telemetry", "a named reading is answered by the reading tool"
    assert result.output["readings"][0]["key"] == "fuel_level"

    diag = await get_car_status(ctx, metric="diagnostics")
    assert diag.tool == "get_car_diagnostics"

    unknown = await get_car_status(ctx, metric="turbo-boost")
    assert unknown.ok is False
    assert "turbo-boost" in (unknown.error or "")


async def test_get_car_telemetry_names_what_it_cannot_read() -> None:
    result = await get_car_telemetry(ctx, metric="fuel,coolant")
    assert result.ok is True
    keys = [r["key"] for r in result.output["readings"]]
    assert keys == ["fuel_level", "coolant_temp"]
    assert all(r["value"] is None for r in result.output["readings"])
    assert "could not read those car values" in result.message.lower()

    unknown = await get_car_telemetry(ctx, metric="flux")
    assert unknown.ok is False
    assert "flux" in (unknown.error or "")


async def test_list_car_metrics_offline_then_online(obd_adapter: _ElmSimulator) -> None:
    offline = await list_car_metrics(ctx)
    assert "no car telemetry is available" in offline.message.lower()

    reset_car_gateway(CarGateway(Elm327Reader("127.0.0.1", obd_adapter.port, timeout=2.0)))
    online = await list_car_metrics(ctx)
    assert online.output["supported"]
    assert "not available from it" in online.message
    online_args = {m["key"] for m in online.output["not_reported"]}
    assert "oil_temp" in online_args
    get_car_gateway().reader.close()


async def test_get_car_diagnostics_offline_does_not_claim_clean_bill() -> None:
    result = await get_car_diagnostics(ctx)
    assert result.ok is True
    assert result.output["available"] is False
    assert "cannot read" in result.message.lower()
    assert "no stored diagnostic trouble codes" not in result.message


async def test_get_car_diagnostics_with_codes() -> None:
    reset_car_gateway(CarGateway(SimulatedReader()))
    result = await get_car_diagnostics(ctx)
    assert result.output["codes"] == ["P0420"]
    assert "P0420" in result.message
    assert "malfunction indicator lamp is on" in result.message
    assert "not a mechanical inspection" in result.message


async def test_get_car_diagnostics_reports_a_clean_sheet_when_it_is_clean() -> None:
    reset_car_gateway(CarGateway(SimulatedReader(dtcs=[], mil=False)))
    result = await get_car_diagnostics(ctx)
    assert "no stored diagnostic trouble codes" in result.message
    assert "lamp is off" in result.message


async def test_get_car_service_status_offline_and_simulated() -> None:
    offline = await get_car_service_status(ctx)
    assert "cannot give you a car service summary" in offline.message.lower()
    assert offline.output["note"].startswith("Telemetry only")

    reset_car_gateway(CarGateway(SimulatedReader()))
    online = await get_car_service_status(ctx)
    assert "61,420 km" in online.message
    assert "missing" in online.output and "oil_temp" in online.output["missing"]


# ---------------------------------------------------------------------------
# Registration and risk
# ---------------------------------------------------------------------------

CAR_TOOLS = {
    "get_car_status",
    "get_car_telemetry",
    "list_car_metrics",
    "get_car_diagnostics",
    "get_car_service_status",
}


def test_car_tools_are_registered_under_the_car_agent() -> None:
    registry = build_default_registry()
    for name in CAR_TOOLS:
        spec = registry.get(name)
        assert spec is not None, f"{name} should be registered"
        assert spec.agent == "car"


def test_the_car_surface_is_read_only() -> None:
    """Phase 10 asks for a read-only prototype: nothing here may actuate."""
    registry = build_default_registry()
    for name in CAR_TOOLS:
        spec = registry.get(name)
        assert spec is not None
        assert int(spec.risk) == 0, f"{name} must stay in the READ tier"
        assert spec.requires_confirmation is False
    assert not [n for n in registry.names() if any(w in n for w in ("unlock", "horn", "ignition", "start_car"))]


def test_car_agent_capabilities_match_the_registry() -> None:
    from jarvis.agents.base import build_agents

    registry = build_default_registry()
    agent = build_agents(registry)["car"]
    assert agent.capabilities() == CAR_TOOLS
    assert all(registry.has(name) for name in agent.capabilities())


# ---------------------------------------------------------------------------
# Planner routing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "phrase,tool",
    [
        ("check my car", "get_car_status"),
        ("how is the car", "get_car_status"),
        ("vehicle status", "get_car_status"),
        ("check my car fuel", "get_car_telemetry"),
        ("what is my car charge", "get_car_telemetry"),
        ("whats the car battery", "get_car_telemetry"),
        ("what is my engine rpm", "get_car_telemetry"),
        ("is the coolant ok", "get_car_telemetry"),
        ("check my car diagnostics", "get_car_diagnostics"),
        ("are there any car error codes", "get_car_diagnostics"),
        ("is the check engine light on", "get_car_diagnostics"),
        ("when is my car service due", "get_car_service_status"),
        ("book nothing but check the car maintenance", "get_car_service_status"),
    ],
)
def test_planner_routes_car_phrases(planner: RulePlanner, phrase: str, tool: str) -> None:
    plan = planner.parse(phrase)
    assert plan.actions, f"{phrase!r} produced no action"
    assert plan.actions[0].tool == tool


def test_fuel_routes_to_a_fuel_reading(planner: RulePlanner) -> None:
    plan = planner.parse("check my car fuel")
    assert plan.actions[0].params["metric"] == "fuel_level"


def test_the_car_rule_does_not_steal_other_devices(planner: RulePlanner) -> None:
    assert planner.parse("what is my battery").actions[0].tool == "get_battery_status"
    assert planner.parse("what is the status of my robots").actions[0].tool in {
        "get_robot_status",
        "list_robots",
    }
    assert planner.parse("stop everything").actions[0].tool == "emergency_stop"


def test_a_memory_about_the_car_stays_a_memory(planner: RulePlanner) -> None:
    plan = planner.parse("remember that my car is a diesel")
    assert plan.actions[0].tool == "remember_fact"


def test_common_words_alone_never_reach_the_car(planner: RulePlanner) -> None:
    """"charging", "diagnostic" and "mileage" are ordinary words without the car."""
    for phrase in ("what is my charging speed", "find diagnostic notes", "what is my mileage"):
        tools = {action.tool for action in planner.parse(phrase).actions}
        assert not tools & CAR_TOOLS, f"{phrase!r} reached the car: {tools}"


def test_planner_only_emits_registered_car_tools(planner: RulePlanner) -> None:
    for phrase in ("check my car", "check my car fuel", "car diagnostics", "car service"):
        for action in planner.parse(phrase).actions:
            assert planner.tools.has(action.tool), action.tool


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------

async def test_e2e_car_status_offline(core: JarvisCore, make_request) -> None:
    response = await core.handle(make_request("check my car"))
    assert response.actions and response.actions[0].ok
    assert "not connected" in response.reply.lower()
    assert response.needs_confirmation is False


async def test_e2e_car_status_with_a_vehicle(core: JarvisCore, make_request, obd_adapter: _ElmSimulator) -> None:
    reset_car_gateway(CarGateway(Elm327Reader("127.0.0.1", obd_adapter.port, timeout=2.0)))
    response = await core.handle(make_request("check my car"))
    assert "Fuel: 66.7 %" in response.reply
    assert response.needs_confirmation is False
    get_car_gateway().reader.close()


async def test_e2e_car_fuel_question(core: JarvisCore, make_request) -> None:
    response = await core.handle(make_request("whats my car fuel"))
    assert response.actions[0].tool == "get_car_telemetry"
    assert "could not read" in response.reply.lower()


# ---------------------------------------------------------------------------
# posture(): a polled health endpoint must not become car traffic
# ---------------------------------------------------------------------------

def test_posture_resolves_nothing() -> None:
    gw = CarGateway()
    posture = gw.posture()
    assert posture["checked"] is False
    assert posture["probed"] is False
    assert posture["supported_metrics"] is None
    assert gw._reader is None, "posture() must not construct or contact a transport"


def test_posture_reports_configuration_without_claiming_an_answer() -> None:
    gw = CarGateway(config=ReaderConfig(obd_host="10.0.0.5", obd_port=35145))
    posture = gw.posture()
    assert posture["configured"] is True
    assert posture["via"] == "obd2"
    assert posture["adapter"] == "10.0.0.5:35145"
    # Configured is not the same as reachable, and neither is "not answering":
    # until something asks the adapter, the honest state is "not read yet".
    assert posture["checked"] is False
    assert posture["connected"] is False
    assert gw._reader is None


async def test_posture_reflects_a_connection_without_reading_again(
    obd_adapter: _ElmSimulator,
) -> None:
    reader = Elm327Reader("127.0.0.1", obd_adapter.port, timeout=2.0)
    gw = CarGateway(reader)
    assert gw.read("fuel_level").value == 66.7          # this is what probes
    before = len(obd_adapter.requests)

    posture = gw.posture()
    assert posture["checked"] is True and posture["connected"] is True
    assert posture["probed"] is True
    assert posture["supported_metrics"] == len(gw.supported())
    assert len(obd_adapter.requests) == before, "posture() must send nothing"
    reader.close()
