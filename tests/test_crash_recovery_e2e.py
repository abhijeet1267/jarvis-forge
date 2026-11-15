"""A real interrupted multi-file commit, reconciled after restart.

The journal's unit tests use synthetic hashes. This drives the same path with
**real files and the real ParallelWorld apply loop**, then simulates the crash at
each point a commit can stop and asserts what a restart can honestly conclude.

"The process died between file one and file two" is not something a mock can
establish: the whole question is what is actually on disk versus what the
journal recorded. So each case here writes real bytes, records the real hashes
through the journal, and then reconciles against a real filesystem read.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from jarvis.core.journal import FileTransactionJournal, sha256_of_file


@pytest.fixture()
def journal(tmp_path):
    return FileTransactionJournal(path=tmp_path / "journal.json")


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _begin(journal, files):
    """Open a transaction the way ParallelWorld.apply does, with real hashes."""
    entries = []
    for path in files:
        entries.append({
            "path": str(path),
            "op": "replace",
            "before_sha256": _digest(path) if path.exists() else None,
            "after_sha256": None,
        })
    tid = journal.begin(entries)
    journal.mark_committing(tid)
    return tid


def test_a_crash_before_any_write_is_reconciled_as_not_applied(tmp_path, journal):
    first = tmp_path / "a.txt"
    second = tmp_path / "b.txt"
    first.write_text("original a\n", encoding="utf-8")
    second.write_text("original b\n", encoding="utf-8")

    _begin(journal, [first, second])
    # Crash here: nothing was written.

    reports = journal.reconcile(sha256_of_file)
    assert len(reports) == 1
    entries = {Path(e["path"]).name: e["verdict"] for e in reports[0]["entries"]}
    assert entries == {"a.txt": "not_applied", "b.txt": "not_applied"}
    assert reports[0]["state"] == "failed"


def test_a_crash_after_the_first_write_reports_exactly_that(tmp_path, journal):
    """Half a commit must be visible as half, not as whole or as none."""
    first = tmp_path / "a.txt"
    second = tmp_path / "b.txt"
    first.write_text("original a\n", encoding="utf-8")
    second.write_text("original b\n", encoding="utf-8")

    tid = _begin(journal, [first, second])

    # The first file is written and recorded; the second never is.
    first.write_text("new a\n", encoding="utf-8")
    journal.mark_entry(tid, str(first), "verified", after_sha256=_digest(first))

    reports = journal.reconcile(sha256_of_file)
    entries = {Path(e["path"]).name: e["verdict"] for e in reports[0]["entries"]}

    assert entries["a.txt"] == "completed"
    assert entries["b.txt"] == "not_applied"
    assert reports[0]["summary"]["completed"] == 1
    assert reports[0]["summary"]["not_applied"] == 1
    assert reports[0]["state"] == "unknown", (
        "a half-finished transaction is not committed and not simply failed")


def test_a_file_edited_after_the_crash_is_reported_as_changed_since(tmp_path, journal):
    """The newer work wins; recovery must not overwrite it."""
    first = tmp_path / "a.txt"
    first.write_text("original\n", encoding="utf-8")
    tid = _begin(journal, [first])

    first.write_text("written by us\n", encoding="utf-8")
    journal.mark_entry(tid, str(first), "verified", after_sha256=_digest(first))

    # While Vantage was down, a person edited the file.
    first.write_text("edited by the person\n", encoding="utf-8")

    reports = journal.reconcile(sha256_of_file)
    entry = reports[0]["entries"][0]
    assert entry["verdict"] == "changed_since"
    assert first.read_text(encoding="utf-8") == "edited by the person\n", (
        "reconciliation must not modify the file")


def test_a_deleted_file_whose_deletion_was_recorded_is_completed(tmp_path, journal):
    target = tmp_path / "gone.txt"
    target.write_text("delete me\n", encoding="utf-8")
    tid = _begin(journal, [target])

    target.unlink()
    journal.mark_entry(tid, str(target), "verified", after_sha256=None)

    reports = journal.reconcile(sha256_of_file)
    assert reports[0]["entries"][0]["verdict"] == "completed"


def test_an_unreadable_file_is_unknown_not_failed(tmp_path, journal):
    """'I could not look' is not 'it did not happen'."""
    target = tmp_path / "a.txt"
    target.write_text("x\n", encoding="utf-8")
    _begin(journal, [target])

    def broken_stat(path):
        raise OSError("permission denied")

    reports = journal.reconcile(broken_stat)
    assert reports[0]["entries"][0]["verdict"] == "unknown"


def test_a_clean_apply_through_parallelworld_is_journaled_as_committed(tmp_path, monkeypatch):
    """The real apply loop writes the journal, not a hand-built one."""
    from jarvis.core import parallelworld

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("JARVIS_HOME", str(home))

    first = tmp_path / "one.txt"
    second = tmp_path / "two.txt"
    first.write_text("one\n", encoding="utf-8")
    second.write_text("two\n", encoding="utf-8")

    world = parallelworld.ParallelWorld()
    result = world.apply(
        world.plan([
            {"op": "replace", "path": str(first), "content": "one changed\n"},
            {"op": "replace", "path": str(second), "content": "two changed\n"},
        ]),
        approved=True)

    assert result["applied"] is True
    assert first.read_text(encoding="utf-8") == "one changed\n"

    stored = json.loads((home / "file_journal.json").read_text())
    transactions = stored.get("transactions", stored)
    records = list(transactions.values()) if isinstance(transactions, dict) else transactions
    assert records, "the real apply must leave a journal record"
    assert records[-1]["state"] == "committed"
    assert sorted(e["path"] for e in records[-1]["entries"]) == sorted(
        [str(first), str(second)])


def test_reconcile_never_writes_to_the_filesystem(tmp_path, journal):
    """Reconciliation reports; it does not repair. Repair needs its own authorization."""
    first = tmp_path / "a.txt"
    first.write_text("original\n", encoding="utf-8")
    tid = _begin(journal, [first])
    first.write_text("written\n", encoding="utf-8")
    journal.mark_entry(tid, str(first), "verified", after_sha256=_digest(first))
    journal.mark_committed(tid)

    before = first.read_bytes()
    journal.reconcile(sha256_of_file)
    assert first.read_bytes() == before

    # And finished transactions are not re-reported as work.
    assert journal.reconcile(sha256_of_file) == [] or \
        all(r["state"] == "committed" for r in journal.reconcile(sha256_of_file))