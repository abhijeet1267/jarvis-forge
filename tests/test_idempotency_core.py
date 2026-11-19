"""The idempotency store: one record per (scope, key), bound to its content.

These tests exercise the store directly against a real SQLite file. They assert
the properties the route wiring will rely on:

* a repeat of the identical request is ``existing`` (not a second execution);
* a key reused for different content is ``conflict`` (never a silent reuse);
* a key is invisible outside its scope;
* the atomic claim admits exactly one winner under real concurrency.

Concurrency is exercised with one ``Database`` connection per thread -- not a
shared handle -- so the guarantee under test is SQLite's unique-index arbitration
(``INSERT ... ON CONFLICT DO NOTHING`` plus its rowcount), not the Python lock
that guards a single handle.
"""

from __future__ import annotations

import threading

import pytest

from jarvis.core.idempotency import (
    CONFLICT,
    EXISTING,
    NEW,
    STATES,
    IdempotencyStore,
    fingerprint_request,
)
from jarvis.db.database import Database


@pytest.fixture()
def store(tmp_path):
    return IdempotencyStore(Database(tmp_path / "idempotency.db"))


def _fp(text: str = "list the workspaces") -> str:
    return fingerprint_request({"text": text, "source": "api"})


# ---------------------------------------------------------------------------
# 1. A repeat of the identical request is "existing", not a new claim.
# ---------------------------------------------------------------------------

def test_same_scope_key_and_fingerprint_is_existing_with_the_same_mission(store):
    first = store.claim("user", "K-1", _fp())
    assert first.status == NEW
    assert first.is_new

    store.update_state("user", "K-1", "executing", mission_id="MIS-42")

    second = store.claim("user", "K-1", _fp())
    assert second.status == EXISTING
    assert second.is_existing
    assert second.record.mission_id == "MIS-42"
    assert second.record.fingerprint == _fp()


# ---------------------------------------------------------------------------
# 2. Reusing a key for different content is a conflict.
# ---------------------------------------------------------------------------

def test_same_key_with_a_different_fingerprint_is_a_conflict(store):
    first = store.claim("user", "K-1", _fp("list the workspaces"))
    assert first.status == NEW

    second = store.claim("user", "K-1", _fp("delete the workspaces"))
    assert second.status == CONFLICT
    assert second.is_conflict
    # The stored row still describes the *original* request, untouched.
    assert second.record.fingerprint == _fp("list the workspaces")


# ---------------------------------------------------------------------------
# 3. Different keys with the same content are independent claims.
# ---------------------------------------------------------------------------

def test_different_keys_with_the_same_fingerprint_are_both_new(store):
    a = store.claim("user", "K-1", _fp())
    b = store.claim("user", "K-2", _fp())
    assert a.status == NEW
    assert b.status == NEW
    assert store.get("user", "K-1") is not None
    assert store.get("user", "K-2") is not None


# ---------------------------------------------------------------------------
# 4. Scope isolation: a key never leaks across actors.
# ---------------------------------------------------------------------------

def test_scope_isolation_a_key_in_one_scope_is_invisible_in_another(store):
    assert store.claim("alice", "K-shared", _fp()).status == NEW
    store.update_state("alice", "K-shared", "completed", mission_id="MIS-A")

    # Same key string, different actor: a separate, unused claim.
    other = store.claim("bob", "K-shared", _fp())
    assert other.status == NEW
    assert other.record.mission_id == ""

    assert store.get("alice", "K-shared").mission_id == "MIS-A"
    assert store.get("bob", "K-shared").mission_id == ""
    # And a scope that never saw the key gets nothing.
    assert store.get("carol", "K-shared") is None


# ---------------------------------------------------------------------------
# 5. State updates persist, and an unknown state is refused.
# ---------------------------------------------------------------------------

def test_update_state_persists_and_rejects_unknown_names(store):
    store.claim("user", "K-1", _fp())
    last = ""
    for state in sorted(STATES):
        record = store.update_state("user", "K-1", state)
        assert record.state == state
        assert store.get("user", "K-1").state == state
        last = state

    with pytest.raises(ValueError):
        store.update_state("user", "K-1", "finished")  # not a real state

    # The refusal did not write anything.
    assert store.get("user", "K-1").state == last

    with pytest.raises(LookupError):
        store.update_state("user", "K-missing", "executing")


# ---------------------------------------------------------------------------
# 6. Concurrency: exactly one "new" per unused key.
# ---------------------------------------------------------------------------

def test_concurrent_claims_of_one_unused_key_produce_exactly_one_new(tmp_path):
    path = tmp_path / "race.db"
    # Create the file and schema once before the threads race on it.
    IdempotencyStore(Database(path)).claim("seed", "seed", _fp())

    width = 8
    ready = threading.Barrier(width)
    results: list[str] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker() -> None:
        # A private connection per thread, so the arbitration is the database's,
        # not a single Python handle's lock.
        db = Database(path)
        try:
            store = IdempotencyStore(db)
            ready.wait(timeout=10)
            claim = store.claim("user", "K-race", _fp())
            with lock:
                results.append(claim.status)
        except BaseException as exc:  # noqa: BLE001 -- surface, don't swallow
            with lock:
                errors.append(exc)
        finally:
            db.close()

    threads = [threading.Thread(target=worker) for _ in range(width)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert not errors, f"a concurrent claim raised: {errors!r}"
    assert len(results) == width
    assert results.count(NEW) == 1, results
    assert results.count(EXISTING) == width - 1, results
    assert results.count(CONFLICT) == 0, results

    check = IdempotencyStore(Database(path))
    assert check.get("user", "K-race") is not None


# ---------------------------------------------------------------------------
# 7. An unknown key reads back as None.
# ---------------------------------------------------------------------------

def test_get_unknown_key_returns_none(store):
    assert store.get("user", "never-claimed") is None
