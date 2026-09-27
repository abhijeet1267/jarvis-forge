"""Coding reports (v1.1).

A report is composed from things JARVIS actually measured — files counted,
searches run, changes applied, tests run. It never asserts that code is correct,
only what was observed.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..logging_setup import get_logger
from .changes import list_backups
from .repo import describe, inspect
from .validation import plan
from .workspace import Workspace

log = get_logger("coding.report")


def build_report(
    workspace: Workspace,
    changes: Optional[List[Dict[str, Any]]] = None,
    validation: Optional[Dict[str, Any]] = None,
    settings=None,
    notes: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Compose a workspace report."""
    changes = changes or []
    info = inspect(workspace)
    test_plan = validation or plan(workspace, settings)

    lines = [f"JARVIS CODING REPORT — {workspace.display}", ""]
    lines.append("Workspace")
    lines.append(f"  root: {workspace.root}")
    lines.append(f"  git repo: {'yes' if info['is_git_repo'] else 'no'}")
    lines.append(f"  source files: {info['file_count']}  test files: {info['test_file_count']}")
    if info["languages"]:
        rendered = ", ".join(f"{k} ({v})" for k, v in list(info["languages"].items())[:5])
        lines.append(f"  languages: {rendered}")
    lines.append("")

    lines.append("Changes")
    if not changes:
        lines.append("  No changes applied in this session.")
    for change in changes:
        status = "applied" if change.get("ok") else "not applied"
        lines.append(f"  {change.get('path', '?')} — {status}")
        if change.get("backup"):
            lines.append(f"    backup: {change['backup']}")
    lines.append("")

    lines.append("Validation")
    lines.append(f"  planned runner: {test_plan.get('runner')} {test_plan.get('args', '')}".rstrip())
    if test_plan.get("allowlisted"):
        lines.append(f"  outcome: {validation.get('outcome', 'not run') if validation else 'not run'}")
        if validation and validation.get("detail"):
            lines.append(f"  detail: {str(validation['detail'])[:160]}")
    else:
        lines.append("  outcome: not run — the runner is not allowlisted (by design).")
        if test_plan.get("note"):
            lines.append(f"  {test_plan['note']}")
    lines.append("")

    backups = list_backups(workspace)
    lines.append("Backups")
    lines.append(f"  {len(backups)} backup file(s)" if backups else "  none")

    if notes:
        lines.append("")
        lines.append("Notes")
        lines.extend(f"  - {note}" for note in notes)

    return {
        "text": "\n".join(lines),
        "workspace": workspace.display,
        "inspection": info,
        "changes": changes,
        "validation": test_plan,
        "backups": backups,
    }
