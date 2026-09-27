"""Unit tests for the risk classifier and the raw-command safety guard."""

from __future__ import annotations

import pytest

from jarvis.core.models import PolicyDecision, RiskLevel
from jarvis.registry.tools import spec
from jarvis.security.risk import RiskClassifier


@pytest.mark.parametrize(
    "text",
    [
        "delete everything",
        "wipe the disk",
        "format the drive",
        "run this command as root",
        "sudo rm -rf /",
        "unlock the door",
        "disable the firewall",
        "install unknown.exe",
        "pay 500 rupees",
        "what is my credit card number",
    ],
)
def test_dangerous_phrasing_is_blocked(text: str) -> None:
    hit = RiskClassifier().classify_command(text)
    assert hit is not None, f"expected {text!r} to be caught"
    assert hit.decision is PolicyDecision.BLOCK


@pytest.mark.parametrize(
    "text",
    ["what is my battery", "open VS Code", "find my report", "what time is it"],
)
def test_benign_phrasing_passes_the_guard(text: str) -> None:
    assert RiskClassifier().classify_command(text) is None


def test_level_0_and_1_execute_without_confirmation() -> None:
    classifier = RiskClassifier()
    read_tool = spec("read_thing", "read", risk=RiskLevel.READ)
    low_tool = spec("open_thing", "open", risk=RiskLevel.LOW)

    assert classifier.decide(read_tool, {})[0] is PolicyDecision.EXECUTE
    assert classifier.decide(low_tool, {})[0] is PolicyDecision.EXECUTE


def test_level_2_plus_confirms() -> None:
    classifier = RiskClassifier()
    for risk in (RiskLevel.EXTERNAL, RiskLevel.DESTRUCTIVE, RiskLevel.PHYSICAL):
        tool = spec("risky", "risky", risk=risk)
        decision, _reason = classifier.decide(tool, {})
        assert decision is PolicyDecision.CONFIRM, f"level {int(risk)} should confirm"


def test_safety_tagged_tools_always_execute() -> None:
    classifier = RiskClassifier()
    stop = spec("emergency_stop", "stop", risk=RiskLevel.PHYSICAL, tags=["safety"])
    assert classifier.decide(stop, {})[0] is PolicyDecision.EXECUTE


def test_paranoid_mode_promotes_low_risk_to_confirm() -> None:
    classifier = RiskClassifier(paranoid=True)
    low_tool = spec("open_thing", "open", risk=RiskLevel.LOW)
    assert classifier.decide(low_tool, {})[0] is PolicyDecision.CONFIRM
