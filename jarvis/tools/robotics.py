"""Robotics tools (v1.6).

Every tool reports its own reachability. When no robot bridge is configured
the motion / experiment tools do **not** claim success -- they say plainly
that no motion command was sent or no experiment was started. Only a delivered
command is reported as applied, and it is reported as *confirmed* only when the
bridge verified it.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..core.models import RiskLevel, ToolSpec, Verification
from ..robotics.gateway import ARM_ACTIONS, AMR_ACTIONS, EXPERIMENTS, Robot, get_robot_gateway
from .base import ToolContext, fail, ok

_ALL_ACTIONS = tuple(dict.fromkeys((*AMR_ACTIONS, *ARM_ACTIONS)))


def _candidates(gw: RobotGateway) -> List[str]:
    return [r.name for r in gw.robots]


def _telemetry_line(r: Dict[str, Any]) -> str:
    if r["kind"] == "amr":
        pose = r.get("pose") or {}
        return (
            f"{r['name']}: x={pose.get('x', 0):.2f} y={pose.get('y', 0):.2f} "
            f"theta={pose.get('theta', 0):.1f}\u00b0 speed={r['speed']:.2f} m/s "
            f"battery={r['battery']}% task={r['task']}"
        )
    joints = r.get("joint_angles") or []
    jtext = "[" + ", ".join(f"{v:.1f}" for v in joints) + "]"
    return (
        f"{r['name']}: joints {jtext} gripper={r['gripper']} "
        f"battery={r['battery']}% task={r['task']}"
    )


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

async def get_robot_status(ctx: ToolContext) -> Any:
    gw = get_robot_gateway()
    s = gw.status()
    faults = ", ".join(s["faults"]) if s["faults"] else "none"
    if s["online"]:
        summary = (
            f"Your robot bridge is online ({s['mode']}). "
            f"{s['active']} of {s['robot_count']} robots are active, "
            f"{s['charging']} charging. Safety faults: {faults}."
        )
    else:
        reason = f" ({s['bridge_error']})" if s.get("bridge_error") else ""
        summary = (
            "No robot bridge is configured, so I am offline and showing last "
            f"known state, not commanding anything{reason}. "
            f"{s['active']} of {s['robot_count']} robots were active. "
            f"Safety faults: {faults}."
        )
    return ok("get_robot_status", summary, device=ctx.device_id, **s)


async def list_robots(ctx: ToolContext, kind: str = "") -> Any:
    gw = get_robot_gateway()
    rows = gw.fleet(kind=kind)
    scope = kind.strip() or "the fleet"
    return ok(
        "list_robots",
        f"{len(rows)} robot(s) in {scope}.",
        device=ctx.device_id,
        online=gw.online,
        robots=rows,
    )


async def get_robot_telemetry(ctx: ToolContext, robot: str = "") -> Any:
    gw = get_robot_gateway()
    source = "live" if gw.online else "last known state"
    if robot:
        target = gw.resolve(robot)
        if target is None:
            return fail(
                "get_robot_telemetry",
                f"I could not find a robot called {robot!r}.",
                device=ctx.device_id,
                candidates=_candidates(gw),
            )
        r = gw.telemetry(target)[0]
        return ok(
            "get_robot_telemetry",
            _telemetry_line(r) + f" ({source}).",
            device=ctx.device_id,
            telemetry=r,
            source=source,
        )
    rows = gw.telemetry()
    return ok(
        "get_robot_telemetry",
        "; ".join(_telemetry_line(r) for r in rows) + f" ({source}).",
        device=ctx.device_id,
        source=source,
        telemetry=rows,
    )


# ---------------------------------------------------------------------------
# Write (honest)
# ---------------------------------------------------------------------------

async def command_robot_motion(
    ctx: ToolContext,
    robot: str = "",
    action: str = "",
    pose: Optional[Dict[str, float]] = None,
    speed: Optional[float] = None,
    joints: Optional[List[float]] = None,
    gripper: str = "",
) -> Any:
    gw = get_robot_gateway()
    action = (action or "").strip().lower()
    if not action:
        return fail(
            "command_robot_motion",
            "Which motion should I command? (" + ", ".join(_ALL_ACTIONS) + ")",
            device=ctx.device_id,
        )
    if action not in _ALL_ACTIONS:
        return fail(
            "command_robot_motion",
            f"I do not understand the motion {action!r}.",
            device=ctx.device_id,
        )

    target: Optional[Robot] = gw.resolve(robot) if robot else None
    if target is None:
        # Infer from the action when no robot is named.
        if action in AMR_ACTIONS and action not in ARM_ACTIONS:
            target = gw.resolve("amr")
        elif action in ARM_ACTIONS:
            target = gw.resolve("arm")
        else:
            target = gw.resolve("amr") or gw.resolve("arm")
    if target is None:
        return fail(
            "command_robot_motion",
            f"I could not find a robot called {robot!r}.",
            device=ctx.device_id,
            candidates=_candidates(gw),
        )

    params: Dict[str, Any] = {}
    if pose is not None:
        params["pose"] = pose
    if speed is not None:
        params["speed"] = speed
    if joints is not None:
        params["joints"] = joints
    if gripper:
        params["gripper"] = gripper

    outcome = gw.motion(target, action, **params)

    if outcome.offline:
        return ok(
            "command_robot_motion",
            "No robot bridge is configured, so I could not "
            f"{action} {target.name}. No motion command was sent.",
            device=ctx.device_id,
            robot_id=target.robot_id,
            robot_name=target.name,
            action=action,
            delivered=False,
            verified=False,
            offline=True,
            simulated=outcome.simulated,
        )

    if not outcome.delivered:
        return ok(
            "command_robot_motion",
            f"The {target.kind} did not accept {action!r} ({outcome.reason}). "
            "No motion command was sent.",
            device=ctx.device_id,
            robot_id=target.robot_id,
            robot_name=target.name,
            action=action,
            delivered=False,
            verified=False,
            offline=False,
            simulated=outcome.simulated,
            reason=outcome.reason,
        )

    if outcome.simulated:
        message = f"{action} {target.name} (simulated; no real robot was reached)."
    elif outcome.verified:
        message = f"{action} {target.name} (confirmed by the bridge)."
    else:
        message = f"{action} {target.name} (sent to the bridge, not yet confirmed)."
    return ok(
        "command_robot_motion",
        message,
        device=ctx.device_id,
        # The bridge is a simulator, so no robot moved: unverified with
        # simulated=True, not a verified world change.
        verification=(Verification.UNKNOWN if outcome.simulated else None),
        robot_id=target.robot_id,
        robot_name=target.name,
        action=action,
        delivered=True,
        verified=outcome.verified,
        simulated=outcome.simulated,
        state=target.to_dict(),
    )


async def run_experiment(ctx: ToolContext, experiment: str = "") -> Any:
    gw = get_robot_gateway()
    experiment = (experiment or "").strip().lower()
    if not experiment:
        return fail(
            "run_experiment",
            "Which experiment should I run? (" + ", ".join(EXPERIMENTS) + ")",
            device=ctx.device_id,
            available=list(EXPERIMENTS),
        )
    result = gw.run_experiment(experiment)
    if not result.get("ok"):
        return fail(
            "run_experiment",
            result.get("error", "unknown experiment"),
            device=ctx.device_id,
            available=result.get("available"),
        )
    if result["offline"]:
        would = ", ".join(f"{s['name']} {s['action']}" for s in result["steps"])
        return ok(
            "run_experiment",
            "No robot bridge is configured, so I could not start "
            f"{experiment!r}. It would involve: {would}. No experiment was started.",
            device=ctx.device_id,
            experiment=experiment,
            offline=True,
            simulated=result["simulated"],
            total=result["total"],
            applied=0,
            steps=result["steps"],
        )
    if result["simulated"]:
        message = (
            f"Started {experiment!r} (simulated): {result['applied']} of {result['total']} "
            "motion step(s) applied. No real robot was reached."
        )
    else:
        confirmed = all(s["verified"] for s in result["steps"])
        tail = "" if confirmed else ", some not yet confirmed"
        message = (
            f"Started {experiment!r}: {result['applied']} of {result['total']} "
            f"motion step(s) applied{tail}."
        )
    return ok(
        "run_experiment",
        message,
        device=ctx.device_id,
        # See command_robot_motion.
        verification=(Verification.UNKNOWN if result["simulated"] else None),
        experiment=experiment,
        offline=False,
        simulated=result["simulated"],
        total=result["total"],
        applied=result["applied"],
        steps=result["steps"],
    )


_SPECS = [
    (
        ToolSpec(
            name="get_robot_status",
            description="Report robot fleet status: counts, activity, charging and safety faults.",
            agent="robotics",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={},
            tags=["robotics", "read"],
        ),
        get_robot_status,
    ),
    (
        ToolSpec(
            name="list_robots",
            description="List robots in the fleet, optionally filtered by kind (amr, arm).",
            agent="robotics",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={"kind": "string"},
            tags=["robotics", "read"],
        ),
        list_robots,
    ),
    (
        ToolSpec(
            name="get_robot_telemetry",
            description="Read robot telemetry: pose and speed for an AMR, joint angles and gripper for an arm.",
            agent="robotics",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={"robot": "string"},
            tags=["robotics", "read"],
        ),
        get_robot_telemetry,
    ),
    (
        ToolSpec(
            name="command_robot_motion",
            description="Command a robot motion (move, home, charge, stop, move_joints, gripper). Physical; reported honestly.",
            agent="robotics",
            risk=RiskLevel.PHYSICAL,
            requires_confirmation=True,
            reversible=True,
            allowed_devices=["laptop_main"],
            parameters={
                "robot": "string",
                "action": "string",
                "pose": "object",
                "speed": "number",
                "joints": "array",
                "gripper": "string",
            },
            required_parameters=["robot", "action"],
            tags=["robotics", "motion"],
        ),
        command_robot_motion,
    ),
    (
        ToolSpec(
            name="run_experiment",
            description="Run a named robot experiment routine (map_lab, pick_place, dock_charge). Physical; reported honestly.",
            agent="robotics",
            risk=RiskLevel.PHYSICAL,
            requires_confirmation=True,
            reversible=True,
            allowed_devices=["laptop_main"],
            parameters={"experiment": "string"},
            required_parameters=["experiment"],
            tags=["robotics", "experiment"],
        ),
        run_experiment,
    ),
]

SPECS = _SPECS
