"""Repository inspection (v1.1).

Read-only understanding of a workspace: what is in it, which languages, where
the tests live, and plain-text search across source.

This is *inspection*, not comprehension — JARVIS reports counts, languages and
matches rather than claiming to understand an architecture it has not read.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Optional

from ..logging_setup import get_logger
from .workspace import (
    SOURCE_EXTENSIONS,
    Workspace,
    is_source_file,
    language_of,
    looks_like_test,
    walk_source,
)

log = get_logger("coding.repo")

#: Files that usually explain how to run a project.
ENTRY_HINTS = [
    "README.md", "readme.md", "pyproject.toml", "package.json", "Makefile",
    "setup.py", "Cargo.toml", "go.mod", "pom.xml", "build.gradle",
]

#: Words that suggest what a test runner is.
RUNNER_HINTS = {
    "pytest": ["pytest", "pyproject.toml", "conftest.py"],
    "npm test": ["package.json"],
    "cargo test": ["Cargo.toml"],
    "go test": ["go.mod"],
}


def inspect(workspace: Workspace, limit: int = 2000) -> Dict[str, Any]:
    """A structured description of the workspace."""
    files = walk_source(workspace.root, limit=limit)

    languages = Counter(language_of(f) for f in files)
    tests = [f for f in files if looks_like_test(f)]

    entries = []
    for hint in ENTRY_HINTS:
        candidate = workspace.contain(hint)
        if candidate is not None and candidate.is_file():
            entries.append(workspace.relative(candidate))

    git_dir = workspace.contain(".git")
    package_files = [
        workspace.relative(workspace.contain(hint))
        for hint in ("pyproject.toml", "package.json", "Cargo.toml", "go.mod")
    ]
    package_files = [p for p in package_files if p]

    runners = []
    present = {f.name for f in files} | {p.split("/")[-1] for p in package_files}
    for runner, hints in RUNNER_HINTS.items():
        if any(hint in present for hint in hints):
            runners.append(runner)

    return {
        "workspace": workspace.display,
        "root": str(workspace.root),
        "is_git_repo": git_dir is not None,
        "file_count": len(files),
        "test_file_count": len(tests),
        "languages": dict(languages.most_common()),
        "entry_points": entries,
        "package_files": package_files,
        "likely_test_runners": runners,
        "largest_files": [
            {"path": workspace.relative(f), "lines": _line_count(f)}
            for f in sorted(files, key=_line_count, reverse=True)[:5]
        ],
    }


def _line_count(path) -> int:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return 0


def search(
    workspace: Workspace,
    query: str,
    limit: int = 20,
    case_sensitive: bool = False,
) -> List[Dict[str, Any]]:
    """Plain-text search across source files in the workspace."""
    needle = query if case_sensitive else (query or "").lower()
    if not needle.strip():
        return []

    results: List[Dict[str, Any]] = []
    for path in walk_source(workspace.root):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue

        for number, line in enumerate(lines, start=1):
            haystack = line if case_sensitive else line.lower()
            if needle in haystack:
                results.append(
                    {
                        "path": workspace.relative(path),
                        "line": number,
                        "text": line.strip()[:200],
                        "language": language_of(path),
                    }
                )
                if len(results) >= limit:
                    return results
    return results


def describe(workspace: Workspace) -> str:
    """A one-paragraph summary for humans."""
    info = inspect(workspace)
    languages = ", ".join(f"{name} ({count})" for name, count in list(info["languages"].items())[:4])
    return (
        f"{info['workspace']}: {info['file_count']} source file(s), "
        f"{info['test_file_count']} test file(s). Languages: {languages or 'unknown'}. "
        f"Git repo: {'yes' if info['is_git_repo'] else 'no'}."
    )
