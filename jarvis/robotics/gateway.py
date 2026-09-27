"""Robot fleet gateway (v1.6).

A direct mirror of the :mod:`jarvis.home.gateway`: the JARVIS process holds a
view of a small research fleet (an autonomous mobile robot and a robot arm)
and, when a ROS bridge is configured, a real transport to it.

By default there is **no** robot bridge, so the gateway is *offline* and every
control call honestly reports that nothing was sent -- it never pretends a
physical action happened. Three states, exactly like the home gateway:

- **offline** -- no bridge. Reads return last-known state, labelled as such.
  Motion / experiment commands report ``delivered=False`` and "no motion
  command was sent" / "no experiment was started".
- **simulate** -- ``JARVIS_ROBOT_SIMULATE=1``. Commands are applied to the
  in-memory fleet and labelled "simulated". Deterministic; no hardware.
- **online** -- ``JARVIS_ROBOT_BRIDGE=host:port``. Commands are sent over a
  real TCP socket; delivery is reported from the socket write and verification
  only from a real acknowledgement from the bridge (this minimal bridge does
  not send one, so delivery is never conflated with confirmation).

The safety controller is deliberately **not** part of this gateway. Per the
plan, temperature/current limits, the watchdog and the emergency stop live on
the robot itself, independent of JARVIS. JARVIS is one (permissioned,
confirmable) command source among several -- never the only one.
"""

from __future__ import annotations

import json
import os
import re
import socket
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, Tuple

from ..core.models import utcnow
from ..logging_setup import get_logger

log = get_logger("robotics.gateway")

KINDS = ("amr", "arm")

#: Motion actions each robot kind can perform. "stop" is a local, reversible
#: stop of one robot's motion; the *global* "stop all robots / stop the
#: experiment" is owned by the safety controller's emergency-stop tool.
AMR_ACTIONS = ("move", "home", "charge", "stop")
ARM_ACTIONS = ("move_joints", "gripper", "home", "stop")

#: Words that map a loose user phrase onto a robot kind during resolution.
ROBOT_SYNONYMS: Dict[str, set] = {
    # Note: the bare, kind-ambiguous words "robot" / "robots" are deliberately
    # NOT here -- they point at the fleet in general, not at a specific kind,
    # so "a robot" must not resolve to one particular machine.
    "amr": {
        "amr", "amrs", "mobile robot", "mobile", "vehicle",
        "drive", "driving", "pose", "position", "location", "map", "mapping",
    },
    "arm": {
        "arm", "arms", "robot arm", "gripper", "grippers", "manipulator",
        "joints", "joint", "angles",
    },
}

#: Named experiment routines: name -> list of (robot_id, action, params).
EXPERIMENTS: Dict[str, List[Tuple[str, str, Dict[str, Any]]]] = {
    "map_lab": [
        ("amr_main", "home", {}),
        ("amr_main", "move", {"pose": {"x": 1.0, "y": 1.0, "theta": 0.0}, "speed": 0.5}),
        ("amr_main", "move", {"pose": {"x": 2.0, "y": 1.0, "theta": 90.0}, "speed": 0.5}),
        ("amr_main", "move", {"pose": {"x": 2.0, "y": 2.0, "theta": 180.0}, "speed": 0.5}),
    ],
    "pick_place": [
        ("arm_01", "gripper", {"gripper": "open"}),
        ("arm_01", "move_joints", {"joints": [30.0, -40.0, 20.0, 60.0, 45.0, 0.0]}),
        ("arm_01", "gripper", {"gripper": "close"}),
        ("arm_01", "move_joints", {"joints": [30.0, -20.0, 10.0, 30.0, 20.0, 0.0]}),
        ("arm_01", "gripper", {"gripper": "open"}),
        ("arm_01", "home", {}),
    ],
    "dock_charge": [
        ("amr_main", "move", {"pose": {"x": 0.0, "y": 0.0, "theta": 0.0}}),
        ("amr_main", "charge", {}),
    ],
}


@dataclass
class Robot:
    """A last-known-state view of one robot."""

    robot_id: str
    kind: str                       # "amr" | "arm"
    name: str
    controllable: bool = True
    battery: Optional[float] = None          # 0..100, None = no sensor
    charging: bool = False
    pose: Optional[Dict[str, float]] = None  # amr: x, y, theta(degrees)
    home_pose: Optional[Dict[str, float]] = None
    joint_angles: Optional[List[float]] = None   # arm, degrees
    home_joints: Optional[List[float]] = None
    speed: float = 0.0                               # m/s (amr)
    gripper: str = "closed"                            # arm: open | closed
    task: str = "idle"        # idle | moving | executing | charging | fault
    safety: str = "safe"      # safe | degraded | fault
    last_changed: Optional[Any] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "robot_id": self.robot_id,
            "kind": self.kind,
            "name": self.name,
            "controllable": self.controllable,
            "battery": self.battery,
            "charging": self.charging,
            "pose": self.pose,
            "home_pose": self.home_pose,
            "joint_angles": self.joint_angles,
            "home_joints": self.home_joints,
            "speed": self.speed,
            "gripper": self.gripper,
            "task": self.task,
            "safety": self.safety,
        }


@dataclass
class MotionOutcome:
    """Result of a single motion command, reported honestly."""

    robot_id: str
    robot_name: str
    action: str
    command: Dict[str, Any]
    delivered: bool
    verified: bool
    simulated: bool
    offline: bool
    reason: str = ""


def default_fleet() -> List[Robot]:
    now = utcnow()
    return [
        Robot(
            robot_id="amr_main",
            kind="amr",
            name="AMR main",
            battery=84.0,
            pose={"x": 0.0, "y": 0.0, "theta": 0.0},
            home_pose={"x": 0.0, "y": 0.0, "theta": 0.0},
            speed=0.0,
            task="idle",
            last_changed=now,
        ),
        Robot(
            robot_id="arm_01",
            kind="arm",
            name="Robot arm 01",
            battery=91.0,
            joint_angles=[0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            home_joints=[0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            gripper="closed",
            task="idle",
            last_changed=now,
        ),
    ]


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------

@dataclass
class TransportResult:
    delivered: bool
    verified: bool
    detail: str = ""


class RobotTransport(Protocol):
    def send(self, topic: str, payload: Dict[str, Any]) -> TransportResult:  # pragma: no cover
        ...


class OfflineTransport:
    """No bridge. Nothing is sent and nothing is claimed."""

    def send(self, topic: str, payload: Dict[str, Any]) -> TransportResult:
        return TransportResult(False, False, "no robot bridge configured")


class SimulatedTransport:
    """Deterministic local stand-in. Everything is delivered and verified."""

    def send(self, topic: str, payload: Dict[str, Any]) -> TransportResult:
        return TransportResult(True, True, "simulated")


class BridgeTransport:
    """A real TCP socket to a ROS/robot bridge (host:port).

    Used only when ``JARVIS_ROBOT_BRIDGE`` is set. If the bridge is not
    reachable the gateway degrades to offline and reports the reason honestly.
    Delivery is reported from the socket write; verification would require an
    acknowledgement that this minimal bridge does not send.
    """

    def __init__(self, host: str, port: int, timeout: float = 3.0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock: Optional[socket.socket] = None

    def _connect(self) -> bool:
        try:
            self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            self._sock.settimeout(self.timeout)
            return True
        except OSError as exc:
            self._sock = None
            log.warning(
                "robot bridge connect failed",
                extra_fields={"host": self.host, "port": self.port, "error": str(exc)},
            )
            return False

    def ping(self) -> bool:
        return self._connect()

    def send(self, topic: str, payload: Dict[str, Any]) -> TransportResult:
        if self._sock is None and not self._connect():
            return TransportResult(False, False, f"cannot reach robot bridge {self.host}:{self.port}")
        if self._sock is None:
            return TransportResult(False, False, "cannot reach robot bridge")
        data = json.dumps({"topic": topic, **payload}) + "\n"
        try:
            self._sock.sendall(data.encode("utf-8"))
        except OSError as exc:
            self._sock = None
            return TransportResult(False, False, f"send failed: {exc}")
        return TransportResult(True, False, f"sent to {self.host}:{self.port}; not verified")

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ARTICLES = {"the", "a", "an", "my", "our", "your"}


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _topic_for(robot: Robot) -> str:
    return f"jarvis/robot/{robot.robot_id}/cmd"


def _command_from(action: str, **params: Any) -> Dict[str, Any]:
    cmd: Dict[str, Any] = {"action": action}
    if params.get("pose") is not None:
        cmd["pose"] = {k: float(v) for k, v in dict(params["pose"]).items()}
    if params.get("speed") is not None:
        cmd["speed"] = float(params["speed"])
    if params.get("joints") is not None:
        cmd["joints"] = [float(v) for v in params["joints"]]
    if params.get("gripper") is not None:
        cmd["gripper"] = str(params["gripper"]).lower()
    return cmd


def _actions_for(robot: Robot) -> Tuple[str, ...]:
    return AMR_ACTIONS if robot.kind == "amr" else ARM_ACTIONS


def _apply_motion(robot: Robot, command: Dict[str, Any]) -> None:
    """Update in-memory state after a command was actually delivered."""
    action = command.get("action")
    if robot.kind == "amr":
        if action == "move" and command.get("pose"):
            robot.pose = dict(command["pose"])
            if command.get("speed") is not None:
                robot.speed = command["speed"]
            robot.task = "moving"
        elif action == "home" and robot.home_pose:
            robot.pose = dict(robot.home_pose)
            robot.speed = 0.0
            robot.task = "moving"
        elif action == "charge":
            robot.charging = True
            robot.speed = 0.0
            robot.task = "charging"
        elif action == "stop":
            robot.speed = 0.0
            robot.task = "idle"
    elif robot.kind == "arm":
        if action == "move_joints" and command.get("joints"):
            robot.joint_angles = list(command["joints"])
            robot.task = "moving"
        elif action == "gripper" and command.get("gripper"):
            robot.gripper = command["gripper"]
            robot.task = "executing"
        elif action == "home" and robot.home_joints:
            robot.joint_angles = list(robot.home_joints)
            robot.task = "moving"
        elif action == "stop":
            robot.task = "idle"


# ---------------------------------------------------------------------------
# Gateway
# ---------------------------------------------------------------------------

class RobotGateway:
    """Owns the robot fleet and the honest transport to it."""

    def __init__(
        self,
        robots: Optional[List[Robot]] = None,
        transport: Optional[RobotTransport] = None,
        *,
        online: Optional[bool] = None,
        simulate: Optional[bool] = None,
    ) -> None:
        self.robots: List[Robot] = robots if robots is not None else default_fleet()
        self._by_id: Dict[str, Robot] = {r.robot_id: r for r in self.robots}
        self._transport: Optional[RobotTransport] = transport
        self._online_override: Optional[bool] = online
        self._simulate_override: Optional[bool] = simulate
        self._bridge_error: str = ""

    # -- connectivity ------------------------------------------------------
    @property
    def simulate(self) -> bool:
        if self._simulate_override is not None:
            return bool(self._simulate_override)
        return bool(os.environ.get("JARVIS_ROBOT_SIMULATE"))

    @property
    def transport(self) -> RobotTransport:
        if self._transport is not None:
            return self._transport
        self._transport = self._default_transport()
        return self._transport

    def _default_transport(self) -> RobotTransport:
        if self.simulate:
            return SimulatedTransport()
        bridge = os.environ.get("JARVIS_ROBOT_BRIDGE", "").strip()
        if bridge:
            host, sep, port = bridge.rpartition(":")
            host = host or "127.0.0.1"
            try:
                port = int(port) if sep and port else 9090
            except ValueError:
                port = 9090
            transport = BridgeTransport(host, port)
            if transport.ping():
                return transport
            self._bridge_error = f"could not reach robot bridge {host}:{port}"
            log.warning("robot bridge unreachable; gateway is offline", extra_fields={"bridge": bridge})
            return OfflineTransport()
        return OfflineTransport()

    @property
    def online(self) -> bool:
        if self._online_override is not None:
            return bool(self._online_override)
        return isinstance(self.transport, (SimulatedTransport, BridgeTransport))

    @property
    def mode(self) -> str:
        if not self.online:
            return "offline"
        if self.simulate:
            return "simulate"
        if isinstance(self.transport, BridgeTransport):
            return "bridge"
        return "online"

    # -- discovery ---------------------------------------------------------
    def resolve(self, query: str) -> Optional[Robot]:
        q = _normalise(query)
        if not q:
            return None
        if q in self._by_id:
            return self._by_id[q]
        for robot_id, robot in self._by_id.items():
            if q == robot_id or q == robot_id.replace("_", " "):
                return robot
        for robot in self.robots:
            if q == robot.name.lower():
                return robot
        best: Optional[Robot] = None
        best_score = 0
        for robot in self.robots:
            score = self._score(robot, q)
            if score > best_score:
                best, best_score = robot, score
        return best

    @staticmethod
    def _score(robot: Robot, q: str) -> int:
        name = robot.name.lower()
        score = 0
        if q and q in name:
            score += 4
        if q and name in q:
            score += 3
        tokens = set(q.split()) - _ARTICLES
        if tokens & ROBOT_SYNONYMS.get(robot.kind, {robot.kind}):
            score += 2
        if robot.kind in tokens or robot.kind in q:
            score += 2
        return score

    # -- reads -------------------------------------------------------------
    def status(self) -> Dict[str, Any]:
        by_kind: Dict[str, int] = {}
        active = charging = 0
        faults: List[str] = []
        for r in self.robots:
            by_kind[r.kind] = by_kind.get(r.kind, 0) + 1
            if r.task != "idle":
                active += 1
            if r.charging:
                charging += 1
            if r.safety == "fault":
                faults.append(r.name)
        return {
            "online": self.online,
            "mode": self.mode,
            "simulated": self.simulate,
            "robot_count": len(self.robots),
            "by_kind": by_kind,
            "active": active,
            "charging": charging,
            "faults": faults,
            "bridge_error": self._bridge_error or None,
        }

    def fleet(self, kind: str = "") -> List[Dict[str, Any]]:
        """Robot rows (as dicts), optionally filtered by kind.

        Named ``fleet`` (not ``robots``) because ``self.robots`` is the
        underlying list attribute; a method of the same name would be shadowed
        by it on the instance.
        """
        kind_l = kind.strip().lower()
        return [
            r.to_dict()
            for r in self.robots
            if not kind_l or r.kind == kind_l
        ]

    def telemetry(self, robot: Optional[Robot] = None) -> List[Dict[str, Any]]:
        source = "live" if self.online else "last_known"
        rows: List[Dict[str, Any]] = []
        for r in ([robot] if robot is not None else self.robots):
            rows.append(
                {
                    "robot_id": r.robot_id,
                    "name": r.name,
                    "kind": r.kind,
                    "battery": r.battery,
                    "charging": r.charging,
                    "pose": r.pose,
                    "speed": r.speed,
                    "joint_angles": r.joint_angles,
                    "gripper": r.gripper,
                    "task": r.task,
                    "safety": r.safety,
                    "source": source,
                }
            )
        return rows

    # -- writes (honest) ---------------------------------------------------
    def motion(self, robot: Robot, action: str, **params: Any) -> MotionOutcome:
        command = _command_from(action, **params)
        offline = not self.online
        if action not in _actions_for(robot):
            return MotionOutcome(
                robot.robot_id, robot.name, action, command,
                False, False, self.simulate, offline,
                f"{robot.kind} does not support {action!r}",
            )
        if not robot.controllable:
            return MotionOutcome(
                robot.robot_id, robot.name, action, command,
                False, False, self.simulate, offline, "not controllable",
            )
        if offline:
            # Nothing is sent and local state is left exactly as it was.
            return MotionOutcome(
                robot.robot_id, robot.name, action, command,
                False, False, self.simulate, True, self._bridge_error or "no robot bridge configured",
            )
        result = self.transport.send(_topic_for(robot), command)
        if result.delivered:
            _apply_motion(robot, command)
            robot.last_changed = utcnow()
        return MotionOutcome(
            robot.robot_id, robot.name, action, command,
            result.delivered,
            bool(result.verified and result.delivered),
            self.simulate,
            False,
            result.detail,
        )

    def run_experiment(self, name: str) -> Dict[str, Any]:
        steps = EXPERIMENTS.get(name)
        if steps is None:
            return {"ok": False, "error": f"unknown experiment {name!r}", "available": list(EXPERIMENTS)}
        offline = not self.online
        results: List[Dict[str, Any]] = []
        for robot_id, action, params in steps:
            robot = self._by_id.get(robot_id)
            if robot is None:
                continue
            outcome = self.motion(robot, action, **params)
            results.append(
                {
                    "robot_id": robot_id,
                    "name": robot.name,
                    "action": action,
                    "delivered": outcome.delivered,
                    "verified": outcome.verified,
                }
            )
        applied = sum(1 for r in results if r["delivered"])
        return {
            "ok": True,
            "experiment": name,
            "offline": offline,
            "simulated": self.simulate,
            "total": len(results),
            "applied": 0 if offline else applied,
            "steps": results,
        }


# ---------------------------------------------------------------------------
# Process-wide gateway (mirrors the home gateway)
# ---------------------------------------------------------------------------

_gateway: Optional[RobotGateway] = None


def get_robot_gateway() -> RobotGateway:
    global _gateway
    if _gateway is None:
        _gateway = RobotGateway()
    return _gateway


def reset_robot_gateway(gateway: Optional[RobotGateway] = None) -> None:
    global _gateway
    if _gateway is not None and isinstance(_gateway.transport, BridgeTransport):
        _gateway.transport.close()
    _gateway = gateway
