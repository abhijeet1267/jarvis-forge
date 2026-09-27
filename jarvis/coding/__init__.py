"""Coding Agent support (v1.1).

Works inside controlled project workspaces, following the plan's workflow:

    Request → Inspect → Plan → Permission → Modify → Test → Review → Report →
    Optional commit

The safety rules are structural:

* **Every path goes through the workspace boundary.** There is no code path that
  touches a file outside the workspace.
* **No delete path exists here.** Files are replaced with a backup, never
  removed, so the repository cannot be deleted by this workflow.
* **Tests run through the existing allowlisted command tool.** This package has
  no execution path of its own, and will not add an interpreter to the
  allowlist — that would make the allowlist decorative.
* **No push.** Committing is possible when explicitly authorised; pushing is not
  implemented at all.
"""

from .changes import ProposedChange, apply, list_backups, propose
from .report import build_report
from .repo import describe, inspect, search
from .validation import describe_validation, plan, run_tests
from .workspace import (
    SKIP_DIRS,
    SOURCE_EXTENSIONS,
    Workspace,
    WorkspaceRegistry,
    is_source_file,
    language_of,
    looks_like_test,
    walk_source,
)

__all__ = [
    "Workspace",
    "WorkspaceRegistry",
    "SKIP_DIRS",
    "SOURCE_EXTENSIONS",
    "is_source_file",
    "language_of",
    "looks_like_test",
    "walk_source",
    "inspect",
    "search",
    "describe",
    "propose",
    "apply",
    "ProposedChange",
    "list_backups",
    "plan",
    "run_tests",
    "describe_validation",
    "build_report",
]
