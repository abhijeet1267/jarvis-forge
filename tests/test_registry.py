"""Tests for the tool, application and device registries."""

from __future__ import annotations

from jarvis.core.models import RiskLevel
from jarvis.registry.apps import AppRegistry
from jarvis.registry.devices import DeviceRegistry
from jarvis.registry.tools import ToolRegistry, spec


def test_tool_registry_registers_and_lists() -> None:
    registry = ToolRegistry()
    registry.register(spec("alpha", "first"), lambda ctx: None)
    registry.register(spec("beta", "second", risk=RiskLevel.DESTRUCTIVE), lambda ctx: None)

    assert registry.has("alpha")
    assert set(registry.names()) == {"alpha", "beta"}
    # Sorted by risk ascending.
    assert registry.all()[0].name == "alpha"


def test_validate_params_detects_missing_and_extra() -> None:
    registry = ToolRegistry()
    registry.register(
        spec("needs_arg", "d", parameters={"path": "string"}, required=["path"]),
        lambda ctx: None,
    )

    assert registry.validate_params("needs_arg", {"path": "/tmp/x"}) == []
    assert any("missing" in p for p in registry.validate_params("needs_arg", {}))
    assert any("unexpected" in p for p in registry.validate_params("needs_arg", {"nope": 1}))
    assert registry.validate_params("ghost", {}) == ["unknown tool 'ghost'"]


def test_default_registry_contains_core_tools() -> None:
    from jarvis.tools import build_default_registry

    registry = build_default_registry()
    for name in ("open_application", "search_files", "delete_file", "get_battery_status", "emergency_stop"):
        assert registry.has(name), f"{name} should be registered"


def test_app_registry_resolves_aliases_and_phrases() -> None:
    apps = AppRegistry()

    assert apps.resolve("vs code").key == "vscode"
    assert apps.resolve("my coding environment").key == "vscode"
    assert apps.resolve("chrome").key == "chrome"
    assert apps.resolve("open the browser").key in {"chrome", "safari"}
    assert apps.resolve("nonexistent-app-xyz") is None


def test_app_registry_respects_platform() -> None:
    apps = AppRegistry()
    assert apps.resolve("safari", platform="windows") is None
    assert apps.resolve("safari", platform="macos") is not None


def test_device_registry_capabilities() -> None:
    devices = DeviceRegistry()
    laptop = devices.get("laptop_main")

    assert laptop is not None
    assert devices.supports("laptop_main", "app_launch")
    assert not devices.supports("laptop_main", "robot_motion")
    assert devices.get("car_main").type == "vehicle"
