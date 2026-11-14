"""PermissionLease: a bounded authorization, and every way it must refuse.

Most of these tests are about refusal, because the danger in an autonomy system
is a lease that quietly authorizes more than it was granted for.
"""

from __future__ import annotations

import threading
import time

import pytest

from jarvis.security.leases import LeaseRefusal, LeaseService


@pytest.fixture()
def leases():
    from jarvis.db.database import get_db

    return LeaseService(get_db())


def _issue(leases, **kwargs):
    defaults = dict(mission_id="MIS-1", session_id="S1", capability="write_file",
                    scope=["/tmp/proj"], ttl_seconds=60, max_uses=1,
                    approval_ref="CONF-1")
    defaults.update(kwargs)
    return leases.issue(**defaults)


# -- the happy path ---------------------------------------------------------

def test_a_lease_permits_a_use_inside_its_scope(leases):
    lease = _issue(leases, scope=["/tmp/proj"])
    spent = leases.consume(lease.lease_id, capability="write_file",
                           resource="/tmp/proj/README.md", session_id="S1")
    assert spent.uses == 1
    assert spent.remaining_uses == 0


def test_two_uses_are_permitted_when_the_lease_grants_two(leases):
    lease = _issue(leases, max_uses=2, scope=["/tmp/proj"])
    leases.consume(lease.lease_id, resource="/tmp/proj/a", session_id="S1")
    second = leases.consume(lease.lease_id, resource="/tmp/proj/b", session_id="S1")
    assert second.remaining_uses == 0


# -- refusals ---------------------------------------------------------------

def test_an_exhausted_lease_is_refused(leases):
    lease = _issue(leases, max_uses=1, scope=["/tmp/proj"])
    leases.consume(lease.lease_id, resource="/tmp/proj/a", session_id="S1")
    with pytest.raises(LeaseRefusal) as excinfo:
        leases.consume(lease.lease_id, resource="/tmp/proj/b", session_id="S1")
    assert "no uses left" in str(excinfo.value)


def test_an_expired_lease_is_refused(leases):
    lease = _issue(leases, ttl_seconds=0.05, scope=["/tmp/proj"])
    time.sleep(0.1)
    with pytest.raises(LeaseRefusal) as excinfo:
        leases.consume(lease.lease_id, resource="/tmp/proj/a", session_id="S1")
    assert "expired" in str(excinfo.value)


def test_a_revoked_lease_is_refused_immediately(leases):
    lease = _issue(leases, scope=["/tmp/proj"])
    leases.revoke(lease.lease_id, reason="the operator changed their mind")
    with pytest.raises(LeaseRefusal) as excinfo:
        leases.consume(lease.lease_id, resource="/tmp/proj/a", session_id="S1")
    assert "revoked" in str(excinfo.value)


def test_a_resource_outside_the_scope_is_refused(leases):
    lease = _issue(leases, scope=["/tmp/proj"])
    with pytest.raises(LeaseRefusal) as excinfo:
        leases.consume(lease.lease_id, resource="/tmp/other/file", session_id="S1")
    assert "outside the lease's scope" in str(excinfo.value)


def test_a_sibling_prefix_is_not_inside_the_scope(leases):
    """`/tmp/proj2` must not satisfy a lease for `/tmp/proj`."""
    lease = _issue(leases, scope=["/tmp/proj"])
    with pytest.raises(LeaseRefusal):
        leases.consume(lease.lease_id, resource="/tmp/proj2/secret", session_id="S1")


def test_a_lease_with_no_scope_authorizes_nothing(leases):
    """The dangerous default is "unrestricted"; this refuses instead."""
    lease = _issue(leases, scope=[])
    with pytest.raises(LeaseRefusal) as excinfo:
        leases.consume(lease.lease_id, resource="/tmp/proj/a", session_id="S1")
    assert "declares no resource scope" in str(excinfo.value)


def test_a_different_capability_is_refused(leases):
    lease = _issue(leases, capability="write_file", scope=["/tmp/proj"])
    with pytest.raises(LeaseRefusal) as excinfo:
        leases.consume(lease.lease_id, capability="delete_file",
                       resource="/tmp/proj/a", session_id="S1")
    assert "authorizes 'write_file'" in str(excinfo.value)


def test_a_different_session_is_refused(leases):
    lease = _issue(leases, session_id="S1", scope=["/tmp/proj"])
    with pytest.raises(LeaseRefusal) as excinfo:
        leases.consume(lease.lease_id, resource="/tmp/proj/a", session_id="S2")
    assert "different session" in str(excinfo.value)


def test_a_lease_for_an_old_plan_revision_is_refused(leases):
    lease = _issue(leases, scope=["/tmp/proj"], plan_revision=4)
    with pytest.raises(LeaseRefusal) as excinfo:
        leases.consume(lease.lease_id, resource="/tmp/proj/a", session_id="S1",
                       plan_revision=5)
    assert "plan changed" in str(excinfo.value)


# -- concurrency ------------------------------------------------------------

def test_two_threads_cannot_spend_one_remaining_use(leases):
    """The single-use case is exactly the one a race would break."""
    lease = _issue(leases, max_uses=1, scope=["/tmp/proj"])
    outcomes = []

    def spend():
        try:
            leases.consume(lease.lease_id, resource="/tmp/proj/a", session_id="S1")
            outcomes.append("spent")
        except LeaseRefusal:
            outcomes.append("refused")

    threads = [threading.Thread(target=spend) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["refused", "spent"], outcomes
    assert leases.get(lease.lease_id).uses == 1


# -- revocation surface -----------------------------------------------------

def test_revoking_a_mission_revokes_its_leases(leases):
    first = _issue(leases, mission_id="MIS-9", scope=["/tmp/proj"])
    second = _issue(leases, mission_id="MIS-9", scope=["/tmp/proj"])
    other = _issue(leases, mission_id="MIS-10", scope=["/tmp/proj"])

    assert leases.revoke_mission("MIS-9", reason="cancelled") == 2
    assert leases.get(first.lease_id).revoked
    assert leases.get(second.lease_id).revoked
    assert not leases.get(other.lease_id).revoked


def test_active_lists_only_usable_leases(leases):
    live = _issue(leases, mission_id="MIS-live", scope=["/tmp/proj"])
    dead = _issue(leases, mission_id="MIS-dead", scope=["/tmp/proj"])
    leases.revoke(dead.lease_id)

    ids = [lease.lease_id for lease in leases.active()]
    assert live.lease_id in ids
    assert dead.lease_id not in ids


# -- issuing ----------------------------------------------------------------

def test_a_lease_needs_a_mission(leases):
    with pytest.raises(ValueError):
        leases.issue(mission_id="", scope=["/tmp/x"])


def test_a_lease_must_have_a_positive_ttl(leases):
    with pytest.raises(ValueError):
        leases.issue(mission_id="M", scope=["/tmp/x"], ttl_seconds=0)


def test_a_lease_must_permit_at_least_one_use(leases):
    with pytest.raises(ValueError):
        leases.issue(mission_id="M", scope=["/tmp/x"], max_uses=0)