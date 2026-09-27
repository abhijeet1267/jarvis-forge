"""Tests for the permission engine.

These use a private tool registry and test-only tool names so the shared
database (which persists grants and revocations) cannot leak state into other
test modules.
"""

from __future__ import annotations

import pytest
from pathlib import Path

from jarvis.core.models import PlannedAction, PolicyDecision, RiskLevel
from jarvis.registry.devices import DeviceRegistry
from jarvis.registry.tools import ToolRegistry, spec
from jarvis.security.policy import PermissionEngine


async def _noop(ctx, **kwargs):  # pragma: no cover - never executed here
    raise AssertionError("handler should not run in policy tests")


@pytest.fixture()
def engine() -> PermissionEngine:
    registry = ToolRegistry()
    registry.register(spec("t_read", "read", risk=RiskLevel.READ), _noop)
    registry.register(spec("t_open", "open", risk=RiskLevel.LOW, parameters={"app": "string"}, required=["app"]), _noop)
    registry.register(
        spec(
            "t_copy",
            "copy",
            risk=RiskLevel.EXTERNAL,
            requires_confirmation=True,
            parameters={"source": "string", "destination": "string"},
            required=["source", "destination"],
        ),
        _noop,
    )
    registry.register(
        spec(
            "t_delete",
            "delete",
            risk=RiskLevel.DESTRUCTIVE,
            requires_confirmation=True,
            parameters={"path": "string"},
            required=["path"],
        ),
        _noop,
    )
    registry.register(
        spec("t_stop", "stop", risk=RiskLevel.PHYSICAL, tags=["safety"]),
        _noop,
    )
    # Argument shapes the name-based guard never inspected: a search root, a
    # command with list arguments, and an arbitrarily nested payload.
    registry.register(
        spec(
            "t_search",
            "search",
            risk=RiskLevel.READ,
            parameters={"query": "string", "directory": "string"},
            required=["query"],
        ),
        _noop,
    )
    registry.register(
        spec(
            "t_run",
            "run",
            risk=RiskLevel.EXTERNAL,
            requires_confirmation=True,
            parameters={"command": "string", "args": "list"},
            required=["command"],
        ),
        _noop,
    )
    registry.register(
        spec(
            "t_nested",
            "nested",
            risk=RiskLevel.READ,
            parameters={"payload": "dict"},
            required=["payload"],
        ),
        _noop,
    )
    return PermissionEngine(registry, DeviceRegistry())


def test_unknown_tool_is_blocked(engine: PermissionEngine) -> None:
    assert engine.evaluate(PlannedAction(tool="does_not_exist")).decision is PolicyDecision.BLOCK


def test_missing_required_parameter_is_blocked(engine: PermissionEngine) -> None:
    result = engine.evaluate(PlannedAction(tool="t_open", params={}))
    assert result.decision is PolicyDecision.BLOCK
    assert "app" in result.reason


def test_unexpected_parameter_is_blocked(engine: PermissionEngine) -> None:
    result = engine.evaluate(PlannedAction(tool="t_open", params={"app": "chrome", "sudo": "yes"}))
    assert result.decision is PolicyDecision.BLOCK


def test_unknown_device_is_blocked(engine: PermissionEngine) -> None:
    assert engine.evaluate(PlannedAction(tool="t_read", device="ghost")).decision is PolicyDecision.BLOCK


def test_offline_device_is_blocked(engine: PermissionEngine) -> None:
    action = PlannedAction(tool="t_read", params={}, device="car_main")
    assert engine.evaluate(action).decision is PolicyDecision.BLOCK


def test_read_and_low_risk_execute(engine: PermissionEngine) -> None:
    assert engine.evaluate(PlannedAction(tool="t_read")).decision is PolicyDecision.EXECUTE
    assert engine.evaluate(PlannedAction(tool="t_open", params={"app": "chrome"})).decision is PolicyDecision.EXECUTE


def test_destructive_requires_confirmation(engine: PermissionEngine) -> None:
    result = engine.evaluate(PlannedAction(tool="t_delete", params={"path": "/tmp/x.txt"}))
    assert result.decision is PolicyDecision.CONFIRM
    assert result.confirmation_id


def test_protected_path_blocked_not_confirmed(engine: PermissionEngine) -> None:
    result = engine.evaluate(PlannedAction(tool="t_delete", params={"path": "/etc/hosts"}))
    assert result.decision is PolicyDecision.BLOCK


def test_safety_tool_always_executes(engine: PermissionEngine) -> None:
    assert engine.evaluate(PlannedAction(tool="t_stop")).decision is PolicyDecision.EXECUTE


def test_confirmation_lifecycle(engine: PermissionEngine) -> None:
    result = engine.evaluate(PlannedAction(tool="t_delete", params={"path": "/tmp/lifecycle.txt"}))
    pending = {c["confirmation_id"] for c in engine.pending()}
    assert result.confirmation_id in pending

    engine.resolve(result.confirmation_id, approve=True)
    assert engine.get_confirmation(result.confirmation_id)["state"] == "approved"
    assert result.confirmation_id not in {c["confirmation_id"] for c in engine.pending()}


def test_denied_confirmation(engine: PermissionEngine) -> None:
    result = engine.evaluate(PlannedAction(tool="t_delete", params={"path": "/tmp/deny.txt"}))
    engine.resolve(result.confirmation_id, approve=False)
    assert engine.get_confirmation(result.confirmation_id)["state"] == "denied"


def test_standing_grant_downgrades_confirmation(engine: PermissionEngine) -> None:
    engine.grant("t_copy", device="laptop_main")
    action = PlannedAction(
        tool="t_copy", params={"source": "/tmp/a", "destination": "/tmp/b"}, device="laptop_main"
    )
    assert engine.evaluate(action).decision is PolicyDecision.EXECUTE


def test_revoked_permission_blocks(engine: PermissionEngine) -> None:
    engine.revoke("t_open", device="laptop_main")
    action = PlannedAction(tool="t_open", params={"app": "chrome"}, device="laptop_main")
    assert engine.evaluate(action).decision is PolicyDecision.BLOCK


PRIVATE_FILE = str(Path.home() / ".ssh" / "id_ed25519")
PRIVATE_DIR = str(Path.home() / "Library" / "LaunchAgents")


@pytest.mark.parametrize(
    "tool,params",
    [
        # The names the old guard did check.
        ("t_delete", {"path": PRIVATE_FILE}),
        ("t_copy", {"source": PRIVATE_FILE, "destination": "/tmp/out"}),
        ("t_copy", {"source": "/tmp/in", "destination": PRIVATE_FILE}),
        # The name it did not: a search root.
        ("t_search", {"query": "plist", "directory": PRIVATE_DIR}),
        # Paths hiding inside a command and inside its arguments.
        ("t_run", {"command": PRIVATE_DIR, "args": ["-maxdepth", "1"]}),
        ("t_run", {"command": "ls", "args": ["/tmp", PRIVATE_FILE]}),
        # Nesting at any depth.
        ("t_nested", {"payload": {"outer": [{"inner": PRIVATE_FILE}]}}),
        ("t_nested", {"payload": {"notes": "see", "targets": [PRIVATE_DIR]}}),
        # Two spellings of the same protected directory.
        ("t_search", {"query": "key", "directory": "~/.ssh"}),
        ("t_search", {"query": "key", "directory": str(Path.home() / ".ssh")}),
        # A safe value alongside a protected one is still refused.
        ("t_run", {"command": "ls", "args": ["~/Documents", PRIVATE_FILE]}),
    ],
)
def test_protected_paths_are_blocked_whatever_the_argument_name(
    engine: PermissionEngine, tool: str, params: dict
) -> None:
    """Refusal follows the value, not the parameter name it was filed under."""
    result = engine.evaluate(PlannedAction(tool=tool, params=params, device="laptop_main"))

    assert result.decision is PolicyDecision.BLOCK, f"{tool} {params}"
    assert "protected" in result.reason.lower()


@pytest.mark.parametrize(
    "tool,params",
    [
        # Look-alike names, and the everyday case.
        ("t_search", {"query": "report", "directory": "~/Documents"}),
        ("t_search", {"query": "LaunchAgents_backup.txt", "directory": "~/Documents"}),
        ("t_nested", {"payload": {"a": [1, 2, {"b": "nothing path-like"}]}}),
    ],
)
def test_safe_arguments_still_execute(engine: PermissionEngine, tool: str, params: dict) -> None:
    """A widened guard that refuses ordinary work is a guard that gets disabled."""
    result = engine.evaluate(PlannedAction(tool=tool, params=params, device="laptop_main"))

    assert result.decision is PolicyDecision.EXECUTE, f"{tool} {params}"


def test_safe_command_arguments_confirm_without_being_blocked(
    engine: PermissionEngine,
) -> None:
    """An external tool still confirms; the guard must not escalate it to BLOCK."""
    result = engine.evaluate(
        PlannedAction(
            tool="t_run", params={"command": "ls", "args": ["~/Documents", "/tmp"]},
            device="laptop_main",
        )
    )

    assert result.decision is PolicyDecision.CONFIRM
