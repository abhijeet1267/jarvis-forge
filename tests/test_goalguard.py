"""Goal guard: bounded scheduling, the duplicate-loop guard, and honest records.

The two things these tests pin hardest are the ones a scheduler would get wrong
on its own: a goal must not be handed out twice for the same moment (or a
restart would double the polling), and an interval under 30 seconds must be
refused rather than quietly honoured. The observation tests use a real
``http.server`` because the point is that the existing observers are actually
called, not that a mock returned a string.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from jarvis.core.goalguard import (
    MIN_INTERVAL_SECONDS,
    Goal,
    GoalStore,
    GoalStoreError,
    run_due_observations,
)


# -- the opt-in is enforced ------------------------------------------------

def test_add_refuses_an_interval_under_thirty_seconds(tmp_path):
    """Monitoring is opt-in and bounded: a 1s cadence would be a polling loop."""
    store = GoalStore(tmp_path / "goals.json")
    with pytest.raises(ValueError) as excinfo:
        store.add({"kind": "file", "target": str(tmp_path / "a.txt"),
                   "interval_seconds": 5})
    message = str(excinfo.value)
    assert "opt-in and bounded" in message
    assert "30s" in message
    assert store.list() == [], "a refused goal must not be stored"


def test_thirty_seconds_is_the_shortest_accepted_interval(tmp_path):
    """The floor itself is allowed; anything below it is not."""
    store = GoalStore(tmp_path / "goals.json")
    goal = store.add({"kind": "file", "target": str(tmp_path / "a.txt"),
                      "interval_seconds": MIN_INTERVAL_SECONDS})
    assert goal.interval_seconds == 30.0


def test_add_refuses_an_unknown_kind_and_an_empty_target(tmp_path):
    store = GoalStore(tmp_path / "goals.json")
    with pytest.raises(ValueError):
        store.add({"kind": "process", "target": "x", "interval_seconds": 60})
    with pytest.raises(ValueError):
        store.add({"kind": "file", "target": "   ", "interval_seconds": 60})
    with pytest.raises(ValueError):
        store.add({"kind": "http", "target": "not-a-url", "interval_seconds": 60})


# -- due() hands each goal out once per interval ---------------------------

def test_due_returns_a_new_goal_once_then_nothing_at_the_same_moment(tmp_path):
    """The duplicate-loop guard: two schedulers asking at once cannot both poll."""
    store = GoalStore(tmp_path / "goals.json")
    goal = store.add({"kind": "file", "target": str(tmp_path / "a.txt"),
                      "interval_seconds": 60})

    first = store.due(now=1_000.0)
    second = store.due(now=1_000.0)

    assert [g.goal_id for g in first] == [goal.goal_id]
    assert second == [], "the same goal must not be due twice for the same moment"


def test_due_returns_the_goal_again_after_the_interval_has_elapsed(tmp_path):
    store = GoalStore(tmp_path / "goals.json")
    goal = store.add({"kind": "file", "target": str(tmp_path / "a.txt"),
                      "interval_seconds": 60})

    store.due(now=1_000.0)
    assert store.due(now=1_059.9) == [], "59.9s is still inside a 60s interval"
    again = store.due(now=1_060.0)
    assert [g.goal_id for g in again] == [goal.goal_id]


def test_a_recorded_observation_ends_the_claim(tmp_path):
    """After recording, due() is driven by the persisted timestamp, not the claim."""
    store = GoalStore(tmp_path / "goals.json")
    goal = store.add({"kind": "file", "target": str(tmp_path / "a.txt"),
                      "interval_seconds": 30})
    store.due(now=1_000.0)
    store.record_observation(goal.goal_id, result="verified", detail="ok",
                             now=1_000.0)

    # A second store over the same file is a restarted process: it has no
    # in-memory claim, and must reach the same answer from the JSON alone.
    restarted = GoalStore(tmp_path / "goals.json")
    assert restarted.due(now=1_010.0) == []
    assert [g.goal_id for g in restarted.due(now=1_030.0)] == [goal.goal_id]


# -- a disabled goal is inert ---------------------------------------------

def test_a_disabled_goal_is_never_due_and_never_observed(tmp_path):
    store = GoalStore(tmp_path / "goals.json")
    goal = store.add({"kind": "file", "target": str(tmp_path / "a.txt"),
                      "interval_seconds": 30})
    store.disable(goal.goal_id)

    assert store.due(now=10_000.0) == []
    outcomes = run_due_observations(store, now=10_000.0)
    assert outcomes == []

    reloaded = store.get(goal.goal_id)
    assert reloaded is not None
    assert reloaded.enabled is False
    assert reloaded.run_count == 0, "a disabled goal was observed anyway"


def test_enable_brings_the_goal_back(tmp_path):
    store = GoalStore(tmp_path / "goals.json")
    goal = store.add({"kind": "file", "target": str(tmp_path / "a.txt"),
                      "interval_seconds": 30})
    store.disable(goal.goal_id)
    store.enable(goal.goal_id)

    assert [g.goal_id for g in store.due(now=10_000.0)] == [goal.goal_id]


# -- observations go through the real observers ---------------------------

def test_run_due_observations_verifies_against_a_healthy_server(tmp_path):
    server, url = _serve(200, "healthy and ready")
    try:
        store = GoalStore(tmp_path / "goals.json")
        goal = store.add({"kind": "http", "target": url,
                          "expect_body": "ready", "interval_seconds": 30})
        outcomes = run_due_observations(store, now=1_000.0)

        assert outcomes == [{
            "goal_id": goal.goal_id,
            "result": "verified",
            "detail": outcomes[0]["detail"],
        }]
        assert outcomes[0]["detail"].startswith("HTTP 200")

        reloaded = store.get(goal.goal_id)
        assert reloaded is not None
        assert reloaded.last_result == "verified"
        assert reloaded.run_count == 1
        assert reloaded.last_observed_at == 1_000.0
    finally:
        server.shutdown()


def test_run_due_observations_fails_on_a_body_mismatch(tmp_path):
    server, url = _serve(200, "not what was promised")
    try:
        store = GoalStore(tmp_path / "goals.json")
        goal = store.add({"kind": "http", "target": url,
                          "expect_body": "ready", "interval_seconds": 30})
        outcomes = run_due_observations(store, now=1_000.0)

        assert [o["goal_id"] for o in outcomes] == [goal.goal_id]
        assert outcomes[0]["result"] == "failed"
        assert "not what was promised" in outcomes[0]["detail"]

        reloaded = store.get(goal.goal_id)
        assert reloaded is not None
        assert reloaded.last_result == "failed"
        assert reloaded.consecutive_failures == 1
    finally:
        server.shutdown()


def test_a_file_goal_is_observed_through_observe_file(tmp_path):
    watched = tmp_path / "state.txt"
    watched.write_text("ready", encoding="utf-8")
    store = GoalStore(tmp_path / "goals.json")
    store.add({"kind": "file", "target": str(watched),
               "expect_text": "ready", "interval_seconds": 30})

    assert run_due_observations(store, now=1_000.0)[0]["result"] == "verified"

    watched.write_text("something else", encoding="utf-8")
    assert run_due_observations(store, now=1_001.0) == [], "still inside the interval"
    assert run_due_observations(store, now=1_030.0)[0]["result"] == "failed"


# -- failure streaks ------------------------------------------------------

def test_consecutive_failures_increments_then_resets_after_a_verified(tmp_path):
    store = GoalStore(tmp_path / "goals.json")
    goal = store.add({"kind": "file", "target": str(tmp_path / "a.txt"),
                      "interval_seconds": 30})

    first = store.record_observation(goal.goal_id, result="failed",
                                     detail="missing", now=1_000.0)
    assert first.consecutive_failures == 1
    second = store.record_observation(goal.goal_id, result="failed",
                                      detail="missing", now=1_030.0)
    assert second.consecutive_failures == 2
    assert second.run_count == 2

    verified = store.record_observation(goal.goal_id, result="verified",
                                        detail="exists", now=1_060.0)
    assert verified.consecutive_failures == 0
    assert verified.run_count == 3


def test_an_unknown_result_leaves_the_failure_streak_alone(tmp_path):
    """Could-not-look is not proof of failure, so it must not escalate either."""
    store = GoalStore(tmp_path / "goals.json")
    goal = store.add({"kind": "file", "target": str(tmp_path / "a.txt"),
                      "interval_seconds": 30})
    store.record_observation(goal.goal_id, result="failed", detail="x", now=1.0)
    after = store.record_observation(goal.goal_id, result="unknown",
                                     detail="unreachable", now=2.0)
    assert after.consecutive_failures == 1
    assert after.last_result == "unknown"


def test_record_observation_rejects_a_made_up_result(tmp_path):
    store = GoalStore(tmp_path / "goals.json")
    goal = store.add({"kind": "file", "target": str(tmp_path / "a.txt"),
                      "interval_seconds": 30})
    with pytest.raises(ValueError):
        store.record_observation(goal.goal_id, result="probably fine", detail="")


# -- persistence ----------------------------------------------------------

def test_observations_persist_to_the_json_file(tmp_path):
    """A fresh store over the same path -- a restarted process -- sees them."""
    path = tmp_path / "goals.json"
    store = GoalStore(path)
    goal = store.add({"kind": "file", "target": str(tmp_path / "a.txt"),
                      "interval_seconds": 45})
    store.record_observation(goal.goal_id, result="failed",
                             detail="missing", now=1_000.0)

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["goals"][goal.goal_id]["last_result"] == "failed"
    assert raw["goals"][goal.goal_id]["last_observed_at"] == 1_000.0
    assert (path.stat().st_mode & 0o777) == 0o600

    fresh = GoalStore(path)
    reloaded = fresh.get(goal.goal_id)
    assert isinstance(reloaded, Goal)
    assert reloaded.last_result == "failed"
    assert reloaded.last_detail == "missing"
    assert reloaded.run_count == 1
    assert reloaded.consecutive_failures == 1
    assert reloaded.expect_text is None
    assert [g.goal_id for g in fresh.list()] == [goal.goal_id]


def test_enable_disable_and_remove_persist(tmp_path):
    path = tmp_path / "goals.json"
    store = GoalStore(path)
    goal = store.add({"kind": "file", "target": str(tmp_path / "a.txt"),
                      "interval_seconds": 30})

    fresh = GoalStore(path)
    assert fresh.disable(goal.goal_id).enabled is False
    assert GoalStore(path).get(goal.goal_id).enabled is False
    assert GoalStore(path).enable(goal.goal_id).enabled is True
    assert GoalStore(path).remove(goal.goal_id) is True
    assert GoalStore(path).remove(goal.goal_id) is False
    assert GoalStore(path).get(goal.goal_id) is None


def test_a_corrupt_goals_file_is_reported_not_silently_replaced(tmp_path):
    """Degrading to an empty store would silently drop every standing monitor."""
    path = tmp_path / "goals.json"
    path.write_text("{not json", encoding="utf-8")
    progress_before = path.read_text(encoding="utf-8")

    store = GoalStore(path)
    with pytest.raises(GoalStoreError):
        store.list()
    assert path.read_text(encoding="utf-8") == progress_before


def test_duplicate_goal_ids_are_refused(tmp_path):
    store = GoalStore(tmp_path / "goals.json")
    store.add({"goal_id": "g1", "kind": "file", "target": str(tmp_path / "a.txt"),
               "interval_seconds": 30})
    with pytest.raises(ValueError):
        store.add({"goal_id": "g1", "kind": "file",
                   "target": str(tmp_path / "b.txt"), "interval_seconds": 30})


# -- helpers --------------------------------------------------------------

def _serve(status: int, body: str):
    """A real local HTTP server, the same shape tests/test_selfhealing.py uses."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            payload = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}/health"

# -- goal -> mission bridge -------------------------------------------------

def test_a_goal_becomes_a_verifiable_mission_condition():
    from jarvis.core.goalguard import mission_condition_for

    class Goal:
        goal_id = "g1"
        kind = "http"
        target = "http://127.0.0.1:9/health"
        expect_body = "ready"

    condition = mission_condition_for(Goal())
    assert condition["condition_id"] == "G:g1"
    assert "HTTP 200" in condition["statement"]
    assert condition["verifier"] == "http"


def test_the_goal_verifier_observes_the_real_resource(tmp_path):
    """The verifier must read the file, not trust the monitor's last result."""
    from jarvis.core.goalguard import verifier_for

    path = tmp_path / "f.txt"
    path.write_text("contains ready\n", encoding="utf-8")

    class Goal:
        goal_id = "g2"
        kind = "file"
        target = str(path)
        expect_text = "ready"
        expect_body = ""

    result = verifier_for(Goal())(None)
    assert result.condition_id == "G:g2"
    assert result.status == "verified"


def test_a_violation_list_includes_unknown_not_only_failed():
    """A goal that cannot be observed is not healthy; it must not be dropped."""
    from jarvis.core.goalguard import violation_outcome

    outcomes = [
        {"goal_id": "a", "result": "verified", "detail": "ok"},
        {"goal_id": "b", "result": "failed", "detail": "503"},
        {"goal_id": "c", "result": "unknown", "detail": "unreachable"},
    ]
    violations = violation_outcome(outcomes)
    ids = [v["goal_id"] for v in violations]
    assert ids == ["b", "c"]
