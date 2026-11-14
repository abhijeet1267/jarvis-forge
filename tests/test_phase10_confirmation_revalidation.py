"""Phase 10 P1-1: an approval must not outlive the authority it was granted under.

Phase 9 measured that ``resolve_confirmation()`` rebuilt the action from the
stored record and executed it without re-entering the permission engine, so
revoking ``delete_file`` after its confirmation existed still performed the
deletion. The fix re-runs the refusal gates at execution time via
``PermissionEngine.pre_execute_check()``.

The second half of this file exists because the controls that already passed in
Phase 9 -- replay, substitution, expiry, 128-bit ids -- must not have been paid
for to get the revalidation.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from jarvis.core.models import CommandRequest, PlannedAction, PolicyDecision


@pytest.fixture()
def engine(core):
    return core.policy


@pytest.fixture(autouse=True)
def _clean_permissions(core):
    """`core` is session-scoped, so a revoke here would otherwise poison later tests.

    Phase 9 recorded this shared-state coupling as a suite-hygiene finding; the
    guard is written down rather than discovered by the next reader.
    """
    db = core.policy.db
    db.execute("DELETE FROM permissions WHERE tool = 'delete_file'")
    yield
    db.execute("DELETE FROM permissions WHERE tool = 'delete_file'")


def _victim(tmp_path: Path, name: str = "victim.txt") -> Path:
    p = tmp_path / name
    p.write_text("PRECIOUS")
    return p


async def _confirm_deletion(core, path: Path):
    """Create a real pending confirmation for a delete of ``path``."""
    action = PlannedAction(
        step_id=1, tool="delete_file", params={"path": str(path)}, device="laptop_main"
    )
    verdict = core.policy.evaluate(action, session_id="s")
    assert verdict.decision is PolicyDecision.CONFIRM, verdict.reason
    return verdict.confirmation_id


# --------------------------------------------------------------------------
# revalidation: what Phase 9 could not stop
# --------------------------------------------------------------------------

async def test_revocation_after_confirmation_blocks_execution(core, engine, tmp_path):
    victim = _victim(tmp_path)
    cid = await _confirm_deletion(core, victim)

    engine.revoke("delete_file", device="laptop_main")
    response = await core.resolve_confirmation(cid, True, session_id="laptop", approver_session="laptop")

    assert response.decision is PolicyDecision.BLOCK
    assert response.actions == [], response.actions
    assert victim.exists(), "a revoked tool executed anyway"
    assert "no longer allowed" in response.reply
    assert "revoked" in response.reply


async def test_device_offline_after_confirmation_blocks_execution(core, engine, tmp_path):
    victim = _victim(tmp_path)
    cid = await _confirm_deletion(core, victim)

    device = core.devices.get("laptop_main")
    was_online = device.online
    device.online = False
    try:
        response = await core.resolve_confirmation(cid, True, session_id="laptop", approver_session="laptop")
    finally:
        device.online = was_online

    assert response.decision is PolicyDecision.BLOCK
    assert victim.exists(), "action ran against a device that had gone offline"
    assert "offline" in response.reply


async def test_protected_path_reached_by_record_edit_is_refused(core, engine, tmp_path):
    """The path guard is re-run, so a stored record cannot smuggle a protected path."""
    victim = _victim(tmp_path)
    cid = await _confirm_deletion(core, victim)

    engine.db.execute(
        "UPDATE confirmations SET params = ? WHERE confirmation_id = ?",
        ('{"path": "/etc/hosts"}', cid),
    )
    response = await core.resolve_confirmation(cid, True, session_id="laptop", approver_session="laptop")

    assert response.decision is PolicyDecision.BLOCK
    assert "protected" in response.reply.lower()


async def test_malformed_stored_params_are_refused(core, engine, tmp_path):
    victim = _victim(tmp_path)
    cid = await _confirm_deletion(core, victim)

    engine.db.execute(
        "UPDATE confirmations SET params = ? WHERE confirmation_id = ?",
        ("{}", cid),  # delete_file requires `path`
    )
    response = await core.resolve_confirmation(cid, True, session_id="laptop", approver_session="laptop")

    assert response.decision is PolicyDecision.BLOCK
    assert victim.exists()


async def test_blocked_resolution_is_audited(core, engine, tmp_path):
    victim = _victim(tmp_path)
    cid = await _confirm_deletion(core, victim)
    engine.revoke("delete_file", device="laptop_main")

    await core.resolve_confirmation(cid, True, session_id="laptop", approver_session="laptop")

    recent = core.audit.recent(20)
    stages = [r for r in recent if r.get("tool") == "delete_file"]
    assert any(str(r.get("result")) == "blocked" for r in stages), stages
    assert any(
        (r.get("detail") or {}).get("stage") == "pre_execute_check" for r in stages
    ), "the refusal must be attributable to revalidation, not to a generic failure"


async def test_a_still_allowed_action_still_executes(core, engine, tmp_path):
    """Positive control: revalidation must not become a blanket refusal."""
    victim = _victim(tmp_path)
    cid = await _confirm_deletion(core, victim)

    response = await core.resolve_confirmation(cid, True, session_id="laptop", approver_session="laptop")

    assert response.decision is PolicyDecision.EXECUTE
    assert not victim.exists() or victim.parent == tmp_path
    assert response.actions, response.reply


# --------------------------------------------------------------------------
# preservation: the Phase 9 PASSes that must still hold
# --------------------------------------------------------------------------

async def test_replay_still_refused(core, engine, tmp_path):
    victim = _victim(tmp_path, "replay.txt")
    cid = await _confirm_deletion(core, victim)

    first = await core.resolve_confirmation(cid, True, session_id="laptop", approver_session="laptop")
    assert first.decision is PolicyDecision.EXECUTE

    again = await core.resolve_confirmation(cid, True, session_id="laptop", approver_session="laptop")
    assert again.decision is PolicyDecision.BLOCK
    assert "already" in again.reply


async def test_substitution_still_impossible(core, engine, tmp_path):
    """Approving A must never run B: the action comes from the stored record."""
    victim = _victim(tmp_path, "a.txt")
    other = tmp_path / "b.txt"
    other.write_text("KEEP")

    cid = await _confirm_deletion(core, victim)
    response = await core.resolve_confirmation(cid, True, session_id="laptop", approver_session="laptop")

    assert response.actions[0].tool == "delete_file"
    assert str(victim) in (response.actions[0].message or "") or not victim.exists()
    assert other.exists(), "the approved confirmation was used to touch a different target"


async def test_expiry_still_wins(core, engine, tmp_path):
    victim = _victim(tmp_path, "expired.txt")
    core.settings.confirm_timeout = 1
    try:
        cid = await _confirm_deletion(core, victim)
        time.sleep(2.1)
        response = await core.resolve_confirmation(cid, True, session_id="laptop", approver_session="laptop")
    finally:
        core.settings.confirm_timeout = 300

    assert response.decision is PolicyDecision.BLOCK
    assert victim.exists(), "an expired approval executed"
    assert "expired" in response.reply


async def test_confirmation_id_is_128_bits(core, engine, tmp_path):
    victim = _victim(tmp_path, "entropy.txt")
    cid = await _confirm_deletion(core, victim)

    prefix, secret = cid.split("-", 1)
    assert prefix == "CONF"
    assert len(secret) == 32, secret
    assert all(c in "0123456789abcdef" for c in secret)


async def test_unknown_confirmation_id_runs_nothing(core, tmp_path):
    import secrets as _secrets

    victim = _victim(tmp_path, "guess.txt")
    response = await core.resolve_confirmation(
        "CONF-" + _secrets.token_hex(16), True, session_id="s"
    )

    assert response.actions == []
    assert victim.exists()
    assert "no record" in response.reply
