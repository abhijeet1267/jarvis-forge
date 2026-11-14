"""The file journal: what a restart can know after an interrupted multi-file apply.

The tests that matter are the ones about *honesty*. A reconciliation that
calls an unproven entry "completed" invites a recovery path to skip work that
never happened; one that silently overwrites a file edited after the crash
destroys exactly the newer work the whole design is trying to protect. So the
cases below write real files into ``tmp_path`` and hash them for real, rather
than mocking the disk: the classification is only worth anything against actual
bytes.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from jarvis.core.journal import (
    FileTransactionJournal,
    JournalCorrupted,
    sha256_of_file,
)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _journal(tmp_path: Path) -> FileTransactionJournal:
    return FileTransactionJournal(tmp_path / "file_journal.json")


def _entry(path: Path, op: str, before: str | None,
           after: str | None) -> dict:
    return {"path": str(path), "op": op,
            "before_sha256": before, "after_sha256": after}


# -- the ordinary, uninterrupted round trip ---------------------------------

def test_a_finished_transaction_round_trips_through_the_store(tmp_path):
    target = tmp_path / "doc.txt"
    target.write_text("after\n", encoding="utf-8")
    journal = _journal(tmp_path)

    tid = journal.begin([_entry(target, "replace", _digest(b"before\n"),
                                _digest(b"after\n"))])
    prepared = journal.get(tid)
    assert prepared["state"] == "prepared"
    assert prepared["entries"][0]["state"] == "pending"

    journal.mark_committing(tid)
    assert journal.get(tid)["state"] == "committing"

    # Written with the hash the apply verified, then promoted to verified.
    journal.mark_entry(tid, str(target), "written",
                       after_sha256=sha256_of_file(target))
    journal.mark_entry(tid, str(target), "verified")
    journal.mark_committed(tid)

    final = journal.get(tid)
    assert final["state"] == "committed"
    assert final["entries"][0]["state"] == "verified"
    assert final["entries"][0]["after_sha256"] == _digest(b"after\n")
    assert journal.unfinished() == []

    # The record is durable, not held in the object: a second instance --
    # what a restarted process is -- reads the same thing.
    reread = FileTransactionJournal(journal.path).get(tid)
    assert reread["state"] == "committed"


def test_an_unknown_transaction_is_none_not_a_crash(tmp_path):
    journal = _journal(tmp_path)
    assert journal.get("no-such-transaction") is None


# -- what a restart looks at ------------------------------------------------

def test_unfinished_finds_a_transaction_left_committing(tmp_path):
    target = tmp_path / "doc.txt"
    target.write_text("half\n", encoding="utf-8")
    journal = _journal(tmp_path)

    tid = journal.begin([_entry(target, "replace", None, _digest(b"half\n"))])
    journal.mark_committing(tid)

    # A second journal object stands in for the process that restarts.
    restarted = _journal(tmp_path)
    open_transactions = restarted.unfinished()
    assert [record["transaction_id"] for record in open_transactions] == [tid]
    assert open_transactions[0]["state"] == "committing"


def test_a_prepared_transaction_is_also_unfinished(tmp_path):
    """A crash between recording the plan and starting it is still open."""
    journal = _journal(tmp_path)
    tid = journal.begin([_entry(tmp_path / "x.txt", "create", None,
                                _digest(b"x"))])
    assert [r["transaction_id"] for r in journal.unfinished()] == [tid]


# -- reconciliation ---------------------------------------------------------

def test_reconcile_classifies_a_genuinely_written_file_as_completed(tmp_path):
    target = tmp_path / "doc.txt"
    target.write_text("the new content\n", encoding="utf-8")
    journal = _journal(tmp_path)

    tid = journal.begin([_entry(target, "replace", _digest(b"old\n"),
                                _digest(b"the new content\n"))])
    journal.mark_committing(tid)
    journal.mark_entry(tid, str(target), "written",
                       after_sha256=sha256_of_file(target))

    reports = journal.reconcile(sha256_of_file)

    assert len(reports) == 1
    report = reports[0]
    assert report["transaction_id"] == tid
    assert report["entries"][0]["verdict"] == "completed"
    assert report["summary"] == {"completed": 1, "not_applied": 0,
                                 "changed_since": 0, "unknown": 0}
    assert report["state"] == "committed"
    assert journal.get(tid)["state"] == "committed"


def test_reconcile_classifies_a_file_nobody_touched_as_not_applied(tmp_path):
    target = tmp_path / "doc.txt"
    target.write_text("old\n", encoding="utf-8")
    journal = _journal(tmp_path)

    tid = journal.begin([_entry(target, "replace", _digest(b"old\n"),
                                _digest(b"new\n"))])
    journal.mark_committing(tid)

    report = journal.reconcile(sha256_of_file)[0]

    assert report["entries"][0]["verdict"] == "not_applied"
    assert report["state"] == "failed"
    assert journal.get(tid)["state"] == "failed"
    # The module never writes user files: the untouched file is untouched.
    assert target.read_text(encoding="utf-8") == "old\n"


def test_reconcile_reports_a_file_edited_after_the_crash_as_changed_since(tmp_path):
    """The newer edit must be surfaced, never rounded to completed or repaired."""
    target = tmp_path / "doc.txt"
    target.write_text("old\n", encoding="utf-8")
    journal = _journal(tmp_path)

    tid = journal.begin([_entry(target, "replace", _digest(b"old\n"),
                                _digest(b"new\n"))])
    journal.mark_committing(tid)
    journal.mark_entry(tid, str(target), "written",
                       after_sha256=_digest(b"new\n"))

    # Someone edits the file after the crash, before the restart reconciles.
    target.write_text("edited by a person\n", encoding="utf-8")

    report = journal.reconcile(sha256_of_file)[0]

    verdict = report["entries"][0]["verdict"]
    assert verdict == "changed_since"
    assert verdict != "completed"
    assert report["summary"]["changed_since"] == 1
    assert report["summary"]["completed"] == 0
    assert "edited after the crash" in report["entries"][0]["detail"]
    assert "must not be overwritten" in report["entries"][0]["detail"]
    assert target.read_text(encoding="utf-8") == "edited by a person\n"
    # A mixed/indeterminate outcome is not silently committed.
    assert journal.get(tid)["state"] == "unknown"


def test_reconcile_reports_unknown_without_recorded_hashes_and_never_completed(tmp_path):
    """No hashes means no proof; "unknown" is the only honest verdict."""
    target = tmp_path / "doc.txt"
    target.write_text("some content\n", encoding="utf-8")
    journal = _journal(tmp_path)

    tid = journal.begin([_entry(target, "replace", None, None)])
    journal.mark_committing(tid)

    report = journal.reconcile(sha256_of_file)[0]

    assert report["entries"][0]["verdict"] == "unknown"
    assert report["summary"]["unknown"] == 1
    assert report["summary"]["completed"] == 0
    assert report["state"] == "unknown"
    assert journal.get(tid)["state"] == "unknown"


def test_reconcile_treats_an_unreadable_file_as_unknown_not_absent(tmp_path):
    """An unreadable file is doubt, and a deleted file is proof. Not the same."""
    directory = tmp_path / "a-folder"
    directory.mkdir()
    journal = _journal(tmp_path)

    # A delete whose after-state is absence. If the callback's failure were
    # read as "absent", this would falsely reconcile as completed.
    tid = journal.begin([_entry(directory, "delete", _digest(b"x"), None)])
    journal.mark_committing(tid)

    report = journal.reconcile(sha256_of_file)[0]

    assert report["entries"][0]["verdict"] == "unknown"
    assert report["state"] == "unknown"


def test_reconcile_classifies_a_completed_delete_as_completed(tmp_path):
    """Absence is a legitimate proof of completion when a delete recorded it."""
    target = tmp_path / "gone.txt"       # never created: already absent
    journal = _journal(tmp_path)

    tid = journal.begin([_entry(target, "delete", _digest(b"old\n"), None)])
    journal.mark_committing(tid)

    report = journal.reconcile(sha256_of_file)[0]

    assert report["entries"][0]["verdict"] == "completed"
    assert report["state"] == "committed"


def test_a_mixed_transaction_reports_every_entry_and_stays_unknown(tmp_path):
    """Half-applied is a person's call, so the state is not rounded either way."""
    written = tmp_path / "done.txt"
    written.write_text("new\n", encoding="utf-8")
    untouched = tmp_path / "skipped.txt"
    untouched.write_text("old\n", encoding="utf-8")
    edited = tmp_path / "edited.txt"
    edited.write_text("someone else\n", encoding="utf-8")
    journal = _journal(tmp_path)

    tid = journal.begin([
        _entry(written, "replace", _digest(b"old\n"), _digest(b"new\n")),
        _entry(untouched, "replace", _digest(b"old\n"), _digest(b"new\n")),
        _entry(edited, "replace", _digest(b"old\n"), _digest(b"new\n")),
    ])
    journal.mark_committing(tid)

    report = journal.reconcile(sha256_of_file)[0]

    verdicts = {row["path"]: row["verdict"] for row in report["entries"]}
    assert verdicts[str(written)] == "completed"
    assert verdicts[str(untouched)] == "not_applied"
    assert verdicts[str(edited)] == "changed_since"
    assert report["summary"] == {"completed": 1, "not_applied": 1,
                                 "changed_since": 1, "unknown": 0}
    assert report["state"] == "unknown"


def test_reconcile_leaves_finished_transactions_out_of_the_report(tmp_path):
    journal = _journal(tmp_path)
    tid = journal.begin([_entry(tmp_path / "x.txt", "create", None,
                                _digest(b"x"))])
    journal.mark_committed(tid)
    assert journal.reconcile(sha256_of_file) == []


def test_mark_recovered_is_a_separate_decision_from_classifying(tmp_path):
    target = tmp_path / "doc.txt"
    target.write_text("old\n", encoding="utf-8")
    journal = _journal(tmp_path)
    tid = journal.begin([_entry(target, "replace", _digest(b"old\n"),
                                _digest(b"new\n"))])
    journal.mark_committing(tid)

    journal.reconcile(sha256_of_file)
    # Reconcile classified it failed; only a caller may call it recovered.
    assert journal.get(tid)["state"] == "failed"
    recovered = journal.mark_recovered(tid, "the person chose to drop it")
    assert recovered["state"] == "recovered"
    assert "drop it" in recovered["note"]


# -- the record itself -----------------------------------------------------

def test_begin_refuses_two_operations_on_one_path(tmp_path):
    journal = _journal(tmp_path)
    target = tmp_path / "doc.txt"
    with pytest.raises(ValueError) as excinfo:
        journal.begin([_entry(target, "create", None, _digest(b"a")),
                       _entry(target, "append", _digest(b"a"), _digest(b"ab"))])
    assert "more than once" in str(excinfo.value)


def test_mark_entry_refuses_a_path_the_transaction_does_not_record(tmp_path):
    journal = _journal(tmp_path)
    tid = journal.begin([_entry(tmp_path / "doc.txt", "create", None,
                                _digest(b"x"))])
    with pytest.raises(KeyError):
        journal.mark_entry(tid, str(tmp_path / "other.txt"), "written")


def test_a_corrupt_journal_raises_instead_of_looking_empty(tmp_path):
    """An unreadable crash record must not be read as "nothing was interrupted"."""
    journal = _journal(tmp_path)
    journal.path.write_text("{not json", encoding="utf-8")

    with pytest.raises(JournalCorrupted):
        journal.unfinished()
    # The damaged file is left in place for a person to inspect.
    assert journal.path.read_text(encoding="utf-8") == "{not json"


# -- retention --------------------------------------------------------------

def test_prune_bounds_stored_transactions(tmp_path):
    journal = _journal(tmp_path)
    ids = []
    for index in range(10):
        tid = journal.begin([_entry(tmp_path / f"f{index}.txt", "create",
                                    None, _digest(b"x"))])
        journal.mark_committed(tid)
        ids.append(tid)

    removed = journal.prune(keep=3)

    assert removed == 7
    stored = json.loads(journal.path.read_text(encoding="utf-8"))
    remaining = [record["transaction_id"] for record in stored["transactions"]]
    assert remaining == ids[-3:], "the newest transactions are the ones kept"


def test_prune_never_drops_an_unfinished_transaction(tmp_path):
    """An open question about disk survives however old it is."""
    journal = _journal(tmp_path)
    old_open = journal.begin([_entry(tmp_path / "open.txt", "replace",
                                     None, _digest(b"x"))])
    journal.mark_committing(old_open)
    for index in range(5):
        tid = journal.begin([_entry(tmp_path / f"done{index}.txt", "create",
                                    None, _digest(b"y"))])
        journal.mark_committed(tid)

    journal.prune(keep=1)

    assert journal.get(old_open) is not None
    assert [r["transaction_id"] for r in journal.unfinished()] == [old_open]
    # Only the newest finished transaction remains besides the open one.
    stored = json.loads(journal.path.read_text(encoding="utf-8"))
    assert len(stored["transactions"]) == 2


def test_the_journal_file_holds_no_file_contents(tmp_path):
    """Only hashes and status: a state record is not a second copy of the data."""
    journal = _journal(tmp_path)
    target = tmp_path / "secret.txt"
    target.write_text("do not copy me into the journal", encoding="utf-8")
    journal.begin([{"path": str(target), "op": "replace",
                    "before_sha256": None, "after_sha256": _digest(b"x"),
                    "original_b64": "ZG8gbm90IGNvcHkgbWU="}])

    text = journal.path.read_text(encoding="utf-8")
    assert "original_b64" not in text
    assert "do not copy me" not in text