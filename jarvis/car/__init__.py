"""Car / vehicle telemetry (v1.7).

A read-only, offline-by-default view of one vehicle, reached either over an
OBD-II adapter (plan §18 Mode B) or a manufacturer vehicle API (Mode A).
"""

from .gateway import (
    ALL_METRICS,
    CarGateway,
    Reading,
    get_car_gateway,
    reset_car_gateway,
)
from .metrics import METRICS, Metric, decode_dtc, decode_dtc_payload, decode_supported_pids, resolve_metric
from .readers import (
    ApiReader,
    CarReader,
    Elm327Reader,
    OfflineReader,
    ReaderConfig,
    SIMULATED_DTCS,
    SIMULATED_METRICS,
    SimulatedReader,
    build_reader,
    parse_hex_line,
    reassemble_frames,
)

__all__ = [
    "ALL_METRICS",
    "METRICS",
    "ApiReader",
    "CarGateway",
    "CarReader",
    "Elm327Reader",
    "Metric",
    "OfflineReader",
    "ReaderConfig",
    "Reading",
    "SIMULATED_DTCS",
    "SIMULATED_METRICS",
    "SimulatedReader",
    "build_reader",
    "decode_dtc",
    "decode_dtc_payload",
    "decode_supported_pids",
    "get_car_gateway",
    "parse_hex_line",
    "reassemble_frames",
    "reset_car_gateway",
    "resolve_metric",
]
