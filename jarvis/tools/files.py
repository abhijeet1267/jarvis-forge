"""File operation tools (section 12).

Safety rules baked in:

* Deletion goes to the OS Trash by default; permanent deletion is a separate,
  always-confirmed path.
* System/protected roots are refused outright.
* Recursive/broad deletes are never inferred from a vague request.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core.models import RiskLevel, ToolSpec
from ..logging_setup import get_logger
from .base import ToolContext, fail, human_size, ok, run_command, unverified

log = get_logger("tools.files")

# Directories JARVIS must never touch live in one place so the permission
# engine and the tools always agree.
from ..security.paths import PROTECTED_ROOTS, PROTECTED_SUBPATHS, protection_reason

MAX_SEARCH_RESULTS = 50

#: A search that matches nothing must cost about as much as one that matches
#: something. Before this, the walk only stopped early on a find, so an unmatched
#: query descended into ~/Library -- macOS' largest tree -- and took 6.9 s against
#: a home directory, where a matching query took 0.36 s.
PRUNED_DIRS = {
    "Library", "Applications", "node_modules", "__pycache__", "dist", "build",
    "venv", ".venv", ".cache", "Caches", ".npm", ".gradle", ".mypy_cache",
    ".pytest_cache", ".Trash",
}
SEARCH_SCAN_CAP = 60_000
SEARCH_TIME_BUDGET_S = 2.5
MAX_READ_BYTES = 200_000

#: How much of a read file goes into the reply -- which is also what gets
#: remembered, so an uncapped read would copy the whole file into memory.
MAX_READ_EXCERPT = 1200


def _resolve(path: str) -> Path:
    return Path(os.path.expanduser(path)).resolve()


def _guard(path: Path) -> Optional[str]:
    """Return a refusal reason if ``path`` is protected, else None."""
    return protection_reason(str(path))


def _trash_dir() -> Path:
    """The OS trash directory to use for reversible deletes."""
    return Path.home() / ".Trash"


def _move_to_trash(target: Path) -> Optional[str]:
    """Move a path to the trash. Returns an error string on failure."""
    trash = _trash_dir()
    try:
        trash.mkdir(parents=True, exist_ok=True)
        destination = trash / target.name
        # Avoid clobbering an existing item with the same name.
        if destination.exists():
            stem, suffix = target.stem, target.suffix
            counter = 1
            while destination.exists():
                destination = trash / f"{stem} {counter}{suffix}"
                counter += 1
        shutil.move(str(target), str(destination))
        return None
    except OSError as exc:
        return str(exc)


def _sha256(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Search & read
# ---------------------------------------------------------------------------
async def search_files(
    ctx: ToolContext,
    query: str = "",
    directory: str = "~",
    limit: int = 10,
    sort: str = "relevance",
) -> Any:
    """Find files by name.

    Results are ranked rather than returned in filesystem order, because
    "find my latest report" is only answered correctly if the newest match
    comes first. ``sort`` is one of ``relevance`` (default) or ``recent``.
    """
    if not query:
        return fail("search_files", "I need something to search for.", device=ctx.device_id)

    base = _resolve(directory)
    if not base.exists():
        return fail("search_files", f"Directory not found: {base}", device=ctx.device_id)

    limit = max(1, min(int(limit), MAX_SEARCH_RESULTS))
    candidates: List[Path] = []

    # Preferred path: Spotlight index (fast, no full-disk walk).
    if ctx.platform == "macos" and shutil.which("mdfind"):
        code, out, _err = await run_command(
            ["mdfind", "-onlyin", str(base), f"kMDItemFSName == '*{query}*'cd"],
            timeout=15.0,
        )
        if code == 0 and out:
            candidates = [Path(line) for line in out.splitlines() if Path(line).exists()]

    # Fallback: bounded filesystem walk. Collect a wider pool so ranking has
    # something to work with, then trim.
    scanned = 0
    skipped: set = set()
    truncated = False
    if not candidates:
        needle = query.lower()
        pool_limit = max(limit * 6, 60)
        deadline = time.perf_counter() + SEARCH_TIME_BUDGET_S
        for root, dirs, files in os.walk(base):
            kept = []
            for name in dirs:
                if name.startswith(".") or name in PRUNED_DIRS:
                    skipped.add(name)
                else:
                    kept.append(name)
            dirs[:] = kept
            for name in files:
                scanned += 1
                if needle in name.lower():
                    candidates.append(Path(root) / name)
                if len(candidates) >= pool_limit or scanned >= SEARCH_SCAN_CAP:
                    break
            if len(candidates) >= pool_limit or scanned >= SEARCH_SCAN_CAP:
                truncated = scanned >= SEARCH_SCAN_CAP and len(candidates) < pool_limit
                break
            if time.perf_counter() > deadline:
                truncated = True
                break

    # Spotlight answers from an index the walk's dot-directory pruning never
    # touches, so a home-rooted search could still name a file inside ~/.ssh.
    # Both sources are filtered here, in one place, rather than each backend
    # being trusted to remember the boundary.
    candidates = [path for path in candidates if not protection_reason(str(path))]

    if not candidates:
        # "nothing matched" and "I stopped looking" are different claims, and only
        # the first one is safe to make from a bounded walk.
        sentence = (
            f"I searched {scanned} entries under {base} and stopped early, so this is "
            "not exhaustive: no file matching " + repr(query) + " turned up."
            if truncated
            else f"I could not find any file matching {query!r} under {base}."
        )
        return ok(
            "search_files",
            sentence,
            device=ctx.device_id,
            query=query,
            results=[],
            scanned=scanned,
            truncated=truncated,
            skipped=sorted(skipped),
        )

    results = _rank(candidates, query, sort=sort)[:limit]

    newest = results[0]
    return ok(
        "search_files",
        (
            f"I found {len(candidates)} file(s) matching {query!r}. "
            f"Most recent: {newest['name']} ({newest['modified_human']})."
            if len(candidates) > 1
            else f"I found {newest['name']}."
        ),
        device=ctx.device_id,
        query=query,
        sort=sort,
        results=results,
        scanned=scanned,
        truncated=truncated,
        skipped=sorted(skipped),
    )


async def find_and_open(ctx: ToolContext, query: str = "", directory: str = "~") -> Any:
    """Find the best-matching file and hand it to the system, in one sentence.

    "find my resume and open it" was two capabilities the build already had and no route
    between them. The finding is verified the way `search_files` verifies; the opening is
    not, because `open` exiting 0 only proves LaunchServices accepted the request -- no
    channel here says a window took the file. So the answer is UNKNOWN about the open,
    and says which half is which.
    """
    if not str(query).strip():
        return fail("find_and_open", "Which file should I look for?", device=ctx.device_id)

    found = await search_files(ctx, query=query, directory=directory)
    if not found.ok:
        return fail("find_and_open", found.error, device=ctx.device_id)
    results = found.output.get("results") or []
    if not results:
        return fail(
            "find_and_open",
            f"I found nothing matching {query!r}, so there is nothing to open.",
            device=ctx.device_id, query=query,
        )

    top = results[0]
    path = str(top.get("path") or "")
    if not path or not Path(path).is_file():
        return fail("find_and_open",
                    f"The best match, {top.get('name') or path!r}, is not a file I can open.",
                    device=ctx.device_id)

    code, _out, err = await run_command(["open", path])
    if code != 0:
        return fail("find_and_open", f"Could not open {Path(path).name}: "
                    f"{err or 'the system refused'}", device=ctx.device_id, path=path)

    runner_up = results[1]["name"] if len(results) > 1 else ""
    return unverified(
        "find_and_open",
        f"Asked your Mac to open {Path(path).name}"
        + (f" (other match: {runner_up})" if runner_up else "")
        + ". I could not confirm a window took it.",
        device=ctx.device_id, path=path, name=Path(path).name,
        matches=[r.get("path") for r in results[:5]],
    )


def _rank(paths: List[Path], query: str, sort: str = "relevance") -> List[Dict[str, Any]]:
    """Order matches so the most useful result is first.

    Relevance combines how well the name matches with how recently the file was
    touched, so an exact recent match beats an old fuzzy one.
    """
    needle = query.lower()
    scored: List[tuple[float, Dict[str, Any]]] = []
    now = time.time()

    for path in paths:
        info = _file_info(path)
        name = path.name.lower()
        stem = path.stem.lower()

        if stem == needle:
            name_score = 1.0
        elif name == needle:
            name_score = 0.98
        elif name.startswith(needle):
            name_score = 0.85
        elif needle in stem:
            name_score = 0.7
        else:
            name_score = 0.5

        # Recency: full marks within a day, decaying over ~90 days.
        age_days = max(0.0, (now - (info.get("modified") or 0)) / 86400.0)
        recency = 1.0 / (1.0 + age_days / 30.0)

        if sort == "recent":
            score = (info.get("modified") or 0.0)
        else:
            score = name_score * 0.65 + recency * 0.35

        scored.append((score, info))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [info for _score, info in scored]


async def list_directory(ctx: ToolContext, path: str = "~") -> Any:
    """List the immediate contents of a directory."""
    target = _resolve(path)
    if not target.is_dir():
        return fail("list_directory", f"Not a directory: {target}", device=ctx.device_id)

    entries = []
    try:
        for child in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
            entries.append(
                {
                    "name": child.name,
                    "path": str(child),
                    "type": "dir" if child.is_dir() else "file",
                    "size": child.stat().st_size if child.is_file() else None,
                }
            )
    except PermissionError:
        return fail("list_directory", f"Permission denied reading {target}", device=ctx.device_id)

    return ok(
        "list_directory",
        f"{target} contains {len(entries)} item(s).",
        device=ctx.device_id,
        path=str(target),
        entries=entries[:100],
    )


async def read_file(ctx: ToolContext, path: str = "") -> Any:
    """Read a small text file (size-capped)."""
    target = _resolve(path)
    guard = _guard(target)
    if guard:
        return fail("read_file", guard, device=ctx.device_id)
    if not target.is_file():
        return fail("read_file", f"File not found: {target}", device=ctx.device_id)
    if target.stat().st_size > MAX_READ_BYTES:
        return fail(
            "read_file",
            f"File is too large to read inline ({human_size(target.stat().st_size)}).",
            device=ctx.device_id,
        )
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return fail("read_file", f"Could not read file: {exc}", device=ctx.device_id)

    excerpt = content.strip()
    truncated = False
    if len(excerpt) > MAX_READ_EXCERPT:
        excerpt = excerpt[:MAX_READ_EXCERPT].rstrip()
        truncated = True
    shown = f":\n{excerpt}" + ("\n… (rest withheld; the file is longer than shown)"
                              if truncated else "")
    return ok(
        "read_file",
        f"Read {target.name}{shown}",
        device=ctx.device_id,
        verified=True,
        path=str(target),
        content=content,
    )


# ---------------------------------------------------------------------------
# Mutation
# ---------------------------------------------------------------------------
async def write_file(
    ctx: ToolContext,
    path: str = "",
    content: str = "",
    mode: str = "overwrite",
) -> Any:
    """Create a file or write into one, then read it back to prove the effect.

    Added in Phase 11: the catalogue could copy, move and delete files but had no
    way to create or modify one, so no "real computer" scenario that starts from
    nothing could be executed at all.

    The result is keyed on what the filesystem says *after* the write, not on the
    write call returning. A successful write that cannot be read back is reported
    UNVERIFIED rather than as a success -- the distinction Phase 9 found missing
    everywhere else in this system.
    """
    if not path:
        return fail("write_file", "Which file should I write to?", device=ctx.device_id)
    if mode not in ("overwrite", "append"):
        return fail(
            "write_file",
            f"Unknown mode {mode!r}. Use 'overwrite' or 'append'.",
            device=ctx.device_id,
        )

    target = _resolve(path)
    guard = _guard(target)
    if guard:
        return fail("write_file", guard, device=ctx.device_id)
    if target.is_dir():
        return fail("write_file", f"{target} is a folder, not a file.", device=ctx.device_id)

    existed_before = target.exists()
    try:
        before = target.read_bytes() if (mode == "append" and existed_before) else b""
    except OSError as exc:
        return fail("write_file", f"Could not read {target} to append to it: {exc}",
                    device=ctx.device_id)

    payload = (before + content.encode("utf-8")) if mode == "append" else content.encode("utf-8")

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    except OSError as exc:
        return fail("write_file", f"Write failed: {exc}", device=ctx.device_id)

    # Independent check: ask the filesystem what is there now.
    if not target.exists():
        return fail(
            "write_file",
            f"The write to {target} reported no error but the file is not there.",
            device=ctx.device_id,
        )
    try:
        written = target.read_bytes()
    except OSError as exc:
        return ok(
            "write_file",
            f"I wrote {target} but could not read it back to check it ({exc}).",
            device=ctx.device_id,
            path=str(target),
            verified=False,
            size=len(payload),
        )

    matches = written == payload
    verb = "Appended to" if mode == "append" else ("Updated" if existed_before else "Created")
    if not matches:
        return ok(
            "write_file",
            f"{verb} {target} but the contents on disk do not match what I wrote, "
            "so I cannot report this as done.",
            device=ctx.device_id,
            path=str(target),
            verified=False,
            expected_bytes=len(payload),
            actual_bytes=len(written),
        )

    return ok(
        "write_file",
        f"{verb} {target.name} ({len(written)} bytes), read it back and it matches.",
        device=ctx.device_id,
        path=str(target),
        verified=True,
        created=not existed_before,
        mode=mode,
        size=len(written),
    )


async def copy_file(ctx: ToolContext, source: str = "", destination: str = "") -> Any:
    """Copy a file or directory to a new location."""
    src, dst = _resolve(source), _resolve(destination)
    if not src.exists():
        return fail("copy_file", f"Source not found: {src}", device=ctx.device_id)
    guard = _guard(src)
    if guard:
        return fail("copy_file", guard, device=ctx.device_id)

    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)
    except OSError as exc:
        return fail("copy_file", f"Copy failed: {exc}", device=ctx.device_id)

    return ok(
        "copy_file",
        f"Copied {src.name} to {dst}.",
        device=ctx.device_id,
        source=str(src),
        destination=str(dst),
        verified=dst.exists(),
    )


async def move_file(ctx: ToolContext, source: str = "", destination: str = "") -> Any:
    """Move a file or directory."""
    src, dst = _resolve(source), _resolve(destination)
    if not src.exists():
        return fail("move_file", f"Source not found: {src}", device=ctx.device_id)
    guard = _guard(src)
    if guard:
        return fail("move_file", guard, device=ctx.device_id)

    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
    except OSError as exc:
        return fail("move_file", f"Move failed: {exc}", device=ctx.device_id)

    return ok(
        "move_file",
        f"Moved {src.name} to {dst}.",
        device=ctx.device_id,
        source=str(src),
        destination=str(dst),
        verified=(not src.exists() and Path(dst).exists()),
    )


async def delete_file(ctx: ToolContext, path: str = "", permanent: bool = False) -> Any:
    """Delete a file. Moves to Trash unless ``permanent`` is explicitly set."""
    target = _resolve(path)
    if not target.exists():
        return fail("delete_file", f"Nothing to delete at {target}", device=ctx.device_id)

    guard = _guard(target)
    if guard:
        return fail("delete_file", guard, device=ctx.device_id)

    # Guard against a request that would nuke an entire home/top-level folder.
    if target.is_dir() and target.parent in (Path.home(), Path.home().parent):
        return fail(
            "delete_file",
            f"{target} is a top-level folder. I will not delete it without an explicit, "
            "itemised request.",
            device=ctx.device_id,
        )

    if permanent or not ctx.settings.trash_on_delete:
        try:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        except OSError as exc:
            return fail("delete_file", f"Delete failed: {exc}", device=ctx.device_id)
        return ok(
            "delete_file",
            f"Permanently deleted {target.name}.",
            device=ctx.device_id,
            path=str(target),
            permanent=True,
            verified=not target.exists(),
        )

    # Default: move to Trash so the action stays reversible.
    if ctx.platform == "macos":
        # Prefer a direct move into ~/.Trash: no Automation permission needed,
        # unlike driving Finder over Apple events.
        error = _move_to_trash(target)
        if error is None:
            return ok(
                "delete_file",
                f"Moved {target.name} to Trash.",
                device=ctx.device_id,
                path=str(target),
                permanent=False,
                verified=not target.exists(),
            )
        log.info("trash move failed", extra_fields={"error": error})

        # Fallback: ask Finder (requires Automation permission).
        script = f'tell application "Finder" to delete POSIX file "{target}"'
        code, _out, err = await run_command(["osascript", "-e", script], timeout=15.0)
        if code != 0:
            return fail(
                "delete_file",
                f"Could not move to Trash: {error or err}",
                device=ctx.device_id,
            )
        return ok(
            "delete_file",
            f"Moved {target.name} to Trash.",
            device=ctx.device_id,
            path=str(target),
            permanent=False,
            verified=not target.exists(),
        )

    try:
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
    except OSError as exc:
        return fail("delete_file", f"Delete failed: {exc}", device=ctx.device_id)
    return ok(
        "delete_file",
        f"Deleted {target.name}.",
        device=ctx.device_id,
        path=str(target),
        permanent=False,
        verified=not target.exists(),
    )


async def compress_path(ctx: ToolContext, path: str = "", destination: str = "") -> Any:
    """Create a .zip archive from a file or folder."""
    src = _resolve(path)
    if not src.exists():
        return fail("compress_path", f"Nothing to compress at {src}", device=ctx.device_id)

    archive = _resolve(destination) if destination else src.with_suffix(src.suffix + ".zip")
    try:
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            if src.is_dir():
                for file in src.rglob("*"):
                    if file.is_file():
                        zf.write(file, file.relative_to(src.parent))
            else:
                zf.write(src, src.name)
    except OSError as exc:
        return fail("compress_path", f"Compression failed: {exc}", device=ctx.device_id)

    return ok(
        "compress_path",
        f"Compressed {src.name} to {archive.name} ({human_size(archive.stat().st_size)}).",
        device=ctx.device_id,
        source=str(src),
        archive=str(archive),
        verified=archive.exists(),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _file_info(path: Path) -> Dict[str, Any]:
    try:
        stat = path.stat()
        size = stat.st_size
        mtime = stat.st_mtime
    except OSError:
        size, mtime = 0, 0.0
    return {
        "name": path.name,
        "path": str(path),
        "size": size,
        "size_human": human_size(size),
        "modified": mtime,
        "modified_human": _human_age(mtime),
        "extension": path.suffix.lower(),
    }


def _human_age(mtime: float) -> str:
    """Render a modification time as a short relative age."""
    if not mtime:
        return "unknown"
    delta = max(0.0, time.time() - mtime)
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)} min ago"
    if delta < 86400:
        return f"{int(delta // 3600)} h ago"
    days = int(delta // 86400)
    if days == 1:
        return "yesterday"
    if days < 30:
        return f"{days} days ago"
    if days < 365:
        return f"{days // 30} months ago"
    return f"{days // 365} years ago"


SPECS = [
    (
        ToolSpec(
            name="search_files",
            description="Find files by name, ranked by relevance and recency.",
            risk=RiskLevel.READ,
            parameters={"query": "string", "directory": "string", "limit": "int", "sort": "string"},
            required_parameters=["query"],
            tags=["files", "read"],
        ),
        search_files,
    ),
    (
        ToolSpec(
            name="find_and_open",
            description="Find the best-matching file by name and ask the system to open it.",
            risk=RiskLevel.LOW,
            reversible=True,
            parameters={"query": "string", "directory": "string"},
            required_parameters=["query"],
            tags=["files", "open"],
        ),
        find_and_open,
    ),
    (
        ToolSpec(
            name="list_directory",
            description="List the contents of a directory.",
            risk=RiskLevel.READ,
            parameters={"path": "string"},
            required_parameters=["path"],
            tags=["files", "read"],
        ),
        list_directory,
    ),
    (
        ToolSpec(
            name="read_file",
            description="Read a small text file.",
            risk=RiskLevel.READ,
            parameters={"path": "string"},
            required_parameters=["path"],
            tags=["files", "read"],
        ),
        read_file,
    ),
    (
        ToolSpec(
            name="write_file",
            description=(
                "Create a file, or write/append content to one. Reads the file back "
                "afterwards to confirm the contents actually landed."
            ),
            risk=RiskLevel.EXTERNAL,
            requires_confirmation=True,
            reversible=True,
            parameters={
                "path": "string",
                "content": "string",
                "mode": "overwrite | append",
            },
            required_parameters=["path", "content"],
            tags=["files", "write"],
        ),
        write_file,
    ),
    (
        ToolSpec(
            name="copy_file",
            description="Copy a file or folder.",
            risk=RiskLevel.EXTERNAL,
            requires_confirmation=True,
            reversible=True,
            parameters={"source": "string", "destination": "string"},
            required_parameters=["source", "destination"],
            tags=["files", "write"],
        ),
        copy_file,
    ),
    (
        ToolSpec(
            name="move_file",
            description="Move or rename a file or folder.",
            risk=RiskLevel.EXTERNAL,
            requires_confirmation=True,
            reversible=True,
            parameters={"source": "string", "destination": "string"},
            required_parameters=["source", "destination"],
            tags=["files", "write"],
        ),
        move_file,
    ),
    (
        ToolSpec(
            name="delete_file",
            description="Delete a file or folder. Moves to Trash unless permanent=true.",
            risk=RiskLevel.DESTRUCTIVE,
            requires_confirmation=True,
            reversible=True,
            parameters={"path": "string", "permanent": "bool"},
            required_parameters=["path"],
            tags=["files", "destructive"],
        ),
        delete_file,
    ),
    (
        ToolSpec(
            name="compress_path",
            description="Create a zip archive of a file or folder.",
            risk=RiskLevel.LOW,
            parameters={"path": "string", "destination": "string"},
            required_parameters=["path"],
            tags=["files", "write"],
        ),
        compress_path,
    ),
]
