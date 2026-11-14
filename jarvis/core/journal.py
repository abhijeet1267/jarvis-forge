"""Durable journal for multi-file applies, and reconciliation after a crash.

ParallelWorld applies a plan one file at a time and returns its journal in
memory. That is enough to undo in the same process, and useless the moment the
process dies halfway through: the record of what was planned and what was
written died with it. On restart the honest question is not "replay the plan"
-- a replay re-runs side effects that may already have landed -- it is "for
each file, did our write happen?". The only answer that is evidence is a
comparison between what the apply recorded and what is on disk now.

This module keeps that record on disk and does the comparison:

* ``begin`` records the plan (path, op, before/after hashes); entries are
  marked as each write lands, so an interrupted apply leaves a readable
  account of where it stopped.
* ``unfinished`` is what a restart reads: transactions still ``prepared`` or
  ``committing``.
* ``reconcile`` classifies every entry against the bytes actually on disk:
  ``completed``, ``not_applied``, ``changed_since`` or ``unknown``. It reads
  disk through a caller-supplied callback and **never writes a user file**, so
  it cannot clobber the edit that made an entry ``changed_since``.

Two refusals are deliberate. An entry with no recorded hash is ``unknown``,
never ``completed`` -- "completed" is the verdict a recovery path acts on, so
it needs proof. And a transaction that is only partly completed stays
``unknown`` rather than being rounded to success, because a half-applied plan
needs a person to read the report.

The store is a JSON file rather than a table because the journal must be
writable before the database may have been initialised (a crash can precede
the first schema migration) and must survive as one replaceable document.
Writes go through a temp file plus ``os.replace``, so a crash while recording
a crash cannot truncate the record. The journal holds hashes and status only,
never file contents: a journal carrying every file's bytes would become a
second, unmanaged copy of the user's data with its own retention problem.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

#: The file this journal lives in, under ``JARVIS_HOME``.
JOURNAL_FILENAME = "file_journal.json"

#: Transaction states. ``prepared`` and ``committing`` are the two a crash can
#: leave behind; the rest are terminal until a person or the reconcile pass
#: settles them.
TRANSACTION_STATES: tuple = (
    "prepared", "committing", "committed", "failed", "recovered", "unknown",
)

#: Per-entry states, written by the apply path as each file is handled.
ENTRY_STATES: tuple = ("pending", "written", "verified", "failed", "restored")

#: What reconcile can say about one entry. Chosen so that each one maps to a
#: different safe action: complete the transaction, retry from a known-clean
#: state, stop and ask, or admit ignorance.
VERDICTS: tuple = ("completed", "not_applied", "changed_since", "unknown")

#: The states a restart must look at. Nothing else is an open question.
UNFINISHED_STATES: tuple = ("prepared", "committing")

#: Bound on the free-text note so a crash loop cannot inflate one record.
MAX_NOTE = 1000

#: One lock for every instance in the process. The startup reconcile and a
#: request-handling journal can be separate objects; a per-instance lock would
#: let them interleave a read-modify-write. Cross-process writes are not
#: guarded -- the backend is the single writer, which is why this is a plain
#: file and not a shared database.
_WRITE_LOCK = threading.RLock()


class JournalCorrupted(RuntimeError):
    """The journal file exists but cannot be trusted to describe a crash.

    Raised instead of returning an empty journal: an unreadable record of an
    interrupted apply reported as "no unfinished transactions" would invite a
    caller to decide the world is clean when the evidence was simply lost. The
    corrupt file is left untouched so a person can inspect it.
    """


class _Unreadable:
    """A callback that raised. That proves less, not that nothing happened."""

    __slots__ = ("detail",)

    def __init__(self, detail: str) -> None:
        self.detail = detail


#: ``on_disk_stat(path)`` returns the file's current sha256, or None when the
#: file is absent. It may raise; see :meth:`FileTransactionJournal.reconcile`.
OnDiskStat = Callable[[str], Optional[str]]


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def sha256_of_file(path: Path | str) -> Optional[str]:
    """The file's sha256, or None when the file does not exist.

    Only a genuinely missing file returns None. A file that exists but cannot
    be read raises ``OSError`` (which includes a directory): mapping that to
    None would make an unreadable file look *absent*, and an absent file is
    what a completed deletion looks like -- a dangerous conflation. Callers
    that pass this as ``reconcile``'s callback get ``unknown`` for the
    unreadable entry, which is the honest verdict.
    """
    target = Path(path)
    try:
        data = target.read_bytes()
    except FileNotFoundError:
        return None
    return hashlib.sha256(data).hexdigest()


def _join_note(existing: str, addition: str) -> str:
    """Append a note, keeping the newest text when the bound is reached."""
    addition = (addition or "").strip()
    if not addition:
        return existing
    joined = f"{existing}; {addition}" if existing else addition
    return joined[-MAX_NOTE:]


class FileTransactionJournal:
    """A JSON-file journal of multi-file applies, keyed by transaction id.

    Every mutation reads the file, changes one record and writes the whole
    document back atomically. Re-reading on each call (rather than caching)
    means two instances -- the startup reconcile and a later request -- always
    see each other's writes, and it keeps the crash story simple: the file is
    the only truth.
    """

    def __init__(self, path: Optional[Path | str] = None) -> None:
        if path is None:
            # Imported lazily so importing this module does not drag in the
            # settings stack, and so a caller can always inject a path.
            from ..config import data_home

            path = data_home() / JOURNAL_FILENAME
        self.path = Path(path).expanduser()

    # -- reading and writing ------------------------------------------------
    def _read(self) -> Dict[str, Any]:
        path = self.path
        if not path.is_file():
            return {"version": 1, "transactions": []}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise JournalCorrupted(
                f"the file journal at {path} could not be read as JSON "
                f"({exc}); it describes an interrupted apply and was left "
                "untouched for inspection") from exc
        if not isinstance(raw, dict):
            raise JournalCorrupted(
                f"the file journal at {path} is not a JSON object")
        transactions = raw.get("transactions")
        if not isinstance(transactions, list):
            raise JournalCorrupted(
                f"the file journal at {path} has no transactions list")
        return {
            "version": 1,
            "transactions": [self._normalize_record(item, path)
                             for item in transactions],
        }

    def _normalize_record(self, raw: Any, path: Path) -> Dict[str, Any]:
        if not isinstance(raw, dict):
            raise JournalCorrupted(
                f"the file journal at {path} contains a transaction that is "
                "not an object")
        transaction_id = str(raw.get("transaction_id") or "")
        entries_raw = raw.get("entries")
        if not transaction_id or not isinstance(entries_raw, list):
            raise JournalCorrupted(
                f"the file journal at {path} contains a transaction with no "
                "id or no entries list")
        entries: List[Dict[str, Any]] = []
        for item in entries_raw:
            if not isinstance(item, dict):
                raise JournalCorrupted(
                    f"the file journal at {path} contains an entry that is "
                    "not an object")
            entry_path = str(item.get("path") or "")
            if not entry_path:
                raise JournalCorrupted(
                    f"the file journal at {path} contains an entry with no "
                    "path")
            state = str(item.get("state") or "pending")
            entries.append({
                "path": entry_path,
                "op": str(item.get("op") or ""),
                "before_sha256": self._hash_or_none(item.get("before_sha256")),
                "after_sha256": self._hash_or_none(item.get("after_sha256")),
                "state": state if state in ENTRY_STATES else "pending",
            })
        state = str(raw.get("state") or "unknown")
        return {
            "transaction_id": transaction_id,
            "created_at": _float(raw.get("created_at"), 0.0),
            "updated_at": _float(raw.get("updated_at"), 0.0),
            "state": state if state in TRANSACTION_STATES else "unknown",
            "entries": entries,
            "note": str(raw.get("note") or ""),
        }

    def _write(self, data: Dict[str, Any]) -> None:
        target = self.path
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, target)
        finally:
            # A failed write must not leave a half-written sibling that a
            # later reader could mistake for the journal.
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass

    @staticmethod
    def _hash_or_none(value: Any) -> Optional[str]:
        """A recorded hash, with "" meaning absent.

        ParallelWorld's snapshots use ``sha256=""`` for a file that does not
        exist, and a delete's post-state is absence. Mapping the empty string
        to None is what lets a completed deletion reconcile as completed.
        """
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def _find(self, data: Dict[str, Any], tid: str) -> Dict[str, Any]:
        for record in data["transactions"]:
            if record["transaction_id"] == tid:
                return record
        raise KeyError(
            f"no transaction {tid!r} is recorded in {self.path}")

    def _update(self, tid: str,
                change: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
        with _WRITE_LOCK:
            data = self._read()
            record = self._find(data, tid)
            change(record)
            record["updated_at"] = time.time()
            self._write(data)
            return copy.deepcopy(record)

    # -- recording an apply -------------------------------------------------
    def begin(self, entries: List[Dict[str, Any]]) -> str:
        """Open a transaction in state ``prepared`` with every entry pending.

        Each entry carries ``path``, ``op`` and optionally ``before_sha256``
        and ``after_sha256``. The after hash may be known at this point
        because ParallelWorld stages the new content before writing; when it
        is not, ``mark_entry`` records it once the write has been verified.

        Extra keys are dropped on purpose. ParallelWorld's in-memory journal
        also carries ``original_b64`` for undo, and file contents have no
        place in a durable state record.
        """
        normalized: List[Dict[str, Any]] = []
        seen = set()
        for raw in entries:
            entry = self._normalize_entry(raw)
            if entry["path"] in seen:
                # Two operations on one path cannot be told apart after a
                # crash: both entries would match, or neither would, and no
                # reconciliation could say which one landed.
                raise ValueError(
                    f"the transaction names {entry['path']!r} more than once; "
                    "one file may appear in a journaled apply at most once")
            seen.add(entry["path"])
            normalized.append(entry)

        tid = uuid.uuid4().hex
        now = time.time()
        record = {
            "transaction_id": tid,
            "created_at": now,
            "updated_at": now,
            "state": "prepared",
            "entries": normalized,
            "note": "",
        }
        with _WRITE_LOCK:
            data = self._read()
            data["transactions"].append(record)
            self._write(data)
        return tid

    @staticmethod
    def _normalize_entry(raw: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(raw, dict):
            raise ValueError("a journal entry must be an object")
        path = str(raw.get("path") or "").strip()
        if not path:
            raise ValueError("a journal entry needs a path")
        return {
            "path": path,
            "op": str(raw.get("op") or ""),
            "before_sha256": FileTransactionJournal._hash_or_none(
                raw.get("before_sha256")),
            "after_sha256": FileTransactionJournal._hash_or_none(
                raw.get("after_sha256")),
            "state": "pending",
        }

    def mark_committing(self, tid: str) -> Dict[str, Any]:
        """Record that writing has started. A crash from here on is ambiguous."""
        return self._update(tid, lambda record: record.update(state="committing"))

    def mark_entry(self, tid: str, path: str, state: str, *,
                   after_sha256: Optional[str] = None) -> Dict[str, Any]:
        """Record one file's progress, and its result hash once known.

        ``after_sha256`` is the hash of the bytes the apply verified on disk,
        so reconcile can later prove completion rather than guess it.
        """
        if state not in ENTRY_STATES:
            raise ValueError(
                f"{state!r} is not an entry state. Known: "
                f"{', '.join(ENTRY_STATES)}.")

        def change(record: Dict[str, Any]) -> None:
            for entry in record["entries"]:
                if entry["path"] == path:
                    entry["state"] = state
                    if after_sha256 is not None:
                        entry["after_sha256"] = self._hash_or_none(after_sha256)
                    return
            raise KeyError(
                f"transaction {tid!r} records no entry for {path!r}")

        return self._update(tid, change)

    def mark_committed(self, tid: str) -> Dict[str, Any]:
        """Every entry landed and was verified; nothing is left to reconcile."""
        return self._update(tid, lambda record: record.update(state="committed"))

    def mark_failed(self, tid: str, note: str = "") -> Dict[str, Any]:
        """The apply stopped without completing. The note says why."""
        def change(record: Dict[str, Any]) -> None:
            record["state"] = "failed"
            record["note"] = _join_note(record.get("note", ""), note)
        return self._update(tid, change)

    def mark_recovered(self, tid: str, note: str = "") -> Dict[str, Any]:
        """A person or the mission runner looked and decided what to do.

        Deliberately a separate step from :meth:`reconcile`: classifying the
        disk is evidence, and marking a transaction recovered is a decision on
        that evidence. Nothing in this module takes that decision itself.
        """
        def change(record: Dict[str, Any]) -> None:
            record["state"] = "recovered"
            record["note"] = _join_note(record.get("note", ""), note)
        return self._update(tid, change)

    # -- reading back -------------------------------------------------------
    def get(self, tid: str) -> Optional[Dict[str, Any]]:
        """One transaction's record, or None when no such transaction exists."""
        with _WRITE_LOCK:
            data = self._read()
        for record in data["transactions"]:
            if record["transaction_id"] == tid:
                return copy.deepcopy(record)
        return None

    def unfinished(self) -> List[Dict[str, Any]]:
        """Transactions a restart must look at: ``prepared`` or ``committing``.

        Ordered as recorded, oldest first, so a caller can process a crash
        history in the order it happened.
        """
        with _WRITE_LOCK:
            data = self._read()
        return [copy.deepcopy(record) for record in data["transactions"]
                if record["state"] in UNFINISHED_STATES]

    # -- reconciliation -----------------------------------------------------
    def reconcile(self, on_disk_stat: OnDiskStat) -> List[Dict[str, Any]]:
        """Compare every unfinished transaction against the disk as it is now.

        ``on_disk_stat(path)`` returns the file's current sha256, or None when
        the file does not exist. If the callback raises, the entry is
        classified ``unknown``: an unreadable file proves less, not that
        nothing happened.

        The safety rules are encoded here so no caller can relax them:

        * ``completed`` requires the recorded after hash to match disk, or a
          recorded deletion whose file is genuinely gone. An entry with no
          recorded hashes is ``unknown`` even when the file "looks right";
          "unknown" is reported and never promoted.
        * An entry matching neither hash is ``changed_since`` and is surfaced
          in the report. This method never writes to a user file, so the edit
          that produced that state cannot be clobbered by reconciling.
        * A transaction settles to ``committed`` only when *every* entry is
          ``completed``, and to ``failed`` only when *every* entry is
          ``not_applied``. A mixed result stays ``unknown``; a half-applied
          plan is a person's call, not a convenience flag.

        Only the transaction's state is updated. Entry states stay as the
        apply recorded them, so "what the apply saw" and "what is on disk
        now" remain two distinct facts.
        """
        with _WRITE_LOCK:
            data = self._read()
            reports: List[Dict[str, Any]] = []
            cache: Dict[str, Any] = {}
            for record in data["transactions"]:
                if record["state"] not in UNFINISHED_STATES:
                    continue
                reports.append(self._reconcile_one(record, on_disk_stat, cache))
            if reports:
                self._write(data)
        return reports

    def _reconcile_one(self, record: Dict[str, Any], on_disk_stat: OnDiskStat,
                       cache: Dict[str, Any]) -> Dict[str, Any]:
        counts = {verdict: 0 for verdict in VERDICTS}
        rows: List[Dict[str, Any]] = []
        for entry in record["entries"]:
            verdict, detail = self._verdict(entry, on_disk_stat, cache)
            counts[verdict] += 1
            rows.append({"path": entry["path"], "verdict": verdict,
                         "detail": detail})

        if not rows:
            # Vacuous truth is not a verdict: an empty plan proves nothing
            # about what happened to the files it never named.
            state = "unknown"
        elif counts["completed"] == len(rows):
            state = "committed"
        elif counts["not_applied"] == len(rows):
            state = "failed"
        else:
            state = "unknown"

        record["state"] = state
        record["updated_at"] = time.time()
        record["note"] = _join_note(
            record.get("note", ""),
            "reconciled after an interrupted apply: "
            + ", ".join(f"{counts[v]} {v}" for v in VERDICTS))

        return {
            "transaction_id": record["transaction_id"],
            "state": state,
            "entries": rows,
            "summary": {
                "completed": counts["completed"],
                "not_applied": counts["not_applied"],
                "changed_since": counts["changed_since"],
                "unknown": counts["unknown"],
            },
        }

    def _verdict(self, entry: Dict[str, Any], on_disk_stat: OnDiskStat,
                 cache: Dict[str, Any]) -> tuple:
        before = entry.get("before_sha256")
        after = entry.get("after_sha256")
        if before is None and after is None:
            return ("unknown",
                    "this entry recorded neither a before nor an after hash, "
                    "so nothing on disk can confirm what the apply did")
        disk = self._stat_cached(entry["path"], on_disk_stat, cache)
        if isinstance(disk, _Unreadable):
            return ("unknown",
                    f"the file could not be read to compare it: {disk.detail}")

        # Completion first: the recorded after hash matching disk is the only
        # proof. When before == after (a no-op write) this settles as
        # completed -- the disk is exactly the state the apply intended.
        if after is not None and disk == after:
            return ("completed",
                    "the file on disk matches the hash recorded after the write")
        if after is None and before is not None and disk is None:
            return ("completed",
                    "the entry recorded a deletion and the file is absent")

        # Never written: the file still holds its pre-apply bytes, or was
        # absent before and is still absent.
        if before is not None and disk == before:
            return ("not_applied",
                    "the file still holds the bytes it held before the apply")
        if before is None and disk is None:
            return ("not_applied",
                    "the file was absent before the apply and is still absent")

        # Anything else has moved since the crash. Reported, never repaired.
        return ("changed_since",
                "the file matches neither the before nor the after hash, so "
                "it was edited after the crash and must not be overwritten")

    @staticmethod
    def _stat_cached(path: str, on_disk_stat: OnDiskStat,
                     cache: Dict[str, Any]) -> Any:
        if path not in cache:
            try:
                cache[path] = on_disk_stat(path)
            except Exception as exc:  # a callback that raises is evidence of doubt
                cache[path] = _Unreadable(str(exc)[:160])
        return cache[path]

    # -- retention ----------------------------------------------------------
    def prune(self, keep: int = 50) -> int:
        """Drop old, finished transactions; return how many were removed.

        Unfinished transactions are never pruned, however old: they are not
        history, they are an open question about files on disk, and deleting
        the question is exactly the silent loss this journal exists to
        prevent. The bound therefore applies to finished transactions, which
        is where unbounded growth actually comes from.
        """
        if keep < 0:
            raise ValueError("keep must be zero or greater")
        with _WRITE_LOCK:
            data = self._read()
            transactions = data["transactions"]
            unfinished = [record for record in transactions
                          if record["state"] in UNFINISHED_STATES]
            finished = [record for record in transactions
                        if record["state"] not in UNFINISHED_STATES]
            keep_finished = finished[-keep:] if keep else []
            kept_ids = {record["transaction_id"]
                        for record in unfinished + keep_finished}
            remaining = [record for record in transactions
                         if record["transaction_id"] in kept_ids]
            removed = len(transactions) - len(remaining)
            if removed:
                data["transactions"] = remaining
                self._write(data)
        return removed