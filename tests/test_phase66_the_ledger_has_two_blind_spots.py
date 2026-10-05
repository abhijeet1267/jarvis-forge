"""Phase 66: the ledger has two blind spots, and neither of them is on the page.

Measured on the delivered app's real database before any code changed:

    total rows in action_logs ......... 525
    /metrics  "sampled" ............... 500   (the newest 500, ORDER BY created_at DESC LIMIT 500)
    /metrics  "chain_checked" ......... 495   of verify_chain()'s 500 (ORDER BY created_at ASC LIMIT 500)

So the surface walks two different windows over the same table. The newest 25 rows are never
chain-verified, the oldest 25 are never counted in `by_result`, and nothing published says either.
A tamper that edits the *last* record -- the most likely one, a day later -- is inside the blind
spot: `verify_chain` does not reach it, so the panel still says "intact" about a ledger it has not
read. And `chain` carries no reason, so a red row tells an operator nothing about what broke.

These tests are written against that gap, not around it: the first one tampers with the newest
record, which is precisely what the bounded walk misses.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[1]
node = shutil.which("node")

import importlib
API = importlib.import_module("jarvis.api.app")
from jarvis.security import audit as audit_module

#: The status tokens this project writes. Anything else in that column is a defect or an edit,
#: and the point of naming the set here is that the surface has to say which it found.
KNOWN_RESULTS = {
    "success", "failure", "denied", "blocked", "awaiting_confirmation", "unverified",
    "no_action", "pending", "recovered", "clarification", "expired", "cancelled", "unknown",
}


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    """A real SQLite audit log in a database of its own, with more rows than the old window.

    `get_db()` is a process singleton and conftest points it at the session database, so a test
    that writes 525 rows there would both pollute every later module and count rows it did not
    write. `Database(path=...)` takes an explicit file and monkeypatch hands the singleton back,
    which is the rule this project learned the hard way twice: never reach into a shared object
    to make an assertion about it.
    """
    from jarvis.db import database as db_module

    fresh = db_module.Database(path=tmp_path / "jarvis.db")
    monkeypatch.setattr(db_module, "_DB", fresh)
    log = audit_module.AuditLog()
    log.path = tmp_path / "audit.jsonl"          # the JSONL mirror, too, stays in the tmp home
    for i in range(525):
        log.record(intent=f"row {i}", tool="p66_probe", result="success" if i % 7 else "failure")
    return log, tmp_path


def _isolated_audit(tmp_path, monkeypatch):
    """The same isolation for the one-off ledgers below."""
    from jarvis.db import database as db_module

    fresh = db_module.Database(path=tmp_path / "jarvis.db")
    monkeypatch.setattr(db_module, "_DB", fresh)
    log = audit_module.AuditLog()
    log.path = tmp_path / "audit.jsonl"
    return log


def _tamper_newest(db_path: Path) -> str:
    """Edit the LAST record, which is the one a bounded ASC walk never reaches."""
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    last = con.execute("SELECT log_id, result FROM action_logs ORDER BY created_at DESC, rowid DESC"
                       " LIMIT 1").fetchone()
    con.execute("UPDATE action_logs SET result = 'success__edited' WHERE log_id = ?",
                (last["log_id"],))
    con.commit()
    con.close()
    return last["log_id"]


# ---------------------------------------------------------------------------
# 1. coverage: verify the whole ledger, and say what was verified
# ---------------------------------------------------------------------------
def test_verification_reaches_the_newest_record(ledger):
    """Red before the fix: the default walk stopped at 500 rows and this file has 525.

    Tampering the last record is the most natural edit a week later, and it was invisible: the
    verifier reported ok=True with a "total" that counted only what it had walked.
    """
    log, home = ledger
    before = log.verify_chain()
    assert before["ok"] is True, before
    assert before["total"] == 525, f"only {before['total']} rows were walked out of 525"

    tampered = _tamper_newest(home / "jarvis.db")
    after = log.verify_chain()
    assert after["ok"] is False, f"the newest record {tampered} was edited and nothing reported it"
    assert any(tampered in str(b.get("log_id")) for b in after["break_details"]), after


def test_the_verifier_reports_what_it_checked_and_what_it_did_not(ledger):
    """A bounded walk is fine as long as it admits the bound."""
    log, _home = ledger
    bounded = log.verify_chain(limit=100)
    assert bounded["checked"] + bounded["breaks"] == 100
    assert bounded["verified_of"] == 525, "the total must be the ledger, not the window"
    assert bounded["unverified"] == 425
    whole = log.verify_chain()
    assert whole["unverified"] == 0 and whole["verified_of"] == 525


def test_a_break_carries_the_reason_on_the_surface(ledger):
    """`BROKEN` with no reason is a red row that cannot be acted on."""
    log, home = ledger
    _tamper_newest(home / "jarvis.db")
    state = log.verify_chain()
    assert state["reason"], state
    assert state["first_bad_log_id"], state


# ---------------------------------------------------------------------------
# 2. the values that are not statuses at all
# ---------------------------------------------------------------------------
def test_early_rows_that_stored_a_sentence_are_counted_whole_ledger_not_by_window(tmp_path, monkeypatch):
    """The two 2026-09-18 rows are older than the newest-500 window, so `by_result` never saw them.

    They are not renamed into a fake status and they are not dropped: they are counted under the
    honest label `legacy_message` and published as a number the operator can ask about.
    """
    log = _isolated_audit(tmp_path, monkeypatch)
    for i in range(520):
        log.record(intent=f"row {i}", tool="p66_probe", result="success")
    log.record(intent="open VS Code", tool="open_app",
               result="Opening Visual Studio Code on your macos machine.")

    summary = API._audit_summary(type("C", (), {"audit": log})())
    assert summary["by_result"]["legacy_message"] == 1, summary["by_result"]
    assert summary["unexpected_result_values"] == 1, summary
    assert set(summary["by_result"]) <= KNOWN_RESULTS | {"legacy_message"}, summary["by_result"]


def test_no_surface_invents_a_status_for_a_value_it_does_not_know(tmp_path, monkeypatch):
    """Guarding the guard: the classifier may bucket, it may not silently relabel as success."""
    log = _isolated_audit(tmp_path, monkeypatch)
    log.record(intent="x", tool="p66_probe", result="forged")
    summary = API._audit_summary(type("C", (), {"audit": log})())
    assert "forged" in summary["by_result"], summary["by_result"]
    assert summary["unexpected_result_values"] == 1
    assert summary["by_result"].get("success") is None, summary["by_result"]


# ---------------------------------------------------------------------------
# 3. resealing is a declared operation, not a quiet one
# ---------------------------------------------------------------------------
def test_seal_chain_without_force_still_refuses_to_touch_a_sealed_ledger(ledger):
    """Existing behaviour, kept: a sealed ledger is not re-hashed by a plain call."""
    log, _home = ledger
    assert log.seal_chain() == 0


def test_a_forced_reseal_leaves_the_ledger_verifying(ledger):
    """What the repair needs, and the property that makes it safe to offer: the result verifies."""
    log, home = ledger
    _tamper_newest(home / "jarvis.db")
    assert log.verify_chain()["ok"] is False
    sealed = log.seal_chain(force=True)
    assert sealed == 525, sealed          # what it re-hashed
    after = log.verify_chain()
    # 526, because the seal writes its own event row -- verified_of is the ledger, not a wish.
    assert after["ok"] is True and after["checked"] == 526, after
    assert after["verified_of"] == 526 and after["unverified"] == 0, after


def test_a_forced_reseal_is_recorded_as_an_event(tmp_path, monkeypatch):
    """Re-sealing destroys tamper evidence for everything before the moment it runs.

    The docstring has always said that; the point of this test is that the ledger itself carries
    the fact, so a later reader can see the chain was re-sealed on a date rather than inferring it.
    """
    log = _isolated_audit(tmp_path, monkeypatch)
    for i in range(3):
        log.record(intent=f"row {i}", tool="p66_probe", result="success")
    log.seal_chain(force=True)
    events = [r["intent"] for r in log.recent(limit=20)]
    assert any("re-sealed" in e or "reseal" in e for e in events), events


# ---------------------------------------------------------------------------
# 4. the shipped panel, run under node rather than grepped
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def chain_render(tmp_path_factory):
    """Run the page's own `auditChainLine` and report the text it writes."""
    if not node:
        pytest.skip("node not available to run the page's own code")
    body = re.search(r"<script>(.*?)</script>",
                     (REPO / "dashboard" / "index.html").read_text(), re.S).group(1)
    start = body.index("function auditChainLine")
    source = body[start:body.index("\n}", start) + 2]
    assert "chain_intact" in source, "the extracted function is not the chain line"
    root = tmp_path_factory.mktemp("p66")

    def _run(audit):
        script = root / "chain.js"
        script.write_text(source + "\nconsole.log(auditChainLine(%s));" % json.dumps(audit),
                          encoding="utf-8")
        out = subprocess_run(script)
        return out.strip()

    return _run


def subprocess_run(script: Path) -> str:
    import subprocess

    done = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return done.stdout


def test_the_panel_says_what_it_verified(chain_render):
    line = chain_render({"chain_intact": True, "chain_checked": 525, "chain_verified_of": 525,
                         "chain_unverified": 0, "chain_breaks": 0, "chain_reason": ""})
    assert "intact" in line and "525" in line, line


def test_the_panel_names_the_break_instead_of_only_colouring_it(chain_render):
    line = chain_render({"chain_intact": False, "chain_checked": 495, "chain_verified_of": 525,
                         "chain_unverified": 0, "chain_breaks": 5,
                         "chain_reason": "record hash mismatch (a record was edited)"})
    assert "BROKEN" in line, line
    assert "5" in line and "495" in line, line
    assert "edited" in line, f"the row carries a colour and no reason: {line}"


def test_the_unverified_tail_is_shown_instead_of_assumed(chain_render):
    """A window that stops early has to say so; silence here would read as 'all of it'."""
    line = chain_render({"chain_intact": True, "chain_checked": 100, "chain_verified_of": 525,
                         "chain_unverified": 425, "chain_breaks": 0, "chain_reason": ""})
    assert "100 of 525" in line and "not verified" in line, line
