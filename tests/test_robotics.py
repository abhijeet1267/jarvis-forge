"""Robotics / sensor-fleet agent tests (v1.6).

The whole point of this agent is *honest reachability*. With no robot bridge
configured the gateway is offline, and the motion / experiment tools must say
plainly that no motion command was sent or no experiment was started -- never
fabricate a physical success. These tests pin that down, plus the simulate and
online paths, the experiment routines, and the planner routing (including the
policy confirmation gate for physical actions).
"""

from __future__ import annotations

import pytest

from jarvis.core.orchestrator import JarvisCore
from jarvis.core.models import Verification
from jarvis.robotics import (
    RobotGateway,
    TransportResult,
    default_fleet,
    get_robot_gateway,
    reset_robot_gateway,
)
from jarvis.tools.base import ToolContext
from jarvis.tools.robotics import (
    command_robot_motion,
    get_robot_status,
    get_robot_telemetry,
    list_robots,
    run_experiment,
)

ctx = ToolContext()


@pytest.fixture(autouse=True)
def _fresh_gateway(monkeypatch: pytest.MonkeyPatch):
    """Keep tests independent: no simulate/bridge flags, one clean gateway each test."""
    monkeypatch.delenv("JARVIS_ROBOT_SIMULATE", raising=False)
    monkeypatch.delenv("JARVIS_ROBOT_BRIDGE", raising=False)
    reset_robot_gateway()
    yield
    reset_robot_gateway()


class _FakeTransport:
    def __init__(self, delivered: bool = True, verified: bool = True, detail: str = "ok"):
        self.delivered = delivered
        self.verified = verified
        self.detail = detail
        self.sent = []

    def send(self, topic, payload):
        self.sent.append((topic, dict(payload)))
        return TransportResult(self.delivered, self.verified, self.detail)


# ---------------------------------------------------------------------------
# Gateway (domain) behaviour
# ---------------------------------------------------------------------------

def test_gateway_offline_by_default() -> None:
    gw = get_robot_gateway()
    assert gw.online is False
    assert gw.mode == "offline"
    assert gw.simulate is False
    assert gw.robots, "default fleet should not be empty"


def test_resolve_robots() -> None:
    gw = get_robot_gateway()
    assert gw.resolve("amr").robot_id == "amr_main"
    assert gw.resolve("the amr").robot_id == "amr_main"
    assert gw.resolve("amr_main").robot_id == "amr_main"
    assert gw.resolve("robot arm").robot_id == "arm_01"
    assert gw.resolve("arm_01").robot_id == "arm_01"
    assert gw.resolve("a nonexistent robot") is None


def test_motion_offline_does_not_change_state() -> None:
    gw = get_robot_gateway()
    robot = gw.resolve("amr_main")
    assert robot is not None and robot.pose == {"x": 0.0, "y": 0.0, "theta": 0.0}
    outcome = gw.motion(robot, "move", pose={"x": 1.0, "y": 2.0, "theta": 0.0})
    assert outcome.offline is True
    assert outcome.delivered is False
    assert robot.pose == {"x": 0.0, "y": 0.0, "theta": 0.0}, "offline motion must not mutate state"


def test_motion_simulate_updates_state() -> None:
    gw = RobotGateway(simulate=True)
    robot = gw.resolve("amr_main")
    assert robot.pose == {"x": 0.0, "y": 0.0, "theta": 0.0}
    outcome = gw.motion(robot, "move", pose={"x": 1.0, "y": 2.0, "theta": 90.0}, speed=0.4)
    assert outcome.offline is False
    assert outcome.delivered is True
    assert outcome.simulated is True
    assert robot.pose == {"x": 1.0, "y": 2.0, "theta": 90.0}
    assert robot.speed == 0.4
    arm = gw.resolve("arm_01")
    gw.motion(arm, "gripper", gripper="open")
    assert arm.gripper == "open"


def test_motion_online_verified_and_unverified() -> None:
    transport = _FakeTransport(delivered=True, verified=True)
    gw = RobotGateway(online=True, transport=transport)
    outcome = gw.motion(gw.resolve("amr_main"), "home")
    assert outcome.delivered is True and outcome.verified is True

    flaky = _FakeTransport(delivered=True, verified=False, detail="sent to bridge; not verified")
    gw2 = RobotGateway(online=True, transport=flaky)
    outcome2 = gw2.motion(gw2.resolve("amr_main"), "home")
    assert outcome2.delivered is True and outcome2.verified is False

    refused = _FakeTransport(delivered=False, verified=False, detail="bridge rejected")
    gw3 = RobotGateway(online=True, transport=refused)
    outcome3 = gw3.motion(gw3.resolve("amr_main"), "home")
    assert outcome3.delivered is False and outcome3.verified is False


def test_motion_rejects_invalid_action() -> None:
    gw = get_robot_gateway()
    arm = gw.resolve("arm_01")
    outcome = gw.motion(arm, "move")
    assert outcome.delivered is False
    assert "does not support" in outcome.reason


def test_run_experiment_offline_reports_not_applied() -> None:
    gw = get_robot_gateway()
    result = gw.run_experiment("map_lab")
    assert result["ok"] is True
    assert result["offline"] is True
    assert result["applied"] == 0
    assert result["total"] > 0


def test_run_experiment_simulate_applies_everything() -> None:
    gw = RobotGateway(simulate=True)
    result = gw.run_experiment("map_lab")
    assert result["offline"] is False
    assert result["simulated"] is True
    assert result["applied"] == result["total"]
    assert gw.resolve("amr_main").task == "moving"


def test_run_experiment_unknown() -> None:
    gw = get_robot_gateway()
    result = gw.run_experiment("dance")
    assert result["ok"] is False
    assert "map_lab" in result["available"]


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------

async def test_get_robot_status_offline() -> None:
    result = await get_robot_status(ctx)
    assert result.ok is True
    assert result.output["online"] is False
    assert result.output["mode"] == "offline"
    assert "offline" in result.message.lower()
    assert result.output["robot_count"] == len(default_fleet())


async def test_list_robots_filters() -> None:
    all_robots = await list_robots(ctx)
    assert all_robots.ok is True
    assert all_robots.output["robots"], "should list the default fleet"

    amrs = await list_robots(ctx, kind="amr")
    assert all(r["kind"] == "amr" for r in amrs.output["robots"])

    arms = await list_robots(ctx, kind="arm")
    assert all(r["kind"] == "arm" for r in arms.output["robots"])


async def test_get_robot_telemetry_all_and_one() -> None:
    all_t = await get_robot_telemetry(ctx)
    assert all_t.ok is True
    assert all_t.output["source"] == "last known state"
    assert len(all_t.output["telemetry"]) == len(default_fleet())

    amr = await get_robot_telemetry(ctx, robot="amr")
    assert amr.ok is True
    assert amr.output["telemetry"]["kind"] == "amr"
    assert "amr main" in amr.message.lower()


async def test_get_robot_telemetry_unknown() -> None:
    result = await get_robot_telemetry(ctx, robot="submarine")
    assert result.ok is False
    assert result.output.get("candidates")


# ---------------------------------------------------------------------------
# Write tools
# ---------------------------------------------------------------------------

async def test_motion_offline_is_honest() -> None:
    result = await command_robot_motion(ctx, robot="amr", action="move")
    assert result.ok is True  # the *call* succeeded; the *action* did not happen
    assert result.output["delivered"] is False
    assert result.output["offline"] is True
    assert "no motion command was sent" in result.message.lower()


async def test_motion_simulate_reports_simulated() -> None:
    reset_robot_gateway(RobotGateway(simulate=True))
    result = await command_robot_motion(ctx, robot="amr", action="home")
    assert result.output["delivered"] is True
    assert result.output["simulated"] is True
    assert "simulated" in result.message.lower()


async def test_motion_online_unverified_not_claimed_done() -> None:
    reset_robot_gateway(RobotGateway(online=True, transport=_FakeTransport(verified=False)))
    result = await command_robot_motion(ctx, robot="amr", action="home")
    assert result.output["delivered"] is True
    # `verified` is an ActionResult field now, not an output key (Phase 9 P1-9).
    assert result.verified is False
    assert result.verification is not Verification.SUCCESS
    assert "not yet confirmed" in result.message.lower()


async def test_motion_requires_action() -> None:
    result = await command_robot_motion(ctx, robot="amr")
    assert result.ok is False


async def test_motion_unknown_action() -> None:
    result = await command_robot_motion(ctx, robot="amr", action="teleport")
    assert result.ok is False


async def test_run_experiment_offline_message() -> None:
    result = await run_experiment(ctx, experiment="map_lab")
    assert result.ok is True
    assert result.output["offline"] is True
    assert result.output["applied"] == 0
    assert "no experiment was started" in result.message.lower()


async def test_run_experiment_simulate_message() -> None:
    reset_robot_gateway(RobotGateway(simulate=True))
    result = await run_experiment(ctx, experiment="map_lab")
    assert result.output["offline"] is False
    assert result.output["simulated"] is True
    assert result.output["applied"] == result.output["total"]


async def test_run_experiment_requires_name() -> None:
    result = await run_experiment(ctx)
    assert result.ok is False
    assert "map_lab" in result.output["available"]


async def test_run_experiment_unknown_name() -> None:
    result = await run_experiment(ctx, experiment="dance")
    assert result.ok is False
    assert "map_lab" in result.output["available"]


# ---------------------------------------------------------------------------
# Planner routing
# ---------------------------------------------------------------------------

def test_planner_routes_experiment(core: JarvisCore) -> None:
    plan = core.planner.parse("run today's experiment")
    assert plan.actions and plan.actions[0].tool == "run_experiment"


def test_planner_routes_motion(core: JarvisCore) -> None:
    plan = core.planner.parse("move the amr to (1, 2)")
    assert plan.actions[0].tool == "command_robot_motion"
    assert plan.actions[0].params["action"] == "move"


def test_planner_routes_status_and_telemetry(core: JarvisCore) -> None:
    status = core.planner.parse("what is the status of my robots")
    assert status.actions[0].tool in {"get_robot_status", "list_robots"}

    telemetry = core.planner.parse("where is the amr")
    assert telemetry.actions[0].tool == "get_robot_telemetry"


def test_planner_routes_robot_battery_to_telemetry(core: JarvisCore) -> None:
    plan = core.planner.parse("what is the amr battery")
    assert plan.actions[0].tool == "get_robot_telemetry"


def test_planner_emergency_stop_beats_robotics(core: JarvisCore) -> None:
    plan = core.planner.parse("stop the amr")
    assert plan.actions[0].tool == "emergency_stop"


def test_planner_does_not_steal_plain_laptop_battery(core: JarvisCore) -> None:
    plan = core.planner.parse("what is my battery")
    assert plan.actions[0].tool == "get_battery_status"


# ---------------------------------------------------------------------------
# End-to-end through the full loop
# ---------------------------------------------------------------------------

async def test_e2e_status_when_offline(core: JarvisCore, make_request) -> None:
    response = await core.handle(make_request("what is the status of my robots"))
    assert response.actions and response.actions[0].ok
    assert "offline" in response.reply.lower()


async def test_e2e_motion_requires_confirmation(core: JarvisCore, make_request) -> None:
    response = await core.handle(make_request("move the amr to (1, 2)"))
    assert response.needs_confirmation is True
    assert response.confirmation_id
    assert "confirm" in response.reply.lower()
