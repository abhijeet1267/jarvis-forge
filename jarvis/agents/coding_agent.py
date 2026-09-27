"""Coding Agent (v1.1).

Works inside controlled project workspaces. It can inspect, search, propose and
apply changes, run tests and produce reports.

What it will not do: touch anything outside its workspace, delete files (there
is no delete path), add an interpreter to the command allowlist to make tests
runnable, install packages, or push code.
"""

from __future__ import annotations

from ..registry.tools import ToolRegistry
from .base import Agent


class CodingAgent(Agent):
    """Repository work inside a bounded workspace."""

    name = "coding"

    def __init__(self, registry: ToolRegistry) -> None:
        super().__init__(registry)

    def capabilities(self) -> set:
        return {
            "open_workspace",
            "list_workspaces",
            "close_workspace",
            "inspect_workspace",
            "search_code",
            "propose_code_change",
            "apply_code_change",
            "run_workspace_tests",
            "coding_report",
            "commit_workspace",
        }
