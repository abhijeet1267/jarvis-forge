"""Capability Passports: standing as a record, never as a credential.

The failure mode of a "capability passport" is that a descriptive document
quietly becomes an authorization: a screen that says "this worker may write
files" starts being read by the gate. So the tests that matter here are the
negative ones -- the passport grants nothing, it is not read by the policy
engine, and it states whether the log it was built from re-walks clean.
"""

from __future__ import annotations

import pytest

from jarvis.core.models import ToolSpec, RiskLevel
from jarvis.core.passports import (
    MEASURED_AFTER,
    PassportError,
    build_passport,
    issue_passport,
    list_passports,
)
from jarvis.registry.tools import ToolRegistry
from jarvis.security.audit import AuditLog


class _Spec:
    """A minimal stand-in for a ToolSpec, so a test states its own registry."""

    def __init__(self, name, *, agent="tester", risk=RiskLevel.READ,
                 confirm=False, reversible=True):
        self.name = name
        self.agent = agent
        self.risk = risk
        self.requires_confirmation = confirm
        self.reversible = reversible
        self.allowed_devices = ("laptop_main",)


class _Registry:
    def __init__(self, specs):
        self._specs = list(specs)

    def all(self):
        return list(self._specs)


class _Audit:
    """A stand-in audit log: rows for `recent`, and a chain verdict."""

    def __init__(self, rows=(), *, ok=True, checked=0, breaks=0):
        self._rows = list(rows)
        self._ok = ok
        self._checked = checked
        self._breaks = breaks
        self.recorded = []

    def recent(self, limit=50):
        return self._rows[:limit]

    def verify_chain(self):
        return {"ok": self._ok, "checked": self._checked, "breaks": self._breaks}

    def record(self, **kwargs):
        self.recorded.append(kwargs)

        class _Record:
            log_id = "LOG-test"

        return _Record()


def _row(tool, *, risk="read", confirmation=0, result="success"):
    return {"tool": tool, "risk": risk, "confirmation": confirmation,
            "result": result}


# -- what a passport states -------------------------------------------------

def test_a_passport_names_the_tools_the_registry_gives_the_worker():
    registry = _Registry([
        _Spec("read_file"),
        _Spec("write_file", risk=RiskLevel.EXTERNAL, confirm=True),
        _Spec("someone_elses_tool", agent="other"),
    ])
    passport = build_passport("tester", registry=registry, audit=_Audit())
    assert [t["tool"] for t in passport.tools] == ["write_file", "read_file"]
    # Costliest first: the destructive/external reach is what a reader needs.
    assert passport.tools[0]["risk"] == "external"


def test_gated_tools_are_the_ones_that_are_not_plain_reads():
    registry = _Registry([
        _Spec("read_file"),
        _Spec("write_file", risk=RiskLevel.EXTERNAL, confirm=True),
    ])
    passport = build_passport("tester", registry=registry, audit=_Audit())
    assert passport.gated_tools == ["write_file"]


def test_a_tool_the_log_never_saw_is_listed_as_never_used():
    registry = _Registry([_Spec("read_file"), _Spec("delete_file",
                                                   risk=RiskLevel.DESTRUCTIVE)])
    audit = _Audit([_row("read_file")])
    passport = build_passport("tester", registry=registry, audit=audit)
    assert passport.exercised == ["read_file"]
    assert passport.never_used == ["delete_file"]
    assert any("never used" in note and "delete_file" in note
               for note in passport.notes)


def test_a_read_only_never_used_tool_is_not_alarmist():
    registry = _Registry([_Spec("read_file"), _Spec("list_dir")])
    passport = build_passport("tester", registry=registry, audit=_Audit())
    # Both are never used, but neither is risky, so no "reach is theoretical" line.
    assert passport.never_used == ["list_dir", "read_file"]
    assert not any("reach is theoretical" in note for note in passport.notes)


# -- counts come from the log, not from optimism ---------------------------

def test_counts_are_read_from_the_audit_rows():
    registry = _Registry([_Spec("write_file", risk=RiskLevel.EXTERNAL)])
    audit = _Audit([
        _row("write_file", risk="external", confirmation=1),
        _row("write_file", risk="external", confirmation=1, result="denied"),
        _row("write_file", risk="external"),
    ])
    passport = build_passport("tester", registry=registry, audit=audit)
    assert passport.actions_total == 3
    assert passport.actions_by_risk == {"external": 3}
    assert passport.confirmations == 2
    assert passport.refusals == 1


def test_rows_for_another_workers_tool_are_ignored():
    registry = _Registry([_Spec("read_file")])
    audit = _Audit([_row("read_file"), _row("send_email", risk="destructive")])
    passport = build_passport("tester", registry=registry, audit=audit)
    assert passport.actions_total == 1


def test_no_actions_is_stated_not_left_blank():
    registry = _Registry([_Spec("read_file")])
    passport = build_passport("tester", registry=registry, audit=_Audit())
    assert passport.actions_total == 0
    assert any("no recorded actions" in note for note in passport.notes)


def test_a_thin_history_says_it_is_thin():
    registry = _Registry([_Spec("read_file")])
    audit = _Audit([_row("read_file") for _ in range(MEASURED_AFTER - 1)])
    passport = build_passport("tester", registry=registry, audit=audit)
    assert any("thin history" in note for note in passport.notes)


def test_a_measured_history_does_not_become_a_score():
    registry = _Registry([_Spec("read_file")])
    audit = _Audit([_row("read_file") for _ in range(MEASURED_AFTER + 2)])
    passport = build_passport("tester", registry=registry, audit=audit)
    body = passport.to_dict()
    # Counts, but never an aggregate rating nobody could verify.
    assert body["actions_total"] == MEASURED_AFTER + 2
    for forbidden in ("score", "trust", "confidence", "rating"):
        assert forbidden not in body


# -- the evidence says whether the evidence is good -------------------------

def test_a_broken_chain_is_stated_on_the_passport():
    registry = _Registry([_Spec("read_file")])
    audit = _Audit([_row("read_file")], ok=False, checked=10, breaks=2)
    passport = build_passport("tester", registry=registry, audit=audit)
    assert passport.evidence["intact"] is False
    assert passport.evidence["breaks"] == 2
    assert any("did not re-walk clean" in note for note in passport.notes)


def test_an_intact_chain_says_nothing_alarming():
    registry = _Registry([_Spec("read_file")])
    audit = _Audit([_row("read_file")], ok=True, checked=1)
    passport = build_passport("tester", registry=registry, audit=audit)
    assert passport.evidence["intact"] is True
    assert not any("did not re-walk clean" in note for note in passport.notes)


# -- it grants nothing -----------------------------------------------------

def test_a_passport_states_that_it_grants_nothing():
    registry = _Registry([_Spec("write_file", risk=RiskLevel.EXTERNAL)])
    body = build_passport("tester", registry=registry, audit=_Audit()).to_dict()
    assert "grants nothing" in body["authority"]


def test_an_unknown_worker_is_an_error_not_an_empty_passport():
    with pytest.raises(PassportError):
        build_passport("nobody", registry=_Registry([_Spec("read_file")]),
                       audit=_Audit())


def test_issuing_a_passport_does_not_change_what_the_gate_decides():
    """Measured: the same action is evaluated before and after a passport is
    built and issued, and the decision is identical. A passport that moved a
    gate would be a credential wearing a document's clothes.

    The earlier version of this test grepped the policy module for the word
    "passport", which passes on any import that never runs -- the same shape of
    check this repo has been burned by before.
    """
    from jarvis.core.models import PlannedAction
    from jarvis.registry.devices import DeviceRegistry
    from jarvis.security.policy import PermissionEngine
    from jarvis.tools import build_default_registry

    engine = PermissionEngine(build_default_registry(), DeviceRegistry())
    action = PlannedAction(tool="write_file",
                           params={"path": "/tmp/passport-gate.txt",
                                   "content": "x", "mode": "overwrite"})

    before = engine.evaluate(action)
    passport = issue_passport("laptop")
    after = engine.evaluate(action)

    assert passport["worker"] == "laptop"
    assert (after.decision, after.reason) == (before.decision, before.reason)


# -- issuing writes an audit entry, and only that ---------------------------

def test_issuing_records_an_audit_entry_that_grants_nothing():
    registry = _Registry([_Spec("read_file")])
    audit = _Audit([_row("read_file")])
    body = issue_passport("tester", registry=registry, audit=audit)
    assert audit.recorded, "issuing must leave a record"
    entry = audit.recorded[0]
    assert entry["intent"] == "issue capability passport"
    assert entry["target"] == "tester"
    assert entry["detail"]["grants"] == "nothing"
    assert body["audit_id"] == "LOG-test"


# -- the shipped registry ---------------------------------------------------

def test_the_shipped_registry_produces_passports_for_real_workers():
    """The default must be the populated registry -- a bare `ToolRegistry()`
    holds no tools, which made every passport read as "can do nothing"."""
    rows = list_passports()
    assert rows, "no workers in the shipped registry"
    assert all("error" not in row for row in rows), rows
    laptop = next(row for row in rows if row["worker"] == "laptop")
    assert laptop["tools"] > 10
    assert laptop["gated"] > 0


# -- over the wire -----------------------------------------------------------

def test_the_passport_is_served_over_http():
    from tests.conftest import TEST_TOKEN
    from fastapi.testclient import TestClient
    from jarvis.api.app import app

    client = TestClient(app, base_url="http://127.0.0.1:8765")
    headers = {"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"}
    body = client.get("/passports", headers=headers).json()
    assert body["workers"]
    assert "not a grant" in body["note"]


def test_a_worker_passport_is_served_over_http():
    from tests.conftest import TEST_TOKEN
    from fastapi.testclient import TestClient
    from jarvis.api.app import app

    client = TestClient(app, base_url="http://127.0.0.1:8765")
    headers = {"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"}
    response = client.get("/passports/laptop", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["worker"] == "laptop"
    assert body["tools"]
    assert "grants nothing" in body["authority"]


def test_an_unknown_worker_is_a_404_over_http():
    from tests.conftest import TEST_TOKEN
    from fastapi.testclient import TestClient
    from jarvis.api.app import app

    client = TestClient(app, base_url="http://127.0.0.1:8765")
    headers = {"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"}
    assert client.get("/passports/nobody", headers=headers).status_code == 404

def test_a_truncated_log_window_is_stated_not_presented_as_a_total():
    """Counts over a window are described as counts over a window."""
    from jarvis.core.passports import AUDIT_ROWS_READ

    registry = _Registry([_Spec("read_file")])
    audit = _Audit([_row("read_file") for _ in range(AUDIT_ROWS_READ)])
    passport = build_passport("tester", registry=registry, audit=audit)
    assert passport.window["complete"] is False
    assert passport.window["rows_read"] == AUDIT_ROWS_READ
    assert any("most recent of them" in note for note in passport.notes)


def test_a_complete_window_is_reported_as_complete():
    registry = _Registry([_Spec("read_file")])
    audit = _Audit([_row("read_file")])
    passport = build_passport("tester", registry=registry, audit=audit)
    assert passport.window["complete"] is True
    assert not any("most recent of them" in note for note in passport.notes)
