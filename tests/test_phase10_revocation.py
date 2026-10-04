"""Phase 10 P1-2: revocation must not depend on wall-clock second granularity.

Phase 9 reproduced that `PermissionEngine.revoke()` silently failed when the
grant and the revocation landed in the same clock second: `granted_at` defaults
to `datetime('now')` (one-second granularity), and the lookup used
`ORDER BY granted_at DESC LIMIT 1`, a non-deterministic key on ties, which
returned the grant row. The revocation row was written, counted, and never read
-- so a risk-3 destructive tool stayed auto-executing forever.

Phase 9 isolated the mechanism by showing that sleeping 1.2 s between the two
calls made the same revocation work. These tests pin the ordering rule instead
of the clock.
"""

from __future__ import annotations

import time

import pytest
from jarvis.core.models import PlannedAction, PolicyDecision, RiskLevel


def _evaluate(engine, path="/tmp/jarvis-revoke-probe.txt"):
    return engine.evaluate(
        PlannedAction(step_id=1, tool="delete_file", params={"path": path},
                      device="laptop_main")
    )


def _clear(db, tool="delete_file"):
    db.execute("DELETE FROM permissions WHERE tool = ?", (tool,))


def test_grant_then_immediate_revoke_removes_authority(core):
    """The original failure: grant and revoke microseconds apart."""
    engine = core.policy
    _clear(engine.db)
    try:
        assert _evaluate(engine).decision is PolicyDecision.CONFIRM

        engine.grant("delete_file", device="laptop_main")
        assert _evaluate(engine).decision is PolicyDecision.EXECUTE

        engine.revoke("delete_file", device="laptop_main")

        assert engine._is_revoked("delete_file", "laptop_main") is True
        assert engine.is_granted("delete_file", "laptop_main") is False
        assert _evaluate(engine).decision is PolicyDecision.BLOCK
    finally:
        _clear(engine.db)


def test_revoke_wins_inside_a_single_clock_second(core):
    """Ten grant/revoke pairs in one second must all end revoked."""
    engine = core.policy
    _clear(engine.db)
    try:
        deadline = time.monotonic() + 0.6  # comfortably within one datetime('now') tick
        i = 0
        while time.monotonic() < deadline:
            i += 1
            _clear(engine.db)
            engine.grant("delete_file", device="laptop_main")
            engine.revoke("delete_file", device="laptop_main")
            assert engine._is_revoked("delete_file", "laptop_main") is True, f"iteration {i}"
            assert _evaluate(engine).decision is PolicyDecision.BLOCK, f"iteration {i}"
        assert i >= 3, f"loop was too short to exercise the tie ({i} iterations)"
    finally:
        _clear(engine.db)


def test_grant_and_revoke_share_one_granted_at_value(core):
    """The exact data shape that used to tie, asserted directly on the rows."""
    engine = core.policy
    _clear(engine.db)
    try:
        engine.grant("delete_file", device="laptop_main")
        engine.revoke("delete_file", device="laptop_main")
        rows = engine.db.query(
            "SELECT granted, granted_at FROM permissions WHERE tool = 'delete_file' "
            "AND device = 'laptop_main' ORDER BY rowid"
        )
        assert len(rows) == 2
        assert rows[0]["granted"] == 1 and rows[1]["granted"] == 0
        # Same-second default ⇒ the two statements carry an identical timestamp.
        assert rows[0]["granted_at"] == rows[1]["granted_at"], (
            "test premise changed: granted_at is no longer tied, so the ordering "
            "regression this file guards may no longer reproduce"
        )
        assert engine._is_revoked("delete_file", "laptop_main") is True
    finally:
        _clear(engine.db)


def test_revoke_after_a_second_elapses_still_works(core):
    """The path Phase 9 used to prove the tie was the whole mechanism."""
    engine = core.policy
    _clear(engine.db)
    try:
        engine.grant("delete_file", device="laptop_main")
        time.sleep(1.1)
        engine.revoke("delete_file", device="laptop_main")
        assert _evaluate(engine).decision is PolicyDecision.BLOCK
    finally:
        _clear(engine.db)


def test_repeated_revoke_is_stable(core):
    engine = core.policy
    _clear(engine.db)
    try:
        engine.grant("delete_file", device="laptop_main")
        for _ in range(3):
            engine.revoke("delete_file", device="laptop_main")
            assert engine._is_revoked("delete_file", "laptop_main") is True
            assert _evaluate(engine).decision is PolicyDecision.BLOCK
        # A re-grant after revocation restores authority: newest row still wins.
        engine.grant("delete_file", device="laptop_main")
        assert _evaluate(engine).decision is PolicyDecision.EXECUTE
    finally:
        _clear(engine.db)


def test_revoke_without_a_prior_grant_blocks_the_tool(core):
    """Revoking something never granted is an explicit denial, not a no-op."""
    engine = core.policy
    _clear(engine.db)
    try:
        engine.revoke("delete_file", device="laptop_main")
        assert engine._is_revoked("delete_file", "laptop_main") is True
        assert _evaluate(engine).decision is PolicyDecision.BLOCK
    finally:
        _clear(engine.db)


def test_revocation_of_one_device_does_not_leak_to_another(core):
    engine = core.policy
    _clear(engine.db)
    try:
        engine.grant("delete_file", device="laptop_main")
        engine.revoke("delete_file", device="phone_main")
        # The laptop grant is the newest statement for that (tool, device) pair.
        assert engine.is_granted("delete_file", "laptop_main") is True
        assert _evaluate(engine).decision is PolicyDecision.EXECUTE
    finally:
        _clear(engine.db)


def test_device_agnostic_grant_stays_inert(core):
    """The half of Phase 10's scope decision that still stands, and the half that did not.

    `grant(tool)` writes device = NULL, and a NULL grant stays inert for a concrete
    device: promoting it to a wildcard would extend a standing grant to every device the
    user never named. That remains deliberate.

    The revocation direction was reversed in Phase 49, and the asymmetry is why: leaving
    a NULL *denial* inert meant `revoke(tool)` -- the only form the dashboard and the API
    can call -- revoked nothing, silently. Widening a denial can only remove authority;
    widening a grant can add it. Same row shape, opposite risk.
    """
    engine = core.policy
    _clear(engine.db)
    try:
        engine.grant("delete_file")  # device=None
        assert engine.is_granted("delete_file", "laptop_main") is False
        assert _evaluate(engine).decision is PolicyDecision.CONFIRM

        engine.revoke("delete_file")  # device=None
        assert engine._is_revoked("delete_file", "laptop_main") is True
        assert _evaluate(engine).decision is PolicyDecision.BLOCK
    finally:
        _clear(engine.db)


def test_standing_grant_revoke_cycle_keeps_risk_tier_metadata(core):
    """The fix must not have quietly changed what a grant is allowed to cover."""
    engine = core.policy
    spec = core.tools.get("delete_file")
    assert int(spec.risk) == int(RiskLevel.DESTRUCTIVE)
    assert spec.requires_confirmation is True
    _clear(engine.db)
    try:
        engine.grant("delete_file", device="laptop_main")
        assert _evaluate(engine).reason == "covered by a standing grant"
        engine.revoke("delete_file", device="laptop_main")
        assert "revoked" in _evaluate(engine).reason
    finally:
        _clear(engine.db)
