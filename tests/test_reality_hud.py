"""Reality HUD and human takeover.

Two things are load-bearing and both are asserted by measurement rather than by
a comment: a takeover must actually refuse the next dispatch (otherwise it is a
badge), and releasing must not undo a pause somebody set by hand.
"""

from __future__ import annotations

import pytest

from jarvis.core.control_tower import ControlTower, WorkerRefusal
from jarvis.core.reality_hud import (
    TAKEOVER_REASON,
    release_all,
    snapshot,
    take_over,
)
from jarvis.tools import build_default_registry


@pytest.fixture()
def tower():
    """A tower for this test only, so a pause never leaks into another file."""
    t = ControlTower()
    t.register_from_registry(build_default_registry())
    return t


# -- the picture -------------------------------------------------------------

def test_a_snapshot_counts_workers_from_the_tower(tower):
    body = snapshot(tower=tower)
    assert len(body["workers"]["workers"]) > 5
    assert body["workers"]["running"] == 0
    assert body["workers"]["paused"] == 0
    assert body["takeover"]["held"] is False


def test_a_snapshot_states_what_a_takeover_cannot_do(tower):
    body = snapshot(tower=tower)
    assert "already inside a handler" in body["takeover"]["cannot"]
    assert body["takeover"]["enforced_at"]


def test_permissions_that_cannot_be_read_are_not_shown_as_green():
    class _Broken:
        def status(self):
            raise RuntimeError("helper missing")

    body = snapshot(tower=ControlTower(), permission_service=_Broken())
    assert body["permissions"]["readable"] is False
    assert body["permissions"]["usable"] == []
    assert "could not be read" in body["permissions"]["note"]


def test_only_usable_permission_states_are_counted_as_usable():
    class _Rows:
        def status(self):
            return [
                {"capability": "screen_recording", "status": "GRANTED",
                 "about_bundled_host": False, "status_note": "n"},
                {"capability": "local_network", "status": "NOT_REQUIRED",
                 "about_bundled_host": False, "status_note": "n"},
                {"capability": "microphone", "status": "NOT_DETERMINED",
                 "explanation": "never asked", "about_bundled_host": False,
                 "status_note": "n"},
                {"capability": "accessibility", "status": "UNKNOWN",
                 "explanation": "cannot ask", "about_bundled_host": False,
                 "status_note": "n"},
            ]

    body = snapshot(tower=ControlTower(), permission_service=_Rows())
    assert sorted(body["permissions"]["usable"]) == ["local_network", "screen_recording"]
    assert [u["capability"] for u in body["permissions"]["unusable"]] == [
        "microphone", "accessibility"]


# -- the takeover actually bites --------------------------------------------

def test_takeover_pauses_every_running_worker(tower):
    result = take_over(tower=tower)
    assert len(result["paused"]) > 5
    body = snapshot(tower=tower)
    assert body["workers"]["paused"] == len(result["paused"])
    assert body["takeover"]["held"] is True


def test_the_next_dispatch_is_refused_after_a_takeover(tower):
    """The measurement that separates a takeover from a badge."""
    tool = "write_file"
    tower.admit(tool)          # allowed before
    tower.release(tool)

    take_over(tower=tower)
    with pytest.raises(WorkerRefusal) as caught:
        tower.admit(tool)
    assert "paused" in str(caught.value)
    assert "human takeover" in str(caught.value)


def test_a_refused_dispatch_is_counted(tower):
    take_over(tower=tower)
    with pytest.raises(WorkerRefusal):
        tower.admit("write_file")
    body = snapshot(tower=tower)
    assert body["workers"]["refused_total"] >= 1


def test_takeover_does_not_adopt_a_pause_somebody_else_set(tower):
    tower.pause("laptop", by="alice", reason="reviewing something")
    result = take_over(tower=tower)
    assert "laptop" in result["already_paused_by_someone_else"]
    assert "laptop" not in result["paused"]
    # And her pause still says it was hers.
    assert tower.worker("laptop").paused_by == "alice"


def test_takeover_skips_a_stopped_worker_rather_than_erroring(tower):
    tower.cancel("car", by="operator", reason="done")
    result = take_over(tower=tower)
    assert "car" not in result["paused"]
    assert any(s["worker"] == "car" for s in result["skipped"])


def test_release_resumes_what_the_takeover_paused(tower):
    take_over(tower=tower)
    result = release_all(tower=tower)
    assert result["resumed"]
    assert snapshot(tower=tower)["workers"]["paused"] == 0


def test_release_leaves_a_hand_set_pause_alone(tower):
    """Undoing somebody's pause would be the takeover overreaching."""
    tower.pause("laptop", by="alice", reason="a human is using this")
    take_over(tower=tower)
    result = release_all(tower=tower)

    assert "laptop" in [row["worker"] for row in result["left_paused"]]
    worker = tower.worker("laptop")
    assert worker.paused is True
    assert worker.paused_by == "alice"
    assert worker.paused_reason == "a human is using this"


def test_the_takeover_reason_is_recorded_on_the_worker(tower):
    take_over(tower=tower)
    worker = tower.worker("laptop")
    assert worker.paused_by == "operator"
    assert worker.paused_reason.startswith("human takeover")
    assert worker.paused_reason == TAKEOVER_REASON


# -- the screen is not a pointer --------------------------------------------

def test_the_module_never_drives_the_screen():
    """No synthetic input, no capture: a takeover stops dispatch, it does not
    take the pointer."""
    import pathlib

    source = pathlib.Path("jarvis/core/reality_hud.py").read_text()
    for forbidden in ("osascript", "CGEvent", "cliclick", "screencapture",
                      "pyautogui"):
        assert forbidden not in source, forbidden


# -- over the wire -----------------------------------------------------------

def test_the_hud_is_served_over_http():
    from tests.conftest import TEST_TOKEN
    from fastapi.testclient import TestClient
    from jarvis.api.app import app

    client = TestClient(app, base_url="http://127.0.0.1:8765")
    headers = {"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"}
    body = client.get("/reality", headers=headers).json()
    assert "permissions" in body
    assert "workers" in body
    assert body["takeover"]["enforced_at"]


def test_takeover_and_release_over_http():
    from tests.conftest import TEST_TOKEN
    from fastapi.testclient import TestClient
    from jarvis.api.app import app

    client = TestClient(app, base_url="http://127.0.0.1:8765")
    headers = {"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"}

    taken = client.post("/reality/takeover", headers=headers, json={}).json()
    try:
        assert "paused" in taken
        assert "already inside a handler" in taken["cannot"]
    finally:
        released = client.post("/reality/release", headers=headers).json()
    assert "resumed" in released

def test_a_takeover_also_releases_whatever_input_is_held(tower):
    """Pausing stops the next dispatch; it does nothing about a held button.

    A drag in progress has the mouse button down. If taking over only paused
    workers, a person grabbing the mouse to stop it would find their own pointer
    dragging something. The takeover must release held input, and must report what
    that actually did rather than assuming success.
    """
    result = take_over(tower=tower)
    assert "released_input" in result, (
        "a takeover reported nothing about the input it may have left held")
    released = result["released_input"]
    assert "status" in released
    # On a machine without post-event access the release is *refused*, and that
    # refusal must be visible rather than reported as a release that happened.
    assert released["status"] in ("verified", "permission_required", "failed",
                                  "performed_unverified", "timed_out"), released


def test_a_failed_release_does_not_fail_the_takeover(tower):
    """A takeover that raised would leave the machine in the state it exists to fix."""
    import jarvis.core.reality_hud as hud

    original = hud._release_held_input
    hud._release_held_input = lambda: {"status": "failed", "reason": "executor missing"}
    try:
        result = take_over(tower=tower)
    finally:
        hud._release_held_input = original
    assert len(result["paused"]) > 5
    assert result["released_input"]["status"] == "failed"
