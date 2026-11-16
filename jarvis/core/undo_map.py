"""Temporal Undo Map: what changed, when, and whether it can honestly be undone.

A person who has let an agent act needs one view: a timeline of the effects that
landed, and for each one an honest answer to "can I take that back?". The
temptation is to draw that map from what the tools *reported*. That produces a
comforting screen and a dangerous promise: "the tool said it copied the file"
is not a record of what is on disk, and "I sent the email" has no inverse at
all.

So the map keeps four things true at once:

* **It records the pre-image, not a claim of one.** When a file write is about
  to happen, the bytes before it are captured to a content-addressed store. The
  entry is reversible only because those bytes exist and hash to the recorded
  value -- not because any tool said so.
* **Reversibility is a property of the effect, not optimism.** A file created or
  modified is reversible (restore the pre-image, or delete the new file). A file
  deleted is reversible *only* if a pre-image was captured. An email sent, a
  payment, a message to a person, a physical action -- are **not reversible**,
  and the map says so in those words rather than offering a button that would
  quietly do nothing or, worse, re-send.
* **Newer work is never clobbered.** An undo refuses when the file has changed
  since the recorded effect. Undoing across somebody else's edit would destroy
  work the map was meant to protect; it reports ``conflict`` instead.
* **Undo is itself recorded.** An undo writes its own entry, so the map never
  claims a state the store does not hold, and undoing an undo is a visible,
  separate act rather than an implicit toggle.

The map is built from the audit log (what ran) joined with the undo store (what
bytes may be restored). It reads the world at query time; it never writes a user
file except on an explicit ``undo`` of one reversible entry.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..logging_setup import get_logger

log = get_logger("core.undo_map")

#: How an effect stands with respect to reversal.
REVERSIBLE = "reversible"
IRREVERSIBLE = "irreversible"
CONFLICTED = "conflicted"      # reversible in principle, but the file moved on
ALREADY_UNDONE = "undone"
UNKNOWN = "unknown"            # cannot be established

#: Tool effects, by what an undo would have to do. A tool not in either table is
#: classified from its risk tier and neutral name; nothing here is trusted from a
#: tool's own message.
#:
#: ``undo_kind`` chosen from: ``restore`` (put back the pre-image), ``remove``
#: (delete a file the action created), ``none`` (no inverse exists).
TOOL_EFFECTS: Dict[str, str] = {
    # Files an action creates or changes: both reversible from a pre-image.
    "write_file": "restore",
    "append_file": "restore",
    "copy_file": "restore",
    "move_file": "restore",
    "rename_file": "restore",
    "create_folder": "remove",
    # Outbound and physical effects have no inverse. Listed explicitly so a new
    # path cannot be assumed reversible by default.
    "send_email": "none",
    "send_message": "none",
    "send_to_device": "none",
    "run_allowlisted_command": "none",
    "open_application": "none",
    "emergency_stop": "none",
    "delete_file": "restore",   # reversible *only if* a pre-image was captured
}

#: Risk tiers where no inverse can exist regardless of the tool's name. These
#: override ``TOOL_EFFECTS``: an action that costs money or moves a body is not
#: undoable by restoring a file.
IRREVERSIBLE_RISK = {"destructive", "physical"}

#: Bundled alongside the map so a screen never has to guess a colour.
MAX_ENTRIES = 500


class UndoError(ValueError):
    """The requested undo cannot be honoured, with the reason it cannot."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class UndoStore:
    """A content-addressed store of pre-images, plus the map's own records.

    One JSON index (entry id -> what it can restore) and one directory of blobs
    keyed by their hash. Keeping blobs content-addressed means two effects that
    started from the same bytes share one copy, and an undo can verify the
    bytes it is about to restore before it touches the file.
    """

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root) if root is not None else self._default_root()
        self.index_path = self.root / "undo_index.json"
        self.blob_dir = self.root / "blobs"
        self._lock = threading.RLock()

    @staticmethod
    def _default_root() -> Path:
        from ..config import data_home

        return data_home() / "undo"

    # -- storage -------------------------------------------------------------
    def _read(self) -> Dict[str, Any]:
        try:
            return json.loads(self.index_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"entries": {}}
        except (OSError, ValueError) as exc:
            log.info("undo index unreadable", extra_fields={"error": str(exc)})
            return {"entries": {}}

    def _write(self, data: Dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self.index_path)

    def _blob_path(self, digest: str) -> Path:
        return self.blob_dir / digest

    def put_blob(self, data: bytes) -> str:
        """Store bytes and return their hash. Idempotent."""
        digest = _sha256(data)
        self.blob_dir.mkdir(parents=True, exist_ok=True)
        target = self._blob_path(digest)
        if not target.exists():
            tmp = target.with_suffix(".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, target)
        return digest

    def get_blob(self, digest: str) -> Optional[bytes]:
        try:
            return self._blob_path(digest).read_bytes()
        except OSError:
            return None

    # -- recording -----------------------------------------------------------
    def record(self, *, tool: str, path: str, before_sha256: str = "",
               after_sha256: str = "", undo_kind: str = "restore",
               log_id: str = "", note: str = "") -> Dict[str, Any]:
        """Record one effect and, when a pre-image exists, what would undo it."""
        entry_id = "UND-" + uuid.uuid4().hex[:12]
        record = {
            "entry_id": entry_id,
            "tool": tool,
            "path": str(path),
            "undo_kind": undo_kind,
            "before_sha256": before_sha256,
            "after_sha256": after_sha256,
            "log_id": log_id,
            "note": note,
            "created_at": time.time(),
            "undone_at": 0.0,
            "undo_entry": "",
        }
        with self._lock:
            data = self._read()
            data["entries"][entry_id] = record
            self._write(data)
        return record

    def capture_before(self, path: str) -> str:
        """Read a file's current bytes into the store and return their hash.

        A file that does not exist yet has no pre-image; the empty hash says so,
        and that entry's undo is "remove what was created" rather than "restore".
        """
        target = Path(path).expanduser()
        try:
            data = target.read_bytes()
        except FileNotFoundError:
            return ""
        except OSError as exc:
            raise UndoError(f"{target} could not be read before the change: {exc}")
        return self.put_blob(data)

    def get(self, entry_id: str) -> Optional[Dict[str, Any]]:
        return self._read()["entries"].get(entry_id)

    def entries(self, *, limit: int = MAX_ENTRIES) -> List[Dict[str, Any]]:
        records = list(self._read()["entries"].values())
        records.sort(key=lambda r: r.get("created_at", 0), reverse=True)
        return records[:max(1, min(int(limit or MAX_ENTRIES), MAX_ENTRIES))]

    def _update(self, entry_id: str, **changes: Any) -> Dict[str, Any]:
        with self._lock:
            data = self._read()
            record = data["entries"].get(entry_id)
            if record is None:
                raise UndoError(f"no undo entry {entry_id!r}")
            record.update(changes)
            self._write(data)
            return record


def classify(tool: str, *, risk: str = "", before_sha256: str = "") -> Tuple[str, str]:
    """How an effect stands, and the sentence that says why.

    Returns ``(state, why)`` where state is one of the module's states. A
    destructive or physical action is irreversible by tier alone; the tool's own
    table is consulted otherwise.
    """
    lowered = str(tool or "").strip()
    tier = str(risk or "").strip().lower()
    kind = TOOL_EFFECTS.get(lowered)

    # An undo is not an effect to be undone -- it is already the reversal of one.
    # Recording it and then asking "can this be undone?" of itself produced a
    # confusing "unknown: the map will not guess" row beneath every real one.
    if lowered == "undo":
        return "undo", ("this is a reversal of the entry below it; it is not itself "
                        "an effect to undo")

    if tier in IRREVERSIBLE_RISK:
        if lowered == "delete_file" and before_sha256:
            return REVERSIBLE, ("the file was deleted but its contents were captured "
                                "beforehand, so it can be restored byte for byte")
        if tier == "physical":
            return IRREVERSIBLE, ("this acted on the physical world; there is no pre-image "
                                  "of an unlocked door or a moved object")
        return IRREVERSIBLE, (f"this was a destructive action; {lowered or 'it'} has no "
                              "inverse that restores what existed before")

    if kind == "none" or lowered in {"send_email", "send_message", "send_to_device"}:
        return IRREVERSIBLE, (f"{lowered} reached something outside this Mac; a recipient "
                              "cannot be un-sent, so it cannot be undone -- only its "
                              "consequences can be managed")

    if kind == "remove":
        return REVERSIBLE, f"{lowered} created something that can be removed"

    if kind == "restore":
        if before_sha256:
            return REVERSIBLE, ("the file's previous bytes were captured, so it can be "
                                "restored exactly")
        return REVERSIBLE, ("the file this action created can be removed; it had no "
                            "previous contents")

    # An unknown tool: fall back to the risk tier, never to optimism.
    if tier in {"", "read"}:
        return UNKNOWN, (f"{lowered or 'this tool'} is not classified for undo; the map "
                         "will not guess that it is reversible")
    return UNKNOWN, (f"{lowered} has no declared undo rule; whether it can be undone is "
                     "not established")


class UndoMap:
    """The timeline and the one operation that really reverses an effect."""

    def __init__(self, store: Optional[UndoStore] = None) -> None:
        self.store = store or UndoStore()

    # -- reads ---------------------------------------------------------------
    def timeline(self, *, limit: int = 60) -> List[Dict[str, Any]]:
        """Every recorded effect, newest first, each with its standing."""
        out: List[Dict[str, Any]] = []
        for record in self.store.entries(limit=limit):
            state, why = classify(record.get("tool", ""), risk=record.get("risk", ""),
                                  before_sha256=record.get("before_sha256", ""))
            if record.get("undone_at"):
                state, why = ALREADY_UNDONE, (f"undone at {record['undone_at']:.0f}; "
                                              "the undo is recorded below it")
            path = record.get("path", "")
            current = self._current_hash(path)
            if (state == REVERSIBLE and record.get("undo_kind") == "restore"
                    and current and record.get("after_sha256")
                    and current != record.get("after_sha256")):
                state, why = CONFLICTED, ("the file changed after this action, so "
                                          "undoing it would overwrite newer work")
            out.append({
                "entry_id": record["entry_id"],
                "tool": record.get("tool", ""),
                "path": path,
                "state": state,
                "why": why,
                "undo_kind": record.get("undo_kind", ""),
                "created_at": record.get("created_at", 0.0),
                "undone_at": record.get("undone_at", 0.0),
                "current_sha256": current,
            })
        return out

    def summary(self) -> Dict[str, Any]:
        rows = self.timeline(limit=MAX_ENTRIES)
        counts: Dict[str, int] = {}
        for row in rows:
            counts[row["state"]] = counts.get(row["state"], 0) + 1
        return {
            "counts": counts,
            "total": len(rows),
            "note": ("An effect is reversible only when the bytes to restore exist and "
                     "still match. Anything that left this Mac, or acted on the physical "
                     "world, is shown as irreversible rather than offered an undo that "
                     "would not be one."),
        }

    def _current_hash(self, path: str) -> str:
        if not path:
            return ""
        try:
            return _sha256(Path(path).expanduser().read_bytes())
        except OSError:
            return ""

    # -- the one real reversal ----------------------------------------------
    def undo(self, entry_id: str) -> Dict[str, Any]:
        """Reverse one reversible effect. Refuses anything that is not one.

        Restores the captured pre-image (verifying its hash first) or, for a file
        this action created, removes it. Refuses when the file has changed since,
        when the entry is irreversible, or when the pre-image is missing -- each
        with the reason, because a silent no-op would read as success.
        """
        record = self.store.get(entry_id)
        if record is None:
            raise UndoError(f"no undo entry {entry_id!r}")
        if record.get("undone_at"):
            raise UndoError(f"{entry_id} was already undone")

        state, why = classify(record.get("tool", ""), risk=record.get("risk", ""),
                              before_sha256=record.get("before_sha256", ""))
        if state != REVERSIBLE:
            raise UndoError(why)

        path = Path(str(record.get("path") or "")).expanduser()
        if not str(path):
            raise UndoError("this entry names no file to undo")

        before = record.get("before_sha256", "")
        after = record.get("after_sha256", "")
        current = self._current_hash(str(path))
        if after and current and current != after:
            raise UndoError(f"{path} has changed since this action; undoing it would "
                            "overwrite newer work, so it was refused")

        if record.get("undo_kind") == "remove":
            if not path.exists():
                raise UndoError(f"{path} is already gone")
            try:
                path.unlink()
            except OSError as exc:
                raise UndoError(f"{path} could not be removed: {exc}")
            restored = f"removed {path}, which this action had created"
        else:
            if not before:
                raise UndoError(
                    f"no pre-image was captured for {path}, so its previous contents "
                    "cannot be restored")
            data = self.store.get_blob(before)
            if data is None:
                raise UndoError(f"the captured contents of {path} are missing from the "
                                "undo store")
            if _sha256(data) != before:
                raise UndoError("the captured contents do not match their recorded hash; "
                                "not writing them over the file")
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".undo-tmp")
            tmp.write_bytes(data)
            os.replace(tmp, path)
            restored = f"restored {path} to the {len(data)} bytes it held before"

        # Record the undo as its own effect, so the map never claims a state the
        # store does not hold and an undo of an undo is a visible choice.
        undo_record = self.store.record(
            tool="undo", path=str(path), undo_kind="none",
            before_sha256=self._current_hash(str(path)),
            note=f"undo of {entry_id}")
        self.store._update(entry_id, undone_at=time.time(),
                           undo_entry=undo_record["entry_id"])
        log.info("undo applied", extra_fields={"entry": entry_id, "path": str(path)})
        return {"entry_id": entry_id, "restored": restored,
                "undo_entry": undo_record["entry_id"],
                "verified_sha256": self._current_hash(str(path))}


_MAP: Optional[UndoMap] = None


def get_undo_map() -> UndoMap:
    global _MAP
    if _MAP is None:
        _MAP = UndoMap()
    return _MAP


def describe_undo_map(undo_map: Optional[UndoMap] = None) -> Dict[str, Any]:
    m = undo_map or get_undo_map()
    body = m.summary()
    body["entries"] = m.timeline(limit=60)
    return body