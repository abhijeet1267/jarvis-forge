"""Tests for session-scoped verified-entity memory (jarvis.voice.entities).

The memory complements the planner's refusal to guess a target from a vague
pronoun. It may only offer a concrete name when exactly one recent verified
entity exists -- so these tests care most about the three ways it must stay
silent: unexpired-only, verified-only, and session-isolated.
"""

from __future__ import annotations

import threading
from typing import List

import pytest

from jarvis.voice.entities import EntityMemory, Entry


class FakeClock:
    """Deterministic monotonic clock the tests advance by hand."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture()
def memory(clock: FakeClock) -> EntityMemory:
    return EntityMemory(max_age_seconds=60.0, max_entries=8, clock=clock)


# ---------------------------------------------------------------------------
# Verification state
# ---------------------------------------------------------------------------
def test_verified_entry_is_returned(memory: EntityMemory) -> None:
    memory.record("s1", "file", "/tmp/AMR_Report.pdf", verified=True, evidence="find")

    entry = memory.last_verified("s1")

    assert isinstance(entry, Entry)
    assert entry.kind == "file"
    assert entry.name == "/tmp/AMR_Report.pdf"
    assert entry.verified is True
    assert entry.evidence == "find"


def test_unverified_entry_is_stored_but_never_returned(memory: EntityMemory, clock: FakeClock) -> None:
    """A plan's guess is not an observation; it must not become a pronoun target."""
    memory.record("s1", "file", "/tmp/guessed.pdf", verified=False)
    assert memory.last_verified("s1") is None
    assert memory.last_verified_of_kind("s1", "file") is None

    # It is still stored, as an honest record, and shows in the snapshot.
    snapshot = memory.snapshot("s1")
    assert len(snapshot["entries"]) == 1
    assert snapshot["entries"][0]["verified"] is False


def test_unverified_latest_does_not_shadow_verified_older(memory: EntityMemory) -> None:
    """A newer unverified guess must not hide the older thing actually seen."""
    memory.record("s1", "file", "/tmp/verified.pdf", verified=True)
    memory.record("s1", "file", "/tmp/guess.pdf", verified=False)

    entry = memory.last_verified("s1")

    assert entry is not None
    assert entry.name == "/tmp/verified.pdf"


def test_last_verified_none_for_unknown_session(memory: EntityMemory) -> None:
    assert memory.last_verified("nobody") is None
    assert memory.last_verified_of_kind("nobody", "app") is None


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------
def test_entry_survives_up_to_max_age_and_expires_after(memory: EntityMemory, clock: FakeClock) -> None:
    memory.record("s1", "app", "Safari", verified=True)

    clock.advance(59.999)
    assert memory.last_verified("s1") is not None

    clock.advance(0.002)
    assert memory.last_verified("s1") is None


def test_expired_entry_is_also_hidden_from_kind_lookup_and_snapshot(
    memory: EntityMemory, clock: FakeClock
) -> None:
    memory.record("s1", "window", "Vantage", verified=True)
    clock.advance(61.0)

    assert memory.last_verified_of_kind("s1", "window") is None
    assert memory.snapshot("s1")["entries"] == []


def test_fresh_entry_survives_pruning_of_expired_siblings(memory: EntityMemory, clock: FakeClock) -> None:
    memory.record("s1", "app", "Old", verified=True)
    clock.advance(59.0)
    memory.record("s1", "app", "New", verified=True)
    clock.advance(2.0)  # "Old" is now 61s old, "New" is 2s old.

    entry = memory.last_verified("s1")

    assert entry is not None
    assert entry.name == "New"
    assert [e["name"] for e in memory.snapshot("s1")["entries"]] == ["New"]


# ---------------------------------------------------------------------------
# Kind filtering
# ---------------------------------------------------------------------------
def test_kind_filter_returns_latest_of_that_kind(memory: EntityMemory) -> None:
    memory.record("s1", "app", "Safari", verified=True)
    memory.record("s1", "file", "/tmp/a.pdf", verified=True)
    memory.record("s1", "app", "Notes", verified=True)

    app = memory.last_verified_of_kind("s1", "app")
    file_entry = memory.last_verified_of_kind("s1", "file")

    assert app is not None and app.name == "Notes"
    assert file_entry is not None and file_entry.name == "/tmp/a.pdf"
    assert memory.last_verified_of_kind("s1", "window") is None


def test_kind_is_normalised_but_name_is_preserved_exactly(memory: EntityMemory) -> None:
    memory.record("s1", "  File ", "  /tmp/name with spaces.pdf ", verified=True)

    entry = memory.last_verified_of_kind("s1", "FILE")

    assert entry is not None
    assert entry.kind == "file"
    assert entry.name == "  /tmp/name with spaces.pdf "


# ---------------------------------------------------------------------------
# Bound and eviction
# ---------------------------------------------------------------------------
def test_eviction_keeps_the_newest_max_entries(clock: FakeClock) -> None:
    memory = EntityMemory(max_age_seconds=600.0, max_entries=3, clock=clock)
    for index in range(5):
        memory.record("s1", "file", f"/tmp/{index}.pdf", verified=True)
        clock.advance(1.0)

    names = [entry["name"] for entry in memory.snapshot("s1")["entries"]]

    assert names == ["/tmp/2.pdf", "/tmp/3.pdf", "/tmp/4.pdf"]
    entry = memory.last_verified("s1")
    assert entry is not None and entry.name == "/tmp/4.pdf"


def test_eviction_is_per_session(memory: EntityMemory) -> None:
    """Filling one session to its bound must not evict another session."""
    for index in range(8):
        memory.record("busy", "file", f"/tmp/{index}.pdf", verified=True)
    memory.record("quiet", "app", "Safari", verified=True)
    memory.record("busy", "file", "/tmp/overflow.pdf", verified=True)

    quiet = memory.last_verified("quiet")

    assert quiet is not None and quiet.name == "Safari"
    assert len(memory.snapshot("busy")["entries"]) == 8


# ---------------------------------------------------------------------------
# Per-session isolation
# ---------------------------------------------------------------------------
def test_session_a_never_sees_session_b(memory: EntityMemory) -> None:
    memory.record("a", "app", "Safari", verified=True)
    memory.record("b", "file", "/tmp/secret.pdf", verified=True)

    a_entry = memory.last_verified("a")
    b_entry = memory.last_verified("b")

    assert a_entry is not None and a_entry.name == "Safari"
    assert b_entry is not None and b_entry.name == "/tmp/secret.pdf"
    assert [e["name"] for e in memory.snapshot("a")["entries"]] == ["Safari"]


def test_clear_forgets_only_that_session(memory: EntityMemory) -> None:
    memory.record("a", "app", "Safari", verified=True)
    memory.record("b", "app", "Notes", verified=True)

    memory.clear("a")

    assert memory.last_verified("a") is None
    assert memory.snapshot("a")["entries"] == []
    b_entry = memory.last_verified("b")
    assert b_entry is not None and b_entry.name == "Notes"


def test_clear_is_idempotent_and_recording_works_after(memory: EntityMemory) -> None:
    memory.clear("nobody")
    memory.record("a", "app", "Safari", verified=True)
    memory.clear("a")
    memory.clear("a")
    memory.record("a", "app", "Notes", verified=True)

    entry = memory.last_verified("a")

    assert entry is not None and entry.name == "Notes"


# ---------------------------------------------------------------------------
# Snapshot contract
# ---------------------------------------------------------------------------
def test_snapshot_is_json_safe(memory: EntityMemory) -> None:
    import json

    memory.record("s1", "file", "/tmp/a.pdf", verified=True, evidence="find")

    payload = memory.snapshot("s1")

    # Must round-trip through JSON without custom encoders.
    assert json.loads(json.dumps(payload)) == payload
    assert payload["session_id"] == "s1"
    assert payload["max_age_seconds"] == 60.0
    assert payload["max_entries"] == 8


# ---------------------------------------------------------------------------
# Constructor validation and thread safety
# ---------------------------------------------------------------------------
def test_constructor_rejects_nonsense_bounds() -> None:
    with pytest.raises(ValueError):
        EntityMemory(max_age_seconds=0)
    with pytest.raises(ValueError):
        EntityMemory(max_entries=0)


def test_record_rejects_empty_identity_fields(memory: EntityMemory) -> None:
    with pytest.raises(ValueError):
        memory.record("", "app", "Safari", verified=True)
    with pytest.raises(ValueError):
        memory.record("s1", "  ", "Safari", verified=True)
    with pytest.raises(ValueError):
        memory.record("s1", "app", "", verified=True)


def test_concurrent_records_stay_bounded_and_consistent(clock: FakeClock) -> None:
    """The lock must keep the bound exact and lookups well-formed under races."""
    memory = EntityMemory(max_age_seconds=600.0, max_entries=16, clock=clock)
    errors: List[BaseException] = []
    barrier = threading.Barrier(8)

    def worker(worker_id: int) -> None:
        try:
            barrier.wait(timeout=5)
            for index in range(50):
                memory.record("s1", "file", f"/tmp/{worker_id}-{index}.pdf", verified=True)
                entry = memory.last_verified("s1")
                assert entry is None or entry.verified
        except BaseException as exc:  # noqa: BLE001 - surfaced in the main thread
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    assert errors == []
    entries = memory.snapshot("s1")["entries"]
    assert len(entries) == 16
    assert all(entry["verified"] for entry in entries)