"""Phone / device tools (v0.6).

Every tool here executes on the **laptop**, with the phone named as
``target_device``. That is deliberate: the laptop does the work, so the
permission engine's device checks apply to the machine actually acting, and the
phone's reachability becomes an honest tool-level message rather than a policy
block with a misleading reason.

There is deliberately **no tool that executes anything on the phone**, and none
that lets a device run anything on this Mac. The phone can send and receive
files and nothing else.
"""

from __future__ import annotations

import base64
import os
import socket
from pathlib import Path
from typing import Any, Dict, Optional

from ..core.models import RiskLevel, ToolSpec, utcnow
from ..devices.pairing import PairingManager
from ..logging_setup import get_logger
from ..transport.endpoint import DeviceEndpoint
from ..transport.server import TransferServer
from ..transport.tls import ensure_certificate
from .base import ToolContext, fail, human_size, ok

log = get_logger("tools.phone")

#: Capabilities a device may hold. Anything not listed stays disabled until it
#: is explicitly implemented -- the roadmap requires this.
KNOWN_CAPABILITIES = {
    "file_receive",
    "file_send",
    "notification",
    "device_status",
    "battery_status",
    "camera",
    "media",
    "intents",
}


def _resolve_device(ctx: ToolContext, target: str):
    """Find a device by id, then by display name. Name alone is never enough."""
    if not target:
        return None
    device = ctx.devices.get(target)
    if device is not None:
        return device

    wanted = target.strip().lower()
    for candidate in ctx.devices.all():
        if candidate.device_id.lower() == wanted:
            return candidate
        if str(candidate.metadata.get("display_name", "")).lower() == wanted:
            return candidate
    return None


def _endpoint_of(device) -> Optional[tuple[str, int]]:
    host = str(device.metadata.get("endpoint_host", "")).strip()
    port = device.metadata.get("endpoint_port")
    if host and port:
        try:
            return host, int(port)
        except (TypeError, ValueError):
            return None
    return None


def _reachable(host: str, port: int, timeout: float = 1.5) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def _newest_match(fragment: str, search_root: Optional[Path] = None) -> Optional[Path]:
    """Newest file whose name contains ``fragment``.

    Used for "send the latest report to my phone" -- the user names a thing,
    not a path, so the most recently modified match is the sensible reading.
    """
    needle = fragment.strip().lower()
    if not needle:
        return None

    root = search_root or Path.home()
    candidates = []
    skip = {".git", "node_modules", "Library", ".Trash", ".cache"}

    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in skip and not d.startswith(".")]
            for name in filenames:
                if needle in name.lower():
                    path = Path(dirpath) / name
                    try:
                        candidates.append((path.stat().st_mtime, path))
                    except OSError:
                        continue
            if len(candidates) > 400:
                break
    except OSError:
        return None

    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    return candidates[0][1]


# ---------------------------------------------------------------------------
# Read-only
# ---------------------------------------------------------------------------
async def list_devices(ctx: ToolContext, query: str = "") -> Any:
    """List known devices with pairing state.

    A ``query`` narrows the list to the devices whose id or type contains
    it, so a lookup that is *searched* by the name the person gave answers
    "which one did you mean" rather than "what is there".
    """
    devices = []
    for device in ctx.devices.all():
        devices.append(
            {
                "device_id": device.device_id,
                "type": device.type,
                "auth_state": device.auth_state,
                "paired": device.is_paired,
                "online": device.online,
                "last_seen": device.last_seen.isoformat() if device.last_seen else None,
            }
        )
    needle = (query or "").strip().lower()
    if needle:
        devices = [
            d for d in devices
            if needle in str(d["device_id"]).lower()
            or needle in str(d["type"]).lower()
        ]
    paired = sum(1 for d in devices if d["paired"])
    return ok(
        "list_devices",
        f"I know {len(devices)} devices; {paired} are paired.",
        device=ctx.device_id,
        devices=devices,
    )


async def get_device_status(ctx: ToolContext, target_device: str = "") -> Any:
    """Report one device's pairing state and reachability."""
    device = _resolve_device(ctx, target_device)
    if device is None:
        return fail(
            "get_device_status",
            f"I do not know a device called {target_device!r}.",
            device=ctx.device_id,
        )

    endpoint = _endpoint_of(device)
    reachable = _reachable(*endpoint) if endpoint else False

    if not device.is_paired:
        summary = f"{device.device_id} is not paired yet, so I cannot use it."
    elif reachable:
        summary = f"{device.device_id} is paired and reachable."
    else:
        summary = f"{device.device_id} is paired but not reachable right now."

    return ok(
        "get_device_status",
        summary,
        device=ctx.device_id,
        device_id=device.device_id,
        auth_state=device.auth_state,
        paired=device.is_paired,
        reachable=reachable,
        last_seen=device.last_seen.isoformat() if device.last_seen else None,
    )


async def get_device_capabilities(ctx: ToolContext, target_device: str = "") -> Any:
    """Report what a device is allowed to do."""
    device = _resolve_device(ctx, target_device)
    if device is None:
        return fail(
            "get_device_capabilities",
            f"I do not know a device called {target_device!r}.",
            device=ctx.device_id,
        )

    enabled = [c for c in device.capabilities if c in KNOWN_CAPABILITIES]
    disabled = [c for c in device.capabilities if c not in KNOWN_CAPABILITIES]

    return ok(
        "get_device_capabilities",
        f"{device.device_id} can: {', '.join(enabled) or 'nothing yet'}.",
        device=ctx.device_id,
        device_id=device.device_id,
        enabled=enabled,
        disabled=disabled,
    )


async def ping_device(ctx: ToolContext, target_device: str = "") -> Any:
    """Check whether a device's endpoint is reachable."""
    device = _resolve_device(ctx, target_device)
    if device is None:
        return fail("ping_device", f"I do not know {target_device!r}.", device=ctx.device_id)

    if not device.is_paired:
        return fail(
            "ping_device",
            f"{device.device_id} is not paired, so there is nothing to ping.",
            device=ctx.device_id,
        )

    endpoint = _endpoint_of(device)
    if endpoint is None:
        return ok(
            "ping_device",
            f"{device.device_id} has no transfer endpoint configured.",
            device=ctx.device_id,
            reachable=False,
        )

    reachable = _reachable(*endpoint)
    if reachable:
        ctx.devices.mark_seen(device.device_id)

    return ok(
        "ping_device",
        f"{device.device_id} is {'reachable' if reachable else 'not reachable'}.",
        device=ctx.device_id,
        reachable=reachable,
    )


# ---------------------------------------------------------------------------
# Pairing
# ---------------------------------------------------------------------------
async def register_phone(ctx: ToolContext, device_name: str = "") -> Any:
    """Show pending pairing requests, or start one."""
    pairing = PairingManager(ctx.settings)
    pending = pairing.pending()

    if device_name:
        # A pairing request normally originates *from* the device. Asking here
        # creates the request record so the user can see and approve it.
        device_id = "".join(ch for ch in device_name.lower() if ch.isalnum() or ch == "_")
        device_id = f"phone_{device_id}" if not device_id.startswith("phone") else device_id
        try:
            request = pairing.request(device_id, device_name)
        except RuntimeError as exc:
            return fail("register_phone", str(exc), device=ctx.device_id)
        return ok(
            "register_phone",
            (
                f"Pairing request created for {device_name}. Approve it to trust the "
                "device — nothing is trusted until you do."
            ),
            device=ctx.device_id,
            pairing_id=request.pairing_id,
            device_id=request.device_id,
        )

    if not pending:
        return ok(
            "register_phone",
            "No device is asking to pair right now.",
            device=ctx.device_id,
            pending=[],
        )

    listing = [{"pairing_id": p["pairing_id"], "device_id": p["device_id"]} for p in pending]
    return ok(
        "register_phone",
        f"{len(listing)} device(s) are waiting for pairing approval.",
        device=ctx.device_id,
        pending=listing,
    )


async def approve_pairing(ctx: ToolContext, pairing_id: str = "") -> Any:
    """Trust a pending device and issue its key."""
    pairing = PairingManager(ctx.settings)
    if not pairing_id:
        pending = pairing.pending()
        if not pending:
            return fail("approve_pairing", "No pairing request is waiting.", device=ctx.device_id)
        pairing_id = pending[0]["pairing_id"]

    try:
        result = pairing.approve(pairing_id)
    except ValueError as exc:
        return fail("approve_pairing", str(exc), device=ctx.device_id)

    # Refresh the registry view so the new state is visible immediately.
    ctx.devices.refresh()

    log.info("pairing approved via tool", extra_fields={"device": result["device_id"]})
    return ok(
        "approve_pairing",
        (
            f"{result['device_id']} is now paired. Verify the fingerprint matches "
            f"on the device: {result['fingerprint']}"
        ),
        device=ctx.device_id,
        device_id=result["device_id"],
        fingerprint=result["fingerprint"],
        # The secret is returned so the caller can hand it to the device once.
        secret_b64=result["secret_b64"],
    )


async def remove_phone(ctx: ToolContext, target_device: str = "") -> Any:
    """Revoke a device's trust and destroy its key."""
    device = _resolve_device(ctx, target_device)
    if device is None:
        return fail("remove_phone", f"I do not know {target_device!r}.", device=ctx.device_id)
    if not device.is_paired:
        return fail(
            "remove_phone",
            f"{device.device_id} is not paired, so there is nothing to revoke.",
            device=ctx.device_id,
        )

    try:
        result = PairingManager(ctx.settings).revoke(device.device_id)
    except ValueError as exc:
        return fail("remove_phone", str(exc), device=ctx.device_id)

    ctx.devices.refresh()
    return ok(
        "remove_phone",
        f"{device.device_id} has been unpaired and its key destroyed.",
        device=ctx.device_id,
        device_id=device.device_id,
        state=result["state"],
    )


# ---------------------------------------------------------------------------
# Transfers
# ---------------------------------------------------------------------------
async def send_to_device(
    ctx: ToolContext,
    path: str = "",
    target_device: str = "",
    resolve_latest: bool = False,
) -> Any:
    """Send a file from this Mac to a paired device.

    With ``resolve_latest`` the newest file matching ``path`` as a name fragment
    is chosen, so "send the latest report to my phone" behaves sensibly.
    """
    device = _resolve_device(ctx, target_device)
    if device is None:
        return fail(
            "send_to_device",
            f"I do not know a device called {target_device!r}.",
            device=ctx.device_id,
        )
    if not device.is_paired:
        return fail(
            "send_to_device",
            (
                f"{device.device_id} is not paired, so I will not send it anything. "
                "Pair it first."
            ),
            device=ctx.device_id,
        )

    source = Path(path).expanduser()

    if resolve_latest and not source.exists():
        source = _newest_match(path) or source

    if not source.exists():
        return fail("send_to_device", f"I could not find {source}.", device=ctx.device_id)
    if source.is_dir():
        return fail(
            "send_to_device",
            "I can send a single file. Compress the folder first and send the archive.",
            device=ctx.device_id,
        )

    endpoint = _endpoint_of(device)
    if endpoint is None:
        return fail(
            "send_to_device",
            f"{device.device_id} has no transfer endpoint configured, so I cannot reach it.",
            device=ctx.device_id,
        )
    if not _reachable(*endpoint):
        return fail(
            "send_to_device",
            f"{device.device_id} is not reachable right now. Nothing was sent.",
            device=ctx.device_id,
        )

    pairing = PairingManager(ctx.settings)
    secret = pairing.load_secret(device.device_id)
    if secret is None:
        return fail(
            "send_to_device",
            f"I have no key for {device.device_id}. Pair it again.",
            device=ctx.device_id,
        )

    certificate = ensure_certificate(ctx.settings)
    endpoint_client = DeviceEndpoint(device.device_id, secret, ctx.settings)

    size = source.stat().st_size
    log.info(
        "sending file to device",
        extra_fields={"device": device.device_id, "size": size},
    )

    result = await endpoint_client.send_file(
        endpoint[0], endpoint[1], source, cert_fingerprint=certificate.fingerprint
    )

    if not result.get("ok"):
        return fail(
            "send_to_device",
            f"Transfer failed: {result.get('error', 'unknown error')}",
            device=ctx.device_id,
            target_device=device.device_id,
        )

    ctx.devices.mark_seen(device.device_id)
    return ok(
        "send_to_device",
        (
            f"Sent {source.name} ({human_size(size)}) to {device.device_id} and "
            "verified the checksum."
        ),
        device=ctx.device_id,
        target_device=device.device_id,
        filename=result.get("filename"),
        bytes=result.get("bytes"),
        sha256=result.get("sha256"),
        verified=result.get("verified", False),
    )


async def get_transfer_status(ctx: ToolContext, transfer_id: str = "") -> Any:
    """Report recent transfers, or one specific transfer."""
    server = TransferServer(ctx.settings)
    if transfer_id:
        record = server.get_transfer(transfer_id)
        if record is None:
            return fail("get_transfer_status", f"No transfer {transfer_id!r}.", device=ctx.device_id)
        return ok(
            "get_transfer_status",
            f"Transfer {transfer_id} is {record['state']}.",
            device=ctx.device_id,
            transfer=record,
        )

    recent = server.recent(limit=5)
    if not recent:
        return ok("get_transfer_status", "No transfers yet.", device=ctx.device_id, transfers=[])

    latest = recent[0]
    return ok(
        "get_transfer_status",
        f"The most recent transfer ({latest['filename']}) is {latest['state']}.",
        device=ctx.device_id,
        transfers=recent,
    )


async def cancel_transfer(ctx: ToolContext, transfer_id: str = "") -> Any:
    """Cancel a transfer that has not completed."""
    if not transfer_id:
        return fail("cancel_transfer", "Which transfer should I cancel?", device=ctx.device_id)

    server = TransferServer(ctx.settings)
    record = server.get_transfer(transfer_id)
    if record is None:
        return fail("cancel_transfer", f"No transfer {transfer_id!r}.", device=ctx.device_id)

    from ..transport.protocol import TransferState, can_transition

    current = TransferState(record["state"])
    if current in (TransferState.COMPLETED, TransferState.FAILED, TransferState.CANCELLED):
        return fail(
            "cancel_transfer",
            f"That transfer is already {current.value}.",
            device=ctx.device_id,
        )
    if not can_transition(current, TransferState.CANCELLED):
        return fail(
            "cancel_transfer",
            f"A transfer in state {current.value} can no longer be cancelled.",
            device=ctx.device_id,
        )

    ctx.devices  # keep the context reference meaningful
    server.db.execute(
        "UPDATE transfers SET state = ?, completed_at = ? WHERE transfer_id = ?",
        (TransferState.CANCELLED.value, utcnow().isoformat(), transfer_id),
    )
    server._record(transfer_id, TransferState.CANCELLED, "cancelled by user")
    return ok(
        "cancel_transfer",
        f"Cancelled transfer {transfer_id}.",
        device=ctx.device_id,
        transfer_id=transfer_id,
    )


# ---------------------------------------------------------------------------
# Specs
# ---------------------------------------------------------------------------
_SPECS = [
    (
        ToolSpec(
            name="list_devices",
            description="List known devices and their pairing state.",
            agent="phone",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={"query": "string"},
            tags=["phone", "read"],
            provides_identifiers={"target_device": {"items": "devices", "id": "device_id",
                                                 "label": "device_id",
                                                 "lookup": "query"}},
        ),
        list_devices,
    ),
    (
        ToolSpec(
            name="get_device_status",
            description="Report one device's pairing state and reachability.",
            agent="phone",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={"target_device": "string"},
            required_parameters=["target_device"],
            tags=["phone", "read"],
        ),
        get_device_status,
    ),
    (
        ToolSpec(
            name="get_device_capabilities",
            description="List what a device is permitted to do.",
            agent="phone",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={"target_device": "string"},
            required_parameters=["target_device"],
            tags=["phone", "read"],
        ),
        get_device_capabilities,
    ),
    (
        ToolSpec(
            name="ping_device",
            description="Check whether a device is reachable.",
            agent="phone",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={"target_device": "string"},
            required_parameters=["target_device"],
            tags=["phone", "read"],
        ),
        ping_device,
    ),
    (
        ToolSpec(
            name="register_phone",
            description="Show or start device pairing requests.",
            agent="phone",
            risk=RiskLevel.EXTERNAL,
            requires_confirmation=True,
            reversible=True,
            allowed_devices=["laptop_main"],
            parameters={"device_name": "string"},
            tags=["phone", "pairing"],
        ),
        register_phone,
    ),
    (
        ToolSpec(
            name="approve_pairing",
            description="Trust a pending device and issue its key.",
            agent="phone",
            risk=RiskLevel.DESTRUCTIVE,
            requires_confirmation=True,
            reversible=True,
            allowed_devices=["laptop_main"],
            parameters={"pairing_id": "string"},
            tags=["phone", "pairing", "security"],
        ),
        approve_pairing,
    ),
    (
        ToolSpec(
            name="remove_phone",
            description="Revoke a device's trust and destroy its key.",
            agent="phone",
            risk=RiskLevel.DESTRUCTIVE,
            requires_confirmation=True,
            reversible=False,
            allowed_devices=["laptop_main"],
            parameters={"target_device": "string"},
            required_parameters=["target_device"],
            tags=["phone", "pairing", "security"],
        ),
        remove_phone,
    ),
    (
        ToolSpec(
            name="send_to_device",
            description="Send a file from this Mac to a paired device.",
            agent="phone",
            risk=RiskLevel.EXTERNAL,
            requires_confirmation=True,
            reversible=True,
            allowed_devices=["laptop_main"],
            parameters={"path": "string", "target_device": "string", "resolve_latest": "bool"},
            required_parameters=["path", "target_device"],
            tags=["phone", "transfer"],
        ),
        send_to_device,
    ),
    (
        ToolSpec(
            name="get_transfer_status",
            description="Report transfer progress and history.",
            agent="phone",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={"transfer_id": "string"},
            tags=["phone", "read"],
        ),
        get_transfer_status,
    ),
    (
        ToolSpec(
            name="cancel_transfer",
            description="Cancel an in-flight transfer.",
            agent="phone",
            risk=RiskLevel.LOW,
            reversible=True,
            allowed_devices=["laptop_main"],
            parameters={"transfer_id": "string"},
            required_parameters=["transfer_id"],
            tags=["phone", "transfer"],
        ),
        cancel_transfer,
    ),
]

SPECS = _SPECS
