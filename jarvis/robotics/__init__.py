"""Robotics / sensor-fleet control (v1.6).

Owns the honest, offline-by-default view of a small research fleet (an AMR and
a robot arm) and, when a ROS bridge is configured, the real transport to it.
"""

from .gateway import (
    ARM_ACTIONS,
    AMR_ACTIONS,
    EXPERIMENTS,
    KINDS,
    Robot,
    RobotGateway,
    TransportResult,
    default_fleet,
    get_robot_gateway,
    reset_robot_gateway,
)

__all__ = [
    "KINDS",
    "AMR_ACTIONS",
    "ARM_ACTIONS",
    "EXPERIMENTS",
    "Robot",
    "RobotGateway",
    "TransportResult",
    "default_fleet",
    "get_robot_gateway",
    "reset_robot_gateway",
]
