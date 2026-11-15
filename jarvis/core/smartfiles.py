"""Smart Files: look inside a folder and report what is actually there.

This module only *reads*. It answers three questions a person asks before they
change anything:

* which files are byte-identical duplicates (so space can be reclaimed safely),
* which files are large (so the obvious wins are visible first),
* what an organisation-by-type move would look like.

Every mutating action is expressed as ParallelWorld operations, so it is
previewed and approved through the one write path the rest of the system uses.
There is deliberately no rename/delete here.

Bounds that keep this from being a disk-scanning tool by accident:

* A depth limit and a file-count cap, so pointing it at `/` cannot run forever.
* Symlinked directories are not followed: a link is not a subtree the person
  selected, and following one is how a scoped scan becomes an unbounded one.
* Nothing is hashed until its size collides with another file, because the
  common case (no duplicates) then costs one stat per file, not one read.
"""

from __future__ import annotations

import hashlib
import os
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..logging_setup import get_logger

log = get_logger("core.smartfiles")

#: How deep a scan goes. Deep enough for a real project, shallow enough that a
#: mistake cannot walk an entire home directory.
MAX_DEPTH = 6

#: How many files a scan will consider. Beyond this the answer is "narrow the
#: folder", which is honest, rather than a number computed from a partial walk.
MAX_FILES = 20000

#: Only files at least this large are hashed for duplicate detection. Two empty
#: files are technically duplicates; they are not worth reporting.
MIN_DUPLICATE_BYTES = 1024


@dataclass
class FileEntry:
    path: str
    size: int
    name: str

    def to_dict(self) -> Dict[str, Any]:
        return {"path": self.path, "size": self.size, "name": self.name}


def _walk(root: Path, *, max_depth: int = MAX_DEPTH,
          max_files: int = MAX_FILES) -> tuple[List[FileEntry], bool]:
    """Every regular file under `root`, and whether the cap was hit."""
    files: List[FileEntry] = []
    truncated = False

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        depth = len(Path(dirpath).relative_to(root).parts)
        if depth >= max_depth:
            dirnames[:] = []          # do not descend further
        # A symlinked directory is skipped rather than followed.
        dirnames[:] = [d for d in dirnames
                       if not os.path.islink(os.path.join(dirpath, d))]

        for name in filenames:
            full = os.path.join(dirpath, name)
            try:
                if os.path.islink(full) or not os.path.isfile(full):
                    continue
                size = os.path.getsize(full)
            except OSError:
                continue
            files.append(FileEntry(path=full, size=size, name=name))
            if len(files) >= max_files:
                truncated = True
                break
        if truncated:
            break

    return files, truncated


def _digest(path: str) -> Optional[str]:
    """The file's sha256, or None when it cannot be read.

    Unreadable is not "not a duplicate": an unread file is reported as skipped
    rather than silently omitted, so a person is not told their folder is clean
    when part of it was never looked at.
    """
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def scan(root_path: str, *, max_depth: int = MAX_DEPTH,
         max_files: int = MAX_FILES) -> Dict[str, Any]:
    """Report duplicates and large files under a folder. Read-only."""
    root = Path(root_path).expanduser()
    if not root.is_dir():
        raise ValueError(f"{root} is not a directory")

    files, truncated = _walk(root, max_depth=max_depth, max_files=max_files)

    # Group by size first: only a size collision can be a duplicate, so the
    # common case costs one stat per file instead of one full read.
    by_size: Dict[int, List[FileEntry]] = defaultdict(list)
    for entry in files:
        if entry.size >= MIN_DUPLICATE_BYTES:
            by_size[entry.size].append(entry)

    duplicates: List[Dict[str, Any]] = []
    unreadable: List[str] = []
    reclaimable = 0

    for size, candidates in by_size.items():
        if len(candidates) < 2:
            continue
        by_hash: Dict[str, List[FileEntry]] = defaultdict(list)
        for entry in candidates:
            digest = _digest(entry.path)
            if digest is None:
                unreadable.append(entry.path)
                continue
            by_hash[digest].append(entry)

        for digest, group in by_hash.items():
            if len(group) < 2:
                continue
            group.sort(key=lambda e: e.path)
            kept = group[0]
            others = group[1:]
            # Everything beyond the first copy is reclaimable space.
            reclaimable += size * len(others)
            duplicates.append({
                "sha256": digest,
                "size": size,
                "keep": kept.path,
                "copies": [e.path for e in others],
                "wasted_bytes": size * len(others),
            })

    duplicates.sort(key=lambda d: d["wasted_bytes"], reverse=True)

    largest = sorted(files, key=lambda e: e.size, reverse=True)[:10]

    return {
        "root": str(root),
        "file_count": len(files),
        "truncated": truncated,
        "duplicate_groups": duplicates,
        "reclaimable_bytes": reclaimable,
        "largest": [e.to_dict() for e in largest],
        "unreadable": unreadable,
        "note": ("This only reads. Removing a duplicate is a file operation that "
                 "gets previewed and approved like any other change."
                 if duplicates else
                 "No byte-identical duplicates were found above "
                 f"{MIN_DUPLICATE_BYTES} bytes."),
    }


#: Where a file goes when organising by type. A small, predictable set -- the
#: point is a tidy folder, not a taxonomy.
_CATEGORIES = {
    "images": {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".svg", ".tiff"},
    "documents": {".pdf", ".doc", ".docx", ".txt", ".md", ".rtf", ".pages"},
    "spreadsheets": {".csv", ".xlsx", ".xls", ".numbers"},
    "audio": {".mp3", ".wav", ".m4a", ".flac", ".aac"},
    "video": {".mp4", ".mov", ".mkv", ".webm", ".avi"},
    "archives": {".zip", ".tar", ".gz", ".7z", ".rar"},
    "code": {".py", ".js", ".ts", ".swift", ".kt", ".go", ".rs", ".java", ".sh"},
}


def category_for(name: str) -> str:
    suffix = Path(name).suffix.lower()
    for category, suffixes in _CATEGORIES.items():
        if suffix in suffixes:
            return category
    return "other"


def organize_plan(root_path: str, *, by: str = "type") -> Dict[str, Any]:
    """Propose moving loose files into per-category subfolders.

    Returns ParallelWorld operations, not actions: the caller previews and
    approves them. Only top-level files are considered, so an organise pass
    cannot rearrange a project's internal structure by surprise.
    """
    root = Path(root_path).expanduser()
    if not root.is_dir():
        raise ValueError(f"{root} is not a directory")
    if by != "type":
        raise ValueError(f"organising by {by!r} is not supported yet; 'type' is")

    operations: List[Dict[str, Any]] = []
    for entry in sorted(root.iterdir()):
        if entry.is_dir() or entry.is_symlink() or not entry.is_file():
            continue
        category = category_for(entry.name)
        if category == "other":
            continue
        operations.append({
            "op": "move",
            "path": str(entry),
            "destination": str(root / category / entry.name),
            "reason": f"{category} file",
        })

    return {
        "root": str(root),
        "operations": operations,
        "summary": (f"{len(operations)} file(s) would move into folders by type."
                    if operations else "Nothing at this level would move."),
        "applicable": bool(operations),
    }