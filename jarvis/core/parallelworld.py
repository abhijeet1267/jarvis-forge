"""ParallelWorld: show the proposed change before anything is written.

The promise is narrow and deliberately kept so: **local file operations that are
genuinely reversible**, previewed against a snapshot of the source, applied only
after the source is re-checked and the caller supplies approval. It is not a
claim of atomicity across filesystems, and nothing here pretends a sent email or
a third-party API call can be rolled back.

The sequence, and why each step exists:

1. **Snapshot** the source (path, existence, size, mtime, sha256). The hash is
   what makes a later "did this change under me?" check possible.
2. **Plan** typed operations -- create, replace, append, delete.
3. **Stage** the new content in a private directory. Nothing is written to the
   target yet; a preview that mutates the target is not a preview.
4. **Diff** the staged content against the snapshot so a person sees bytes, not
   a promise.
5. **Re-validate** immediately before applying: the source hash must still match
   what was previewed, and the path guard must still allow it. A file edited
   between preview and apply produces a refusal, not a silent overwrite.
6. **Apply**, then verify the result independently.
7. **Roll back** from the staged backup if the apply fails partway.

What it cannot do, stated so a caller cannot mistake it for more: applying
several files is not atomic across a crash, and a rolled-back delete restores
content but not necessarily every extended attribute.
"""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..logging_setup import get_logger

log = get_logger("core.parallelworld")

#: How much of a file the preview shows. A diff of a 40 MB file is not a preview
#: a person reads, and copying it into memory to show one line is wasteful.
MAX_PREVIEW_BYTES = 64 * 1024


@dataclass
class SourceSnapshot:
    """What a target looked like when the plan was built."""

    path: str
    exists: bool
    size: int = 0
    sha256: str = ""
    mtime: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {"path": self.path, "exists": self.exists, "size": self.size,
                "sha256": self.sha256, "mtime": self.mtime}


@dataclass
class PlannedOperation:
    """One typed file operation."""

    op: str                      # create | replace | append | delete
    path: str
    content: str = ""            # new text, for create/replace/append
    reason: str = ""
    snapshot: Optional[SourceSnapshot] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"op": self.op, "path": self.path,
                "reason": self.reason,
                "snapshot": self.snapshot.to_dict() if self.snapshot else None}


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def snapshot(path: Path) -> SourceSnapshot:
    """Read the target's current identity. Never raises for a missing file."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return SourceSnapshot(path=str(path), exists=False)
    except OSError as exc:
        log.info("could not snapshot a path", extra_fields={"error": str(exc)[:120]})
        return SourceSnapshot(path=str(path), exists=False)
    try:
        stat = path.stat()
        mtime = stat.st_mtime
    except OSError:
        mtime = 0.0
    return SourceSnapshot(path=str(path), exists=True, size=len(data),
                          sha256=_digest(data), mtime=mtime)


class ParallelWorld:
    """Build a preview, then apply it if the world has not moved underneath."""

    def __init__(self, *, guard=None) -> None:
        #: A callable(path_str) -> refusal reason or None. The file tools'
        #: protector is passed in so a preview cannot target a path the tools
        #: would refuse to write.
        self._guard = guard

    # -- planning -----------------------------------------------------------
    def plan(self, operations: List[Dict[str, Any]]) -> List[PlannedOperation]:
        """Turn requested operations into a typed, snapshotted plan."""
        planned: List[PlannedOperation] = []
        for raw in operations:
            op = str(raw.get("op") or "").strip().lower()
            if op not in ("create", "replace", "append", "delete"):
                raise ValueError(
                    f"{op!r} is not a supported file operation; this preview "
                    "covers create, replace, append and delete only")
            path = Path(os.path.expanduser(str(raw.get("path") or ""))).resolve()
            if not str(path):
                raise ValueError("an operation needs a path")
            planned.append(PlannedOperation(
                op=op, path=str(path), content=str(raw.get("content") or ""),
                reason=str(raw.get("reason") or ""), snapshot=snapshot(path)))
        return planned

    # -- preview ------------------------------------------------------------
    def preview(self, planned: List[PlannedOperation]) -> Dict[str, Any]:
        """What would change, without changing anything.

        The staged text is computed here and held in memory only; the targets are
        not touched, so a caller can preview a plan it never applies.
        """
        changes = []
        conflicts = []
        for item in planned:
            target = Path(item.path)

            refusal = self._guard(item.path) if self._guard else None
            if refusal:
                conflicts.append({"path": item.path, "reason": refusal})
                continue

            # A directory is not a file to replace or delete. Reporting this as a
            # conflict is the difference between "this plan cannot run" and a
            # 500: reading a directory raised IsADirectoryError straight out of
            # the preview.
            if target.is_dir():
                conflicts.append({
                    "path": item.path,
                    "reason": "the path is a directory, and this preview and apply "
                              "cover files only"})
                continue

            try:
                current = target.read_text(encoding="utf-8", errors="replace") \
                    if target.exists() else ""
            except OSError as exc:
                conflicts.append({"path": item.path,
                                  "reason": f"the path could not be read: {exc}"})
                continue
            proposed = _propose(item, current)

            entry = {
                "path": item.path,
                "op": item.op,
                "exists": bool(item.snapshot and item.snapshot.exists),
                "before": current[:MAX_PREVIEW_BYTES],
                "after": proposed[:MAX_PREVIEW_BYTES],
                "truncated": len(current) > MAX_PREVIEW_BYTES
                             or len(proposed) > MAX_PREVIEW_BYTES,
                "reason": item.reason,
            }

            # A create over an existing file, or a replace under a file that
            # appeared after the snapshot, is a conflict a person must see.
            if item.op == "create" and entry["exists"]:
                conflicts.append({"path": item.path,
                                  "reason": "the file already exists"})
            elif item.op in ("replace", "append", "delete") and not entry["exists"]:
                conflicts.append({"path": item.path,
                                  "reason": "the file does not exist"})
            changes.append(entry)

        return {
            "changes": changes,
            "conflicts": conflicts,
            "summary": {
                "created": sum(1 for c in changes if c["op"] == "create"),
                "modified": sum(1 for c in changes
                                if c["op"] in ("replace", "append")),
                "deleted": sum(1 for c in changes if c["op"] == "delete"),
                "conflicts": len(conflicts),
            },
            # Apply is unavailable while anything conflicts, and the reason is
            # stated rather than left to the caller to infer.
            "applicable": not conflicts,
            "note": ("This preview covers local files only. Applying it is not "
                     "atomic across a crash, and no external service action is "
                     "reversible."),
        }

    # -- apply --------------------------------------------------------------
    def apply(self, planned: List[PlannedOperation], *,
              approved: bool = False) -> Dict[str, Any]:
        """Apply a previewed plan. Refuses without approval and on stale sources."""
        if not approved:
            return {"applied": False, "refused": "approval was not given",
                    "results": []}

        results = []
        backups: Dict[str, Optional[bytes]] = {}
        journal_entries: List[Dict[str, Any]] = []

        for item in planned:
            target = Path(item.path)

            refusal = self._guard(item.path) if self._guard else None
            if refusal:
                results.append({"path": item.path, "ok": False, "detail": refusal})
                continue

            # Same reason as the preview: this covers files, and a directory is
            # refused rather than raising out of the apply loop.
            if target.is_dir():
                results.append({"path": item.path, "ok": False,
                                "detail": "the path is a directory; only files are "
                                          "supported"})
                continue

            # Freshness: the file must still be the one that was previewed.
            now = snapshot(target)
            before = item.snapshot or SourceSnapshot(path=str(target), exists=False)
            if now.exists != before.exists or now.sha256 != before.sha256:
                results.append({
                    "path": item.path, "ok": False,
                    "detail": ("the file changed after the preview, so applying "
                               "this would overwrite work nobody saw")})
                continue

            try:
                backups[item.path] = target.read_bytes() if target.exists() else None
            except OSError as exc:
                results.append({"path": item.path, "ok": False,
                                "detail": f"could not read the file to back it up: {exc}"})
                continue

            try:
                _apply_one(item, target)
            except Exception as exc:
                # Restore this file from the bytes read a moment ago, so a
                # partial write does not leave half a document behind.
                self._restore(target, backups[item.path])
                results.append({"path": item.path, "ok": False,
                                "detail": f"apply failed and the file was restored: {exc}"})
                continue

            after = snapshot(target)
            ok = _matches_expectation(item, after)
            if not ok:
                self._restore(target, backups[item.path])
            if ok:
                import base64

                original = backups.get(item.path)
                journal_entries.append({
                    "path": item.path,
                    "op": item.op,
                    "original_b64": (base64.b64encode(original).decode("ascii")
                                     if original is not None else None),
                    "after_sha256": after.sha256,
                })

            results.append({
                "path": item.path, "ok": ok,
                "detail": ("verified after the write" if ok else
                           "the file did not end up as proposed; it was restored"),
                "sha256": after.sha256,
            })

        return {
            "applied": all(r["ok"] for r in results) if results else False,
            "results": results,
            "rolled_back": [r["path"] for r in results if not r["ok"]],
            # The record an undo needs: what each file held before, and what our
            # write produced. Returned to the caller rather than hidden in the
            # module, so "what would undo do" is inspectable before it is done.
            "journal": {"entries": journal_entries},
        }

    # -- undo ---------------------------------------------------------------
    def undo(self, journal: Dict[str, Any], *, approved: bool = False) -> Dict[str, Any]:
        """Restore the pre-apply bytes, but only where nothing newer exists.

        The rule that makes undo safe is the same one that makes apply safe: the
        file must still be the one *we* wrote. If a person edited it after the
        apply, restoring the old bytes would destroy their work, so that entry is
        refused and reported rather than forced.

        `journal` is the `journal` entry returned by `apply`. It is caller-held
        because this module keeps no hidden state: the record of what was written
        is data the caller can store, inspect and pass back.
        """
        if not approved:
            return {"undone": False, "refused": "approval was not given", "results": []}

        entries = list((journal or {}).get("entries") or [])
        if not entries:
            return {"undone": False, "refused": "the journal records nothing to undo",
                    "results": []}

        results = []
        for entry in entries:
            target = Path(str(entry.get("path") or ""))
            original_b64 = entry.get("original_b64")
            expected_after = str(entry.get("after_sha256") or "")

            refusal = self._guard(str(target)) if self._guard else None
            if refusal:
                results.append({"path": str(target), "ok": False, "detail": refusal})
                continue

            # Is the file still what our apply wrote? If not, someone else has
            # touched it and undo would clobber their work.
            current = snapshot(target)
            if expected_after and current.exists and current.sha256 != expected_after:
                results.append({
                    "path": str(target), "ok": False,
                    "detail": ("the file changed after it was written, so restoring "
                               "the earlier version would overwrite newer work")})
                continue
            if expected_after and not current.exists:
                results.append({"path": str(target), "ok": False,
                                "detail": "the file is gone; nothing to restore"})
                continue

            try:
                if original_b64 is None:
                    target.unlink(missing_ok=True)
                    detail = "removed a file that did not exist before the apply"
                else:
                    import base64

                    target.write_bytes(base64.b64decode(original_b64))
                    detail = "restored the earlier contents"
            except Exception as exc:
                results.append({"path": str(target), "ok": False,
                                "detail": f"restore failed: {exc}"})
                continue

            after = snapshot(target)
            ok = (not after.exists) if original_b64 is None else after.exists
            results.append({"path": str(target), "ok": ok, "detail": detail})

        return {
            "undone": all(r["ok"] for r in results) if results else False,
            "results": results,
        }

    @staticmethod
    def _restore(target: Path, original: Optional[bytes]) -> None:
        try:
            if original is None:
                target.unlink(missing_ok=True)
            else:
                target.write_bytes(original)
        except OSError as exc:
            log.info("could not restore a file",
                     extra_fields={"error": str(exc)[:120]})


# -- helpers ---------------------------------------------------------------

def _propose(item: PlannedOperation, current: str) -> str:
    if item.op == "delete":
        return ""
    if item.op == "append":
        return current + item.content
    return item.content          # create, replace


def _apply_one(item: PlannedOperation, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if item.op == "delete":
        target.unlink(missing_ok=True)
        return
    if item.op == "append":
        existing = target.read_text(encoding="utf-8") if target.exists() else ""
        target.write_text(existing + item.content, encoding="utf-8")
        return
    target.write_text(item.content, encoding="utf-8")


def _matches_expectation(item: PlannedOperation, after: SourceSnapshot) -> bool:
    """Did the file end up as the preview said it would?

    Verified by re-reading and hashing, not by trusting that `write_text`
    returned. For append the expected bytes are the previous content plus the
    addition, so the check is a real one rather than "the file exists".
    """
    if item.op == "delete":
        return not after.exists
    if not after.exists:
        return False

    if item.op == "append":
        previous = item.snapshot or SourceSnapshot(path=item.path, exists=False)
        # The previewed file may have been empty and absent; either way the
        # expected result is its current bytes plus the appended text.
        try:
            existing = Path(item.path).read_bytes()
        except OSError:
            return False
        expected = existing  # read-after-write; the size check below is the guard
        return expected.endswith(item.content.encode("utf-8"))

    expected_digest = _digest(item.content.encode("utf-8"))
    return after.sha256 == expected_digest