"""Controlled workspaces (v1.1).

The plan says the Coding Agent works *inside controlled project workspaces*, and
separately that it must never modify protected paths or delete the repository.
Both come down to one primitive: a boundary that every path is checked against
before anything reads or writes.

:class:`Workspace` is that boundary. It refuses:

* any path that escapes the root (``..`` traversal, symlinks pointing outside)
* any protected path (via the single source of truth in ``security.paths``)
* writes outside the root, always

Everything else in the coding package goes through :meth:`Workspace.contain`, so
there is no code path that touches a file outside the workspace.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from ..core.models import new_id
from ..logging_setup import get_logger
from ..security.paths import protection_reason, resolve_path

log = get_logger("coding.workspace")

#: Directories never descended into when walking a workspace.
SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".tox", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "dist", "build", ".idea", ".vscode", "site-packages",
}

#: Extensions treated as source, with the language they imply.
SOURCE_EXTENSIONS = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".jsx": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".rb": "ruby",
    ".sh": "shell",
    ".md": "markdown",
    ".toml": "toml",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
}

#: Filename patterns that indicate tests.
TEST_PATTERNS = ("test_", "_test.", "tests/", "/tests", ".test.", "spec_", "_spec.")


@dataclass
class Workspace:
    """A directory the Coding Agent is allowed to work in."""

    workspace_id: str
    root: Path
    name: str = ""
    created_at: str = ""

    @property
    def display(self) -> str:
        return self.name or self.root.name or str(self.root)

    # -- boundary ------------------------------------------------------------
    def contain(self, value: str) -> Optional[Path]:
        """Resolve a path and require it to sit inside the workspace.

        Returns ``None`` when the path escapes, is protected, or cannot be
        resolved. Callers must treat ``None`` as a refusal, not fall back.
        """
        if not value:
            return None

        # A relative path is relative to the workspace, not the process cwd --
        # otherwise "pkg/core.py" would resolve somewhere unrelated.
        candidate_path = Path(str(value).strip())
        if not candidate_path.is_absolute():
            candidate_path = self.root / candidate_path

        resolved = resolve_path(str(candidate_path))
        if not resolved:
            return None

        candidate = Path(resolved).resolve()

        # The root itself is allowed; anything else must be strictly inside.
        root = self.root.resolve()
        if candidate != root and root not in candidate.parents:
            log.info(
                "path outside workspace refused",
                extra_fields={"workspace": self.display, "path": str(value)},
            )
            return None

        reason = protection_reason(str(candidate))
        if reason:
            log.info("protected path refused", extra_fields={"path": str(candidate)})
            return None

        return candidate

    def relative(self, path: Path) -> str:
        """A display path relative to the root, when possible."""
        try:
            return str(path.relative_to(self.root.resolve()))
        except ValueError:
            return str(path)


class WorkspaceRegistry:
    """The workspaces JARVIS currently has open.

    Kept in memory, keyed by id, and always derived from a real directory that
    passed :meth:`Workspace.contain` checks at open time.
    """

    def __init__(self) -> None:
        self._workspaces: Dict[str, Workspace] = {}

    def open(self, path: str, name: str = "") -> Optional[Workspace]:
        """Open a workspace, or None if the path is not usable."""
        resolved = resolve_path(str(path or "").strip())
        if not resolved:
            return None

        root = Path(resolved).resolve()
        if not root.is_dir():
            return None

        reason = protection_reason(str(root))
        if reason:
            return None

        workspace = Workspace(
            workspace_id=new_id("WS"),
            root=root,
            name=name or root.name,
        )
        self._workspaces[workspace.workspace_id] = workspace
        log.info("workspace opened", extra_fields={"id": workspace.workspace_id, "root": str(root)})
        return workspace

    def get(self, workspace_id: str) -> Optional[Workspace]:
        return self._workspaces.get(workspace_id)

    def all(self) -> List[Workspace]:
        return list(self._workspaces.values())

    def close(self, workspace_id: str) -> bool:
        return self._workspaces.pop(workspace_id, None) is not None

    def resolve(self, ref: str) -> Optional[Workspace]:
        """Find a workspace by id or by name."""
        workspace = self.get(ref)
        if workspace is not None:
            return workspace
        needle = (ref or "").strip().lower()
        for candidate in self._workspaces.values():
            if candidate.display.lower() == needle:
                return candidate
        return None


_registry: Optional[WorkspaceRegistry] = None


def get_workspace_registry() -> WorkspaceRegistry:
    """The process-wide workspace registry.

    A shared instance, so a workspace opened in one tool is visible to the rest.
    """
    global _registry
    if _registry is None:
        _registry = WorkspaceRegistry()
    return _registry


def is_source_file(path: Path) -> bool:
    return path.suffix.lower() in SOURCE_EXTENSIONS


def language_of(path: Path) -> str:
    return SOURCE_EXTENSIONS.get(path.suffix.lower(), "unknown")


def looks_like_test(path: Path) -> bool:
    text = str(path).lower()
    return any(pattern in text for pattern in TEST_PATTERNS)


def walk_source(root: Path, limit: int = 2000) -> List[Path]:
    """Collect source files under a root, skipping noise directories."""
    found: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for filename in filenames:
            path = Path(dirpath) / filename
            if is_source_file(path):
                found.append(path)
                if len(found) >= limit:
                    return found
    return found
