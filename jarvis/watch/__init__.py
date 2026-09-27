"""Watch companion control (v1.8).

An outbox-based, offline-by-default link to a paired smartwatch: the companion
calls in to collect prompts and reports back what its wearer decided. Delivery
stages are never collapsed into one another, so "queued" is never reported as
"shown on your watch".
"""

from .gateway import (
    CHOICES,
    DEVICE_ID,
    KINDS,
    STATES,
    WATCH_ROUTINES,
    BridgeTransport,
    DeliveryResult,
    QueueTransport,
    SimulatedTransport,
    WatchGateway,
    WatchItem,
    get_watch_gateway,
    reset_watch_gateway,
)

__all__ = [
    "CHOICES",
    "DEVICE_ID",
    "KINDS",
    "STATES",
    "WATCH_ROUTINES",
    "BridgeTransport",
    "DeliveryResult",
    "QueueTransport",
    "SimulatedTransport",
    "WatchGateway",
    "WatchItem",
    "get_watch_gateway",
    "reset_watch_gateway",
]
