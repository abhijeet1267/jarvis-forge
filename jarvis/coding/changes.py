"""Proposed and applied code changes (v1.1).

The plan's workflow is ``Inspect → Plan → Permission → Modify → Test → Review →
Report → Optional commit``, so proposing is a separate step from applying, and
applying is gated by the permission engine (the tool is risk tier 2 and requires
confirmation).

What this module will not do:

* **delete files** — there is no delete path here at all, so the repository
  cannot be removed by the coding workflow
* **write outside the workspace** — every path goes through ``Workspace.contain``
* **guess at a replacement** — an edit that does not match exactly is refused,
  never silently approximated
"""

from __future__ import annotations

import difflib
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core.models import utcnow
from ..logging_setup import get_logger
from .workspace import Workspace

log = get_logger("coding.changes")

#: Where backups of replaced files are kept.
BACKUP_DIR = ".jarvis_backups"


@dataclass
class ProposedChange:
    """A change that has been computed but not written."""

    path: Path
    relative: str
    old: str
    new: str
    occurrences: int = 1
    diff: str = ""
    applies: bool = True
    reason: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "path": self.relative,
            "old": self.old,
            "new": self.new,
            "occurrences": self.occurrences,
            "applies": self.applies,
            "reason": self.reason,
            "diff": self.diff,
        }


def _read(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _backup(workspace: Workspace, path: Path) -> Optional[Path]:
    """Copy a file into the workspace's backup directory before changing it."""
    target = workspace.contain(str(path))
    if target is None:
        return None

    backups = workspace.contain(BACKUP_DIR)
    if backups is None:
        # The backup dir is inside the workspace, so contain() allows it.
        backups = workspace.root / BACKUP_DIR
    backups.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    destination = backups / f"{path.name}.{stamp}.bak"
    try:
        destination.write_text(_read(target) or "", encoding="utf-8")
    except OSError:
        return None
    return destination


def propose(
    workspace: Workspace,
    relative_path: str,
    old: str,
    new: str,
) -> ProposedChange:
    """Compute what a change would do, without writing anything."""
    target = workspace.contain(relative_path)
    change = ProposedChange(path=target or Path(relative_path), relative=relative_path, old=old, new=new)

    if target is None:
        change.applies = False
        change.reason = "That path is outside the workspace or protected."
        return change
    if not target.is_file():
        change.applies = False
        change.reason = f"{relative_path} is not a file in this workspace."
        return change
    if not old:
        change.applies = False
        change.reason = "I need the exact text to replace."
        return change

    source = _read(target)
    if source is None:
        change.applies = False
        change.reason = f"I could not read {relative_path}."
        return change

    change.occurrences = source.count(old)
    if change.occurrences == 0:
        change.applies = False
        change.reason = (
            f"I could not find that text in {relative_path}. I will not guess at a "
            "similar-looking replacement."
        )
        return change

    updated = source.replace(old, new)
    change.diff = "\n".join(
        difflib.unified_diff(
            source.splitlines(),
            updated.splitlines(),
            fromfile=f"a/{relative_path}",
            tofile=f"b/{relative_path}",
            lineterm="",
            n=2,
        )
    )
    return change


def apply(workspace: Workspace, change: ProposedChange) -> Dict[str, Any]:
    """Write a proposed change. Callers must have confirmed first."""
    if not change.applies:
        return {"ok": False, "error": change.reason}

    target = workspace.contain(change.relative)
    if target is None:
        return {"ok": False, "error": "That path is outside the workspace."}

    source = _read(target)
    if source is None:
        return {"ok": False, "error": "I could not read that file."}
    if change.old not in source:
        return {"ok": False, "error": "The text to replace is no longer there; nothing was written."}

    backup = _backup(workspace, target)
    updated = source.replace(change.old, change.new)
    try:
        target.write_text(updated, encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": f"could not write: {exc}"}

    log.info(
        "code change applied",
        extra_fields={"workspace": workspace.display, "path": change.relative},
    )
    return {
        "ok": True,
        "path": change.relative,
        "backup": str(backup) if backup else None,
        "replacements": source.count(change.old),
    }


def list_backups(workspace: Workspace) -> List[str]:
    """Backups of files changed in this workspace."""
    directory = workspace.root / BACKUP_DIR
    if not directory.is_dir():
        return []
    return sorted(p.name for p in directory.iterdir() if p.is_file())
