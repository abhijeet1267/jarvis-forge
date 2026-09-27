"""Vehicle telemetry metrics (plan §18, v1.7).

Each metric names *where* a value can legitimately come from. A metric with
``pid`` set is readable over OBD-II (Mode B); one with ``api_key`` set is
readable from a manufacturer API (Mode A). A metric with neither is declared
here only so the honest "not exposed by this adapter" answer has something to
point at.

The formulas are the SAE J1979 mode 01 A/B byte equations, not approximations.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional


@dataclass(frozen=True)
class Metric:
    key: str
    label: str
    unit: str
    service: str = "01"
    pid: str = ""
    api_key: str = ""
    decode: Optional[Callable[[List[int]], Optional[float]]] = None
    #: Values below this are implausible for a healthy read and are dropped.
    sane_min: Optional[float] = None
    sane_max: Optional[float] = None

    def in_range(self, value: Optional[float]) -> bool:
        if value is None:
            return False
        if self.sane_min is not None and value < self.sane_min:
            return False
        if self.sane_max is not None and value > self.sane_max:
            return False
        return True


def _bytes(payload: List[int]) -> List[int]:
    return list(payload) + [0, 0, 0, 0]


def _ab(payload: List[int]) -> List[int]:
    b = _bytes(payload)
    return [b[0], b[1]]


# --- SAE J1979 mode 01 decoders --------------------------------------------
def _pct_a(payload: List[int]) -> float:      # A * 100 / 255
    return round(_ab(payload)[0] * 100.0 / 255.0, 1)


def _temp_a(payload: List[int]) -> float:     # A - 40 degC
    return float(_ab(payload)[0] - 40)


def _raw_a(payload: List[int]) -> float:      # A, untransformed
    return float(_ab(payload)[0])


def _ab16(payload: List[int]) -> float:       # (256A + B)
    a, b = _ab(payload)
    return float(a * 256 + b)


def _rpm(payload: List[int]) -> float:        # (256A + B) / 4
    return round(_ab16(payload) / 4.0, 1)


def _volts(payload: List[int]) -> float:      # (256A + B) / 1000
    return round(_ab16(payload) / 1000.0, 3)


def _seconds(payload: List[int]) -> float:    # (256A + B) / 1000 s
    return round(_ab16(payload) / 1000.0, 1)


def _maf(payload: List[int]) -> float:        # (256A + B) / 100 g/s
    return round(_ab16(payload) / 100.0, 2)


METRICS: Dict[str, Metric] = {
    m.key: m
    for m in (
        Metric("fuel_level", "Fuel", "%", "01", "2F", "fuel_level", _pct_a, 0.0, 100.0),
        Metric("engine_load", "Engine load", "%", "01", "04", "engine_load", _pct_a, 0.0, 100.0),
        Metric("coolant_temp", "Coolant temperature", "°C", "01", "05", "coolant_temp", _temp_a, -40.0, 215.0),
        Metric("intake_temp", "Intake air temperature", "°C", "01", "0F", "intake_temp", _temp_a, -40.0, 130.0),
        Metric("manifold_pressure", "Intake manifold pressure", "kPa", "01", "0B", "", _raw_a, 0.0, 255.0),
        Metric("engine_rpm", "Engine RPM", "rpm", "01", "0C", "engine_rpm", _rpm, 0.0, 12000.0),
        Metric("vehicle_speed", "Vehicle speed", "km/h", "01", "0D", "speed", _raw_a, 0.0, 300.0),
        Metric("maf_rate", "Mass air flow", "g/s", "01", "10", "", _maf, 0.0, 655.0),
        Metric("throttle", "Throttle position", "%", "01", "11", "throttle", _pct_a, 0.0, 100.0),
        Metric("run_time", "Engine run time", "s", "01", "1F", "", _seconds, 0.0, None),
        Metric("distance_since_mil", "Distance since MIL on", "km", "01", "21", "", _raw_a, 0.0, 255.0),
        Metric("barometric_pressure", "Barometric pressure", "kPa", "01", "33", "", _raw_a, 0.0, 255.0),
        Metric("battery_voltage", "Battery / charging voltage", "V", "01", "42", "battery_voltage", _volts, 0.0, 65.535),
        Metric("ambient_temp", "Ambient temperature", "°C", "01", "46", "ambient_temp", _temp_a, -40.0, 130.0),
        Metric("oil_temp", "Engine oil temperature", "°C", "01", "5C", "oil_temp", _temp_a, -40.0, 215.0),
        # --- Mode A only: the instrument cluster / backend computes these. ---
        Metric("range", "Estimated range", "km", "", "", "range", None, 0.0, 5000.0),
        Metric("charge_level", "State of charge", "%", "", "", "soc", None, 0.0, 100.0),
        Metric("odometer", "Odometer", "km", "", "", "odometer", None, 0.0, None),
    )
}

#: Metrics a read-only prototype exposes at all, in reporting order.
STATUS_ORDER = (
    "fuel_level",
    "range",
    "odometer",
    "battery_voltage",
    "coolant_temp",
    "engine_rpm",
)

TELEMETRY_ALIASES: Dict[str, str] = {
    "fuel": "fuel_level",
    "fuel_level": "fuel_level",
    "petrol": "fuel_level",
    "diesel": "fuel_level",
    "tank": "fuel_level",
    "range": "range",
    "odometer": "odometer",
    "mileage": "odometer",
    "rpm": "engine_rpm",
    "engine_rpm": "engine_rpm",
    "revs": "engine_rpm",
    "speed": "vehicle_speed",
    "vehicle_speed": "vehicle_speed",
    "coolant": "coolant_temp",
    "coolant_temp": "coolant_temp",
    "temperature": "coolant_temp",
    "engine_temp": "coolant_temp",
    "oil": "oil_temp",
    "oil_temp": "oil_temp",
    "voltage": "battery_voltage",
    "battery": "battery_voltage",
    "battery_voltage": "battery_voltage",
    "throttle": "throttle",
    "charge": "charge_level",
    "charge_level": "charge_level",
    "soc": "charge_level",
    "maf": "maf_rate",
    "maf_rate": "maf_rate",
    "load": "engine_load",
    "engine_load": "engine_load",
    "intake_temp": "intake_temp",
    "ambient_temp": "ambient_temp",
    "barometric": "barometric_pressure",
    "manifold_pressure": "manifold_pressure",
    "run_time": "run_time",
}


def resolve_metric(name: str) -> Optional[str]:
    """Map a loose user word onto a metric key, or None if unknown."""
    key = (name or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not key:
        return None
    if key in METRICS:
        return key
    return TELEMETRY_ALIASES.get(key)


# ---------------------------------------------------------------------------
# SAE J1979 diagnostic trouble codes
# ---------------------------------------------------------------------------

#: The two-bit prefix of a DTC's high byte, per SAE J2012.
DTC_PREFIXES = ("P", "C", "B", "U")
_HEX = "0123456789ABCDEF"


def decode_dtc(high: int, low: int) -> str:
    """Decode one raw J1979 DTC pair into its 5-character code.

    Byte A: bits 7-6 the letter, bits 5-4 the first digit, bits 3-0 the second
    digit (as hex); byte B supplies the remaining two digits. So 0x01 0x04 is
    P0104 and 0x12 0x34 is C1234.
    """
    letter = DTC_PREFIXES[(high >> 6) & 0x03]
    first = (high >> 4) & 0x03
    second = _HEX[high & 0x0F]
    third = _HEX[(low >> 4) & 0x0F]
    fourth = _HEX[low & 0x0F]
    return f"{letter}{first}{second}{third}{fourth}"


def decode_dtc_payload(payload: List[int]) -> List[str]:
    """Decode a mode 03 / mode 0A response body into DTC codes.

    A shows 0xFF (no code) is skipped, which is how an adapter reports an empty
    slot -- reading those as codes would invent faults the car never stored.
    """
    codes: List[str] = []
    pairs = [payload[i:i + 2] for i in range(0, len(payload) - 1, 2)]
    for pair in pairs:
        high, low = pair
        if high == 0x00 and low == 0x00:
            break                       # end-of-list marker
        if high == 0xFF:
            continue                    # A==0xFF means "this slot is empty"
        codes.append(decode_dtc(high, low))
    return codes


def mil_from_monitor_status(payload: List[int]) -> Optional[bool]:
    """From PID 01 01 byte A: bit 7 is the malfunction indicator lamp."""
    if not payload:
        return None
    return bool(payload[0] & 0x80)


def dtc_count_from_monitor_status(payload: List[int]) -> Optional[int]:
    """From PID 01 01: the DTC count is (A & 0x03) * 4 + B."""
    b = _bytes(payload)
    a, second = b[0], b[1]
    if a == 0xFF:
        return None
    return (a & 0x03) * 4 + second


def decode_supported_pids(payload: List[int], base: int = 0x00) -> set:
    """Turn a "PID 00" response into the set of supported PIDs (as hex).

    Four bytes of bitmask cover 32 PIDs starting after ``base``, most
    significant bit first: byte A is ``base``+1..+8, byte B is +9..+16 and so
    on. A single response therefore only covers 0x01-0x20, which is why the
    gateway probes ``base`` 0x00, 0x20 and 0x40 to learn about PIDs such as 2F
    and 5C.
    """
    supported = set()
    b = _bytes(payload)
    for byte_index, mask_byte in enumerate(b[:4]):
        for bit in range(8):
            pid_number = base + byte_index * 8 + bit + 1
            if pid_number > 0xFF:
                break
            if mask_byte & (0x80 >> bit):
                supported.add(f"{pid_number:02X}")
    return supported
