"""Adaptive Autonomy Control: one dial, with a floor it cannot cross.

The property under test is not "the level is stored" -- it is that **the dial can
only make a decision stricter**. A control that a mis-set level could use to
auto-execute a destructive action would be worse than no control at all, so the
ceiling and the "never loosen" rule are what these tests pin.
"""

from __future__ import annotations

import pytest

from jarvis.core.models import PolicyDecision, RiskLevel, ToolSpec
from jarvis.security.autonomy import (
    CEILING_RISK,
    DEFAULT_LEVEL,
    LEVELS,
    AutonomyControl,
    AutonomyError,
    AutonomySetting,
)


def _spec(name="read_file", risk=RiskLevel.READ, tags=()):
    return ToolSpec(name=name, risk=risk, tags=tuple(tags))


@pytest.fixture()
def control(tmp_path):
    return AutonomyControl(path=tmp_path / "autonomy.json")


# -- the dial only ever tightens ---------------------------------------------

def test_a_low_dial_promotes_a_read_to_a_confirmation(control):
    decision, reason = control.apply(
        _spec(), {}, PolicyDecision.EXECUTE, "low-risk action")
    # Level 3 (default) leaves a read alone: a read needs no asking.
    assert decision is PolicyDecision.EXECUTE

    control.set(level=0)
    decision, reason = control.apply(
        _spec(), {}, PolicyDecision.EXECUTE, "low-risk action")
    assert decision is PolicyDecision.CONFIRM
    assert "ask before everything" in reason


def test_the_default_level_does_not_change_todays_behaviour(control):
    assert control.setting.level == DEFAULT_LEVEL
    for risk in RiskLevel:
        decision = control.apply(_spec(risk=risk), {},
                                 PolicyDecision.CONFIRM, "classified")
        assert decision[0] is PolicyDecision.CONFIRM


def test_the_dial_never_loosens_a_confirmation(control):
    """Level 4 must not turn a CONFIRM the classifier made into an EXECUTE."""
    control.set(level=4)
    for risk in RiskLevel:
        decision, _ = control.apply(_spec(risk=risk), {},
                                    PolicyDecision.CONFIRM, "classified")
        assert decision is PolicyDecision.CONFIRM, (
            f"level 4 auto-executed a {risk.name} action the classifier confirmed")


def test_the_dial_never_turns_a_block_into_an_execute(control):
    for level in LEVELS:
        control.set(level=level)
        decision, _ = control.apply(_spec(risk=RiskLevel.DESTRUCTIVE), {},
                                    PolicyDecision.BLOCK, "refused")
        assert decision is PolicyDecision.BLOCK


# -- the ceiling -------------------------------------------------------------

def test_a_destructive_action_is_never_auto_executed_at_any_level(control):
    """The whole point of a *ceiling*: no level, including 4, crosses it."""
    for level in LEVELS:
        control.set(level=level)
        decision, _ = control.apply(
            _spec(name="delete_file", risk=RiskLevel.DESTRUCTIVE), {},
            PolicyDecision.CONFIRM, "destructive")
        assert decision is PolicyDecision.CONFIRM, f"level {level} auto-ran a destructive action"


def test_the_ceiling_is_destructive_and_above(control):
    assert CEILING_RISK is RiskLevel.DESTRUCTIVE
    control.set(level=4)
    physical, _ = control.apply(_spec(risk=RiskLevel.PHYSICAL), {},
                                PolicyDecision.CONFIRM, "physical")
    assert physical is PolicyDecision.CONFIRM


# -- level 1 and 2 tiers -----------------------------------------------------

def test_level_one_stops_above_a_pure_read(control):
    control.set(level=1)
    read, _ = control.apply(_spec(risk=RiskLevel.READ), {}, PolicyDecision.EXECUTE, "read")
    low, _ = control.apply(_spec(risk=RiskLevel.LOW), {}, PolicyDecision.EXECUTE, "low")
    assert read is PolicyDecision.EXECUTE
    assert low is PolicyDecision.CONFIRM


def test_level_two_lets_a_low_risk_action_run(control):
    control.set(level=2)
    low, _ = control.apply(_spec(risk=RiskLevel.LOW), {}, PolicyDecision.EXECUTE, "low")
    external, _ = control.apply(
        _spec(risk=RiskLevel.EXTERNAL), {}, PolicyDecision.CONFIRM, "external")
    assert low is PolicyDecision.EXECUTE
    assert external is PolicyDecision.CONFIRM


# -- scope -------------------------------------------------------------------

def test_a_scoped_dial_leaves_everything_outside_it_alone(control):
    control.set(level=0, scope="/Users/me/Projects/lab")
    inside, _ = control.apply(_spec(), {"path": "/Users/me/Projects/lab/a.txt"},
                              PolicyDecision.EXECUTE, "low-risk action")
    outside, _ = control.apply(_spec(), {"path": "/Users/me/Projects/prod/a.txt"},
                               PolicyDecision.EXECUTE, "low-risk action")
    assert inside is PolicyDecision.CONFIRM
    assert outside is PolicyDecision.EXECUTE


def test_a_scoped_dial_does_not_cover_a_call_that_names_no_path(control):
    """A tool with no path-like parameter is not "under a directory scope"."""
    control.set(level=0, scope="/Users/me/lab")
    decision, _ = control.apply(_spec(), {}, PolicyDecision.EXECUTE, "low")
    assert decision is PolicyDecision.EXECUTE


def test_a_copy_that_writes_outside_the_scope_is_not_scoped(control):
    """Reads inside but writes outside is an escape, not a scoped action."""
    control.set(level=0, scope="/Users/me/lab")
    decision, _ = control.apply(
        _spec(risk=RiskLevel.LOW),
        {"source": "/Users/me/lab/in.txt", "destination": "/etc/out.txt"},
        PolicyDecision.EXECUTE, "low")
    assert decision is PolicyDecision.EXECUTE, "one parameter widened the dial"


# -- storage -----------------------------------------------------------------

def test_the_dial_survives_a_reload(tmp_path):
    path = tmp_path / "autonomy.json"
    first = AutonomyControl(path=path)
    first.set(level=1, scope="/Users/me/lab", note="focus on the lab project")

    second = AutonomyControl(path=path)
    assert second.setting.level == 1
    assert second.setting.scope == "/Users/me/lab"
    assert "lab project" in second.setting.note


def test_a_corrupt_dial_file_falls_back_to_the_safe_default(tmp_path):
    path = tmp_path / "autonomy.json"
    path.write_text("{not json")
    control = AutonomyControl(path=path)
    assert control.setting.level == DEFAULT_LEVEL


def test_an_undefined_level_is_refused(control):
    with pytest.raises(AutonomyError):
        control.set(level=99)


def test_the_describe_output_states_the_ceiling(control):
    described = control.describe()
    assert "never" in described["ceiling"]
    assert {row["level"] for row in described["levels"]} == set(LEVELS)


# -- through the permission engine -------------------------------------------

def test_the_permission_engine_applies_the_dial(monkeypatch):
    """The dial is wired into `PermissionEngine.evaluate`, so a low setting
    actually changes a real verdict rather than only the module's own answer."""
    from jarvis.core.models import PlannedAction

    monkeypatch.setenv("JARVIS_HOME", "/tmp/vantage_autonomy_test")
    from jarvis.security import autonomy as mod

    mod.reset_autonomy()
    mod.get_autonomy().set(level=0)
    try:
        _assert_dial_applied()
    finally:
        # Restore level 3 and drop the cached dial, so a later test in this
        # module (or another one) sees the default rather than this test's level.
        mod.get_autonomy().set(level=DEFAULT_LEVEL)
        mod.reset_autonomy()


def _assert_dial_applied():
    from jarvis.core.models import PlannedAction

    from tests.conftest import TEST_TOKEN  # noqa: F401  (ensures the app fixtures load)
    from jarvis.api.app import get_core

    core = get_core()
    spec = core.tools.get("search_files")
    assert spec is not None
    result = core.policy.evaluate(
        PlannedAction(tool="search_files", device="laptop_main",
                      params={"query": "notes"}))
    assert result.decision is PolicyDecision.CONFIRM, result.reason


# -- over the wire ------------------------------------------------------------

def test_the_dial_is_served_and_set_over_http():
    from tests.conftest import TEST_TOKEN
    from fastapi.testclient import TestClient
    from jarvis.api.app import app

    client = TestClient(app, base_url="http://127.0.0.1:8765")
    headers = {"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"}

    posture = client.get("/autonomy", headers=headers).json()
    assert posture["level"] in LEVELS
    assert "never" in posture["ceiling"]

    moved = client.post("/autonomy", headers=headers, json={"level": 2})
    assert moved.status_code == 200
    assert moved.json()["level"] == 2


def test_the_preview_shows_the_stricter_verdict():
    from tests.conftest import TEST_TOKEN
    from fastapi.testclient import TestClient
    from jarvis.api.app import app

    client = TestClient(app, base_url="http://127.0.0.1:8765")
    headers = {"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"}

    body = client.post("/autonomy/preview", headers=headers,
                       json={"tool": "search_files", "level": 0}).json()
    assert body["final"]["decision"] in {"CONFIRM", "BLOCK"}
    assert body["stricter"] is True


def test_the_preview_does_not_leave_the_dial_moved():
    """A preview must restore the real setting; otherwise asking changes it."""
    from tests.conftest import TEST_TOKEN
    from fastapi.testclient import TestClient
    from jarvis.api.app import app

    client = TestClient(app, base_url="http://127.0.0.1:8765")
    headers = {"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"}

    before = client.get("/autonomy", headers=headers).json()["level"]
    client.post("/autonomy/preview", headers=headers,
                json={"tool": "search_files", "level": 0})
    after = client.get("/autonomy", headers=headers).json()["level"]
    assert before == after


def test_an_unknown_tool_is_a_404_in_the_preview():
    from tests.conftest import TEST_TOKEN
    from fastapi.testclient import TestClient
    from jarvis.api.app import app

    client = TestClient(app, base_url="http://127.0.0.1:8765")
    headers = {"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"}
    response = client.post("/autonomy/preview", headers=headers,
                           json={"tool": "no_such_tool", "level": 0})
    assert response.status_code == 404