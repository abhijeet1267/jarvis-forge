"""Home automation domain (v1.5).

A thin, honest bridge to a local home gateway. See :mod:`jarvis.home.gateway`.
"""

from .gateway import (
    ControlOutcome,
    HomeGateway,
    HomeTransport,
    OfflineTransport,
    SimulatedTransport,
    TransportResult,
    ZoneDevice,
    default_inventory,
    get_home_gateway,
    reset_home_gateway,
)

__all__ = [
    "ControlOutcome",
    "HomeGateway",
    "HomeTransport",
    "OfflineTransport",
    "SimulatedTransport",
    "TransportResult",
    "ZoneDevice",
    "default_inventory",
    "get_home_gateway",
    "reset_home_gateway",
]
