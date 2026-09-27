"""Agents package."""

from .base import Agent, ToolAgent, build_agents, find_agent
from .calendar_agent import CalendarAgent
from .car import CarAgent
from .coding_agent import CodingAgent
from .design_agent import DesignAgent
from .vision_agent import VisionAgent
from .watch import WatchAgent
from .email_agent import EmailAgent
from .home import HomeAgent
from .robotics import RoboticsAgent
from .laptop import LaptopAgent
from .laptop_center import LaptopCenterAgent
from .memory_agent import MemoryAgent
from .phone import PhoneAgent
from .research_agent import ResearchAgent
from .scheduler_agent import SchedulerAgent

__all__ = [
    "Agent",
    "ToolAgent",
    "build_agents",
    "find_agent",
    "LaptopAgent",
    "LaptopCenterAgent",
    "MemoryAgent",
    "PhoneAgent",
    "EmailAgent",
    "CalendarAgent",
    "CarAgent",
    "SchedulerAgent",
    "ResearchAgent",
    "CodingAgent",
    "DesignAgent",
    "VisionAgent",
    "WatchAgent",
    "HomeAgent",
    "RoboticsAgent",
]
