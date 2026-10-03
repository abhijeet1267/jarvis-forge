"""Device registry (section 6).

Every connected device gets a stable identity and an explicit capability list.
The policy engine consults this to reject actions a device cannot perform.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..core.models import DeviceSpec, utcnow
from ..db.database import get_db
from ..logging_setup import get_logger

log = get_logger("registry.devices")

#: The machine this process is running on, as its own device record.
#:
#: A single source because both the pairing layer and the clients have to agree on it:
#: the row is what a person reads as "this laptop", and revoking it is not disconnecting
#: a gadget -- it is the host deleting its own identity, with no client left that could
#: restore it. Measured 2026-10-03: the HTTP route had no such check and a single POST
#: set the running laptop to ``revoked``.
HOST_DEVICE_ID = "laptop_main"

# Devices present in the v0.1 reference deployment.
DEFAULT_DEVICES: List[DeviceSpec] = [
    DeviceSpec(
        device_id="laptop_main",
        type="computer",
        owner="user",
        platform="macos",
        trust_level="high",
        capabilities=[
            "app_launch",
            "app_close",
            "file_operations",
            "file_search",
            "terminal",
            "screen",
            "system_status",
            "battery",
            "screenshot",
            "media",
        ],
    ),
    DeviceSpec(
        device_id="phone_main",
        type="android",
        platform="android",
        trust_level="medium",
        online=False,
        capabilities=[
            "notifications",
            "file_transfer",
            "intents",
            "battery_status",
            "media",
            "camera",
        ],
        connection=["local_transfer"],
    ),
    DeviceSpec(
        device_id="watch_main",
        type="smartwatch",
        platform="wearos",
        trust_level="medium",
        online=False,
        capabilities=["voice", "notifications", "vibration", "confirmation"],
    ),
    DeviceSpec(
        device_id="home_gateway",
        type="home_hub",
        platform="home_assistant",
        trust_level="medium",
        online=False,
        capabilities=[
            "lights",
            "fans",
            "plugs",
            "sensors",
            "temperature",
            "motion",
            "energy",
            "routines",
        ],
        connection=["mqtt", "home_assistant"],
    ),
    DeviceSpec(
        device_id="car_main",
        type="vehicle",
        platform="obd2",
        trust_level="low",
        online=False,
        capabilities=["fuel_level", "battery_status", "diagnostics", "range"],
        connection=["manufacturer_api", "obd2"],
    ),
]


class DeviceRegistry:
    """Persisted registry of known devices.

    One instance is shared process-wide (see :func:`get_device_registry`).
    Separate instances keep separate in-memory caches, which means a device
    paired at runtime would be invisible to a component holding a different
    instance -- the exact bug that appeared once pairing was added.
    """

    def __init__(self) -> None:
        self.db = get_db()
        self._cache: Dict[str, DeviceSpec] = {}
        self._seed_defaults()

    def refresh(self) -> None:
        """Re-read the device table into the cache.

        Needed after another component writes a device directly.
        """
        for row in self.db.query("SELECT * FROM devices"):
            self._cache[row["device_id"]] = self._row_to_spec(row)

    def _seed_defaults(self) -> None:
        for device in DEFAULT_DEVICES:
            self.register(device, persist=False)
        # Merge any previously persisted rows (they may carry updated status).
        self.refresh()

    def register(self, device: DeviceSpec, persist: bool = True) -> None:
        self._cache[device.device_id] = device
        if persist:
            self.db.execute(
                """
                INSERT INTO devices
                    (device_id, type, owner, platform, trust_level, capabilities,
                     online, metadata, fingerprint, auth_state, paired_at,
                     last_seen, key_ref, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(device_id) DO UPDATE SET
                    type=excluded.type,
                    owner=excluded.owner,
                    platform=excluded.platform,
                    trust_level=excluded.trust_level,
                    capabilities=excluded.capabilities,
                    online=excluded.online,
                    metadata=excluded.metadata,
                    fingerprint=excluded.fingerprint,
                    auth_state=excluded.auth_state,
                    paired_at=excluded.paired_at,
                    last_seen=excluded.last_seen,
                    key_ref=excluded.key_ref,
                    updated_at=datetime('now')
                """,
                (
                    device.device_id,
                    device.type,
                    device.owner,
                    device.platform,
                    device.trust_level,
                    json.dumps(device.capabilities),
                    1 if device.online else 0,
                    json.dumps(device.metadata),
                    device.fingerprint,
                    device.auth_state,
                    device.paired_at.isoformat() if device.paired_at else None,
                    device.last_seen.isoformat() if device.last_seen else None,
                    device.key_ref,
                ),
            )

    def get(self, device_id: str) -> Optional[DeviceSpec]:
        return self._cache.get(device_id)

    def all(self) -> List[DeviceSpec]:
        return list(self._cache.values())

    def paired(self) -> List[DeviceSpec]:
        """Devices that have completed pairing and are not revoked."""
        return [d for d in self._cache.values() if d.is_paired]

    def set_auth_state(
        self,
        device_id: str,
        state: str,
        *,
        fingerprint: str = "",
        key_ref: str = "",
    ) -> Optional[DeviceSpec]:
        """Move a device between unpaired / pending / paired / revoked."""
        device = self.get(device_id)
        if device is None:
            return None
        device.auth_state = state
        if fingerprint:
            device.fingerprint = fingerprint
        if key_ref:
            device.key_ref = key_ref
        if state == "paired":
            device.paired_at = utcnow()
        self.register(device)
        log.info("device auth state changed", extra_fields={"device": device_id, "state": state})
        return device

    def mark_seen(self, device_id: str) -> None:
        device = self.get(device_id)
        if device is None:
            return
        device.last_seen = utcnow()
        self.register(device)

    def supports(self, device_id: str, capability: str) -> bool:
        device = self.get(device_id)
        return bool(device and capability in device.capabilities)

    def set_online(self, device_id: str, online: bool) -> None:
        device = self.get(device_id)
        if device is None:
            return
        device.online = online
        self.register(device)
        log.info("device status changed", extra_fields={"device": device_id, "online": online})

    @staticmethod
    def _row_to_spec(row: Any) -> DeviceSpec:
        def _ts(value: Any):
            if not value:
                return None
            try:
                return datetime.fromisoformat(str(value))
            except ValueError:
                return None

        def _column(name: str, default: Any = "") -> Any:
            """Tolerate rows read before the v0.6 migration has run."""
            try:
                return row[name]
            except (IndexError, KeyError):
                return default

        return DeviceSpec(
            device_id=row["device_id"],
            type=row["type"],
            owner=row["owner"] or "user",
            platform=row["platform"] or "unknown",
            trust_level=row["trust_level"] or "medium",
            capabilities=json.loads(row["capabilities"] or "[]"),
            online=bool(row["online"]),
            metadata=json.loads(row["metadata"] or "{}"),
            fingerprint=_column("fingerprint") or "",
            auth_state=_column("auth_state", "unpaired") or "unpaired",
            paired_at=_ts(_column("paired_at", None)),
            last_seen=_ts(_column("last_seen", None)),
            key_ref=_column("key_ref") or "",
        )


# ---------------------------------------------------------------------------
# Shared instance
# ---------------------------------------------------------------------------
_registry: Optional[DeviceRegistry] = None


def get_device_registry() -> DeviceRegistry:
    """The process-wide device registry.

    Everything that reads or writes device state must go through this, so a
    device paired at runtime is immediately visible everywhere.
    """
    global _registry
    if _registry is None:
        _registry = DeviceRegistry()
    return _registry
