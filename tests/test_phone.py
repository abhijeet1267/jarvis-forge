"""Phone Agent tests (v0.6).

Covers the pairing lifecycle, the tool surface, and the policy guards that stop
a standing grant from silently approving a transfer to an unpaired device.
"""

from __future__ import annotations

import base64

import pytest

from jarvis.core.models import PlannedAction, PolicyDecision, RiskLevel
from jarvis.devices.pairing import PairingManager, fingerprint_of, generate_secret
from jarvis.registry.devices import DeviceRegistry
from jarvis.tools.base import ToolContext
from jarvis.tools.phone import (
    KNOWN_CAPABILITIES,
    approve_pairing,
    get_device_capabilities,
    get_device_status,
    list_devices,
    ping_device,
    register_phone,
    remove_phone,
    send_to_device,
)


@pytest.fixture()
def ctx() -> ToolContext:
    return ToolContext()


@pytest.fixture(autouse=True)
def _clean_pairing_state():
    """Keep pending pairings from leaking between tests.

    Pairing state is process-wide (the registry is a shared singleton), so
    without this a test that leaves requests pending would trip the flood cap
    for the next one.
    """
    manager = PairingManager()
    for pending in manager.pending():
        try:
            manager.deny(pending["pairing_id"])
        except ValueError:
            pass
    yield


# ---------------------------------------------------------------------------
# Pairing lifecycle
# ---------------------------------------------------------------------------
def test_pairing_request_grants_nothing() -> None:
    """A pending request must not make the device usable."""
    manager = PairingManager()
    registry = DeviceRegistry()

    registry.set_auth_state("watch_main", "unpaired")
    request = manager.request("watch_main", "Test Watch")

    device = registry.get("watch_main")
    assert device.auth_state == "unpaired"
    assert device.is_paired is False
    assert request.state == "pending"


def test_approval_grants_trust_and_issues_a_key() -> None:
    manager = PairingManager()
    request = manager.request("phone_pair_a", "Pair Phone A")
    result = manager.approve(request.pairing_id)

    assert result["state"] == "paired"
    assert len(result["secret_b64"]) > 20
    assert DeviceRegistry().get("phone_pair_a").is_paired is True

    secret = base64.b64decode(result["secret_b64"])
    assert manager.load_secret("phone_pair_a") == secret


def test_denial_leaves_the_device_untrusted() -> None:
    manager = PairingManager()
    request = manager.request("phone_denied", "Denied Phone")
    manager.deny(request.pairing_id)

    device = DeviceRegistry().get("phone_denied")
    # A denied device is never registered as trusted, and gets no key.
    assert device is None or device.is_paired is False
    assert manager.load_secret("phone_denied") is None
    assert manager.get(request.pairing_id)["state"] == "denied"


def test_cannot_approve_twice() -> None:
    manager = PairingManager()
    request = manager.request("phone_twice", "Twice Phone")
    manager.approve(request.pairing_id)

    with pytest.raises(ValueError, match="already"):
        manager.approve(request.pairing_id)


def test_pending_requests_are_capped() -> None:
    """The unauthenticated pairing path must not be usable to flood."""
    manager = PairingManager()
    for existing in manager.pending():
        manager.deny(existing["pairing_id"])

    accepted = 0
    with pytest.raises(RuntimeError, match="Too many pending"):
        for index in range(20):
            manager.request(f"flood_{index}", f"Flood {index}")
            accepted += 1

    assert accepted <= 5


def test_fingerprint_is_stable_and_short() -> None:
    secret = generate_secret()
    first = fingerprint_of(secret)
    assert first == fingerprint_of(secret)
    assert first != fingerprint_of(generate_secret())
    assert len(first.replace(" ", "")) == 16


def test_key_file_is_owner_only(tmp_area) -> None:
    from jarvis.config import Settings

    settings = Settings(home=tmp_area, api_token="")
    manager = PairingManager(settings)
    request = manager.request("phone_perm", "Perm Phone")
    manager.approve(request.pairing_id)

    path = manager.key_path("phone_perm")
    assert path.exists()
    assert (path.stat().st_mode & 0o777) == 0o600


# ---------------------------------------------------------------------------
# Tool surface
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_list_devices(ctx: ToolContext) -> None:
    result = await list_devices(ctx)
    assert result.ok
    ids = {d["device_id"] for d in result.output["devices"]}
    assert {"laptop_main", "phone_main", "watch_main"} <= ids


@pytest.mark.asyncio
async def test_device_status_reports_pairing(ctx: ToolContext) -> None:
    result = await get_device_status(ctx, target_device="phone_main")
    assert result.ok
    assert "auth_state" in result.output


@pytest.mark.asyncio
async def test_unknown_device_is_refused(ctx: ToolContext) -> None:
    result = await get_device_status(ctx, target_device="not_a_device")
    assert not result.ok
    assert "do not know" in result.summary


@pytest.mark.asyncio
async def test_capabilities_report_only_known_ones(ctx: ToolContext) -> None:
    result = await get_device_capabilities(ctx, target_device="phone_main")
    assert result.ok
    assert set(result.output["enabled"]) <= KNOWN_CAPABILITIES


@pytest.mark.asyncio
async def test_ping_unpaired_device_is_refused(ctx: ToolContext) -> None:
    DeviceRegistry().set_auth_state("watch_main", "unpaired")
    result = await ping_device(ctx, target_device="watch_main")
    assert not result.ok
    assert "not paired" in result.summary


@pytest.mark.asyncio
async def test_send_refuses_an_unpaired_device(ctx: ToolContext, tmp_area) -> None:
    """The tool must refuse independently of the policy layer."""
    DeviceRegistry().set_auth_state("watch_main", "unpaired")
    payload = tmp_area / "x.txt"
    payload.write_text("data")

    result = await send_to_device(ctx, path=str(payload), target_device="watch_main")
    assert not result.ok
    assert "not paired" in result.summary


@pytest.mark.asyncio
async def test_send_refuses_a_missing_file(ctx: ToolContext) -> None:
    manager = PairingManager()
    if not DeviceRegistry().get("phone_main").is_paired:
        manager.approve(manager.request("phone_main", "Phone").pairing_id)

    result = await send_to_device(
        ctx, path="/tmp/definitely-not-here-9f3a.txt", target_device="phone_main"
    )
    assert not result.ok


@pytest.mark.asyncio
async def test_send_refuses_a_directory(ctx: ToolContext, tmp_area) -> None:
    manager = PairingManager()
    if not DeviceRegistry().get("phone_main").is_paired:
        manager.approve(manager.request("phone_main", "Phone").pairing_id)

    folder = tmp_area / "folder"
    folder.mkdir()
    result = await send_to_device(ctx, path=str(folder), target_device="phone_main")
    assert not result.ok
    assert "single file" in result.summary


@pytest.mark.asyncio
async def test_register_phone_lists_pending(ctx: ToolContext) -> None:
    manager = PairingManager()
    for existing in manager.pending():
        manager.deny(existing["pairing_id"])
    manager.request("phone_show", "Show Phone")

    result = await register_phone(ctx)
    assert result.ok
    assert any(p["device_id"] == "phone_show" for p in result.output["pending"])


@pytest.mark.asyncio
async def test_approve_pairing_tool(ctx: ToolContext) -> None:
    manager = PairingManager()
    request = manager.request("phone_tool", "Tool Phone")

    result = await approve_pairing(ctx, pairing_id=request.pairing_id)
    assert result.ok
    assert result.output["fingerprint"]
    assert DeviceRegistry().get("phone_tool").is_paired is True


@pytest.mark.asyncio
async def test_remove_phone_tool_revokes(ctx: ToolContext) -> None:
    manager = PairingManager()
    manager.approve(manager.request("phone_remove", "Remove Phone").pairing_id)

    result = await remove_phone(ctx, target_device="phone_remove")
    assert result.ok
    assert DeviceRegistry().get("phone_remove").auth_state == "revoked"
    assert manager.load_secret("phone_remove") is None


# ---------------------------------------------------------------------------
# Tool registration and risk
# ---------------------------------------------------------------------------
def test_tools_are_registered_under_the_phone_agent(core) -> None:
    phone_tools = [s for s in core.tools.all() if s.agent == "phone"]
    assert len(phone_tools) == 10
    assert core.agents["phone"].owns("send_to_device")


@pytest.mark.parametrize(
    "tool,expected",
    [
        ("list_devices", RiskLevel.READ),
        ("send_to_device", RiskLevel.EXTERNAL),
        ("approve_pairing", RiskLevel.DESTRUCTIVE),
        ("remove_phone", RiskLevel.DESTRUCTIVE),
    ],
)
def test_risk_tiers(core, tool: str, expected: RiskLevel) -> None:
    assert core.tools.get(tool).risk == expected


@pytest.mark.parametrize("tool", ["send_to_device", "approve_pairing", "remove_phone"])
def test_sensitive_tools_require_confirmation(core, tool: str) -> None:
    assert core.tools.get(tool).requires_confirmation is True


def test_no_tool_can_execute_commands_on_a_device(core) -> None:
    """Non-negotiable rule 9: a remote device must never run commands here.

    And symmetrically, no tool should be able to run commands *on* the phone.
    """
    forbidden = ("exec", "shell", "command", "run_remote", "remote_exec")
    for spec in core.tools.all():
        if spec.agent != "phone":
            continue
        lowered = spec.name.lower()
        assert not any(word in lowered for word in forbidden), spec.name


# ---------------------------------------------------------------------------
# Policy guards (gap G7)
# ---------------------------------------------------------------------------
def test_transfer_requires_confirmation(core) -> None:
    action = PlannedAction(
        tool="send_to_device",
        params={"path": "/tmp/a.txt", "target_device": "phone_main"},
        device="laptop_main",
    )
    assert core.policy.evaluate(action).decision is PolicyDecision.CONFIRM


def test_standing_grant_cannot_auto_approve_a_transfer_to_an_unpaired_device(core) -> None:
    """Consent to repeat an action is not consent to trust a new device."""
    DeviceRegistry().set_auth_state("watch_main", "unpaired")
    core.policy.grant("send_to_device", device="laptop_main")

    action = PlannedAction(
        tool="send_to_device",
        params={"path": "/tmp/a.txt", "target_device": "watch_main"},
        device="laptop_main",
    )
    verdict = core.policy.evaluate(action)

    assert verdict.decision is PolicyDecision.CONFIRM, (
        "an unpaired target must always be confirmed, even with a standing grant"
    )


def test_standing_grant_cannot_auto_approve_trust_changes(core) -> None:
    """Approving or revoking trust must never be silently authorised."""
    core.policy.grant("approve_pairing", device="laptop_main")
    core.policy.grant("remove_phone", device="laptop_main")

    for tool, params in (
        ("approve_pairing", {"pairing_id": "PAIR-whatever"}),
        ("remove_phone", {"target_device": "phone_main"}),
    ):
        action = PlannedAction(tool=tool, params=params, device="laptop_main")
        assert core.policy.evaluate(action).decision is PolicyDecision.CONFIRM, tool


def test_policy_does_not_block_transfers_on_the_offline_phone(core) -> None:
    """The executor is the laptop, so the phone being offline is not a block.

    This is the design decision from the v0.6 plan: routing the action to the
    phone would make the permission engine reject it for the wrong reason.
    """
    DeviceRegistry().set_online("phone_main", False)

    action = PlannedAction(
        tool="send_to_device",
        params={"path": "/tmp/a.txt", "target_device": "phone_main"},
        device="laptop_main",
    )
    verdict = core.policy.evaluate(action)
    assert verdict.decision is not PolicyDecision.BLOCK


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text,expected_tool",
    [
        ("send /tmp/report.pdf to my phone", "send_to_device"),
        ("pair my phone", "register_phone"),
        ("list my devices", "list_devices"),
        ("what is my phone status", "get_device_status"),
        ("unpair my phone", "remove_phone"),
        ("how is the transfer going", "get_transfer_status"),
    ],
)
def test_planner_routes_device_intents(core, text: str, expected_tool: str) -> None:
    plan = core.planner.parse(text)
    assert plan.actions and plan.actions[0].tool == expected_tool


def test_planner_refuses_vague_transfer_targets(core) -> None:
    """'send this file' must ask, not guess which file leaves the machine."""
    plan = core.planner.parse("send this file to my phone")
    assert plan.actions == []
    assert plan.direct_reply
