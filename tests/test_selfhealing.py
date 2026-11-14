"""Self-healing: bounded, evidence-driven recovery -- and its refusals.

The refusals are the feature. A recovery system that restarts anything it cannot
see is worse than no recovery system, so most of these tests pin what the
controller will *not* do.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from jarvis.core.missions import ConditionStatus
from jarvis.core.selfhealing import (
    GoalObservation,
    RecoveryAttempt,
    RecoveryController,
    observe_file,
    observe_http,
)


# -- observers tell the truth ----------------------------------------------

def test_http_observation_verifies_a_real_response():
    server, url = _serve(200, "healthy")
    try:
        obs = observe_http(url)
        assert obs.result == ConditionStatus.VERIFIED.value
        assert obs.actual.startswith("HTTP 200")
    finally:
        server.shutdown()


def test_http_observation_fails_on_a_wrong_status():
    server, url = _serve(503, "down")
    try:
        obs = observe_http(url)
        assert obs.result == ConditionStatus.FAILED.value
        assert "503" in obs.actual
    finally:
        server.shutdown()


def test_http_observation_fails_when_the_body_is_wrong():
    server, url = _serve(200, "not what was promised")
    try:
        obs = observe_http(url, expect_body="ready")
        assert obs.result == ConditionStatus.FAILED.value
    finally:
        server.shutdown()


def test_http_observation_is_unknown_not_failed_when_unreachable():
    """Unreachable is 'I could not look', which is not the same as 'it is down'."""
    port = _free_port()
    obs = observe_http(f"http://127.0.0.1:{port}/health", timeout=0.5)
    assert obs.result == ConditionStatus.UNKNOWN.value
    assert "unreachable" in obs.actual


def test_file_observation_verifies_and_fails(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("contains ready", encoding="utf-8")

    assert observe_file(str(target), expect_text="ready").result == ConditionStatus.VERIFIED.value
    assert observe_file(str(target), expect_text="absent").result == ConditionStatus.FAILED.value
    missing = observe_file(str(tmp_path / "nope.txt"))
    assert missing.result == ConditionStatus.FAILED.value


# -- the controller refuses more than it acts ------------------------------

def _failed(actual="HTTP 503") -> GoalObservation:
    return GoalObservation("G1", "http", "http://x", "HTTP 200", actual,
                           ConditionStatus.FAILED.value)


def test_a_verified_goal_needs_no_recovery():
    controller = RecoveryController()
    ok = GoalObservation("G1", "http", "http://x", "HTTP 200", "HTTP 200",
                         ConditionStatus.VERIFIED.value)
    assert controller.propose(ok)["needed"] is False


def test_an_unowned_resource_is_never_restarted():
    """The denial the directive asks for: arbitrary processes are not ours."""
    proposal = RecoveryController().propose(_failed(), can_restart=False)
    assert proposal["allowed"] is False
    assert "not one Vantage owns" in proposal["reason"]


def test_an_unobservable_goal_escalates_rather_than_acting():
    unknown = GoalObservation("G1", "http", "http://x", "HTTP 200",
                              "unreachable: connection refused",
                              ConditionStatus.UNKNOWN.value)
    proposal = RecoveryController().propose(unknown, can_restart=True)
    assert proposal["allowed"] is False
    assert proposal.get("escalate") is True


def test_an_owned_failure_may_be_repaired():
    proposal = RecoveryController().propose(_failed(), can_restart=True,
                                            restart_target="demo-service")
    assert proposal["allowed"] is True
    assert proposal["action"] == "restart"


def test_attempts_are_bounded():
    controller = RecoveryController(max_attempts=2)
    attempts = [RecoveryAttempt("G1", "restart", at=time.time() - 100)
                for _ in range(2)]
    proposal = controller.propose(_failed(), attempts=attempts, can_restart=True)
    assert proposal["allowed"] is False
    assert proposal.get("escalate") is True
    assert "2 recovery attempts" in proposal["reason"]


def test_a_recent_attempt_is_not_repeated_immediately():
    controller = RecoveryController(min_interval=60.0)
    attempts = [RecoveryAttempt("G1", "restart", at=time.time())]
    proposal = controller.propose(_failed(), attempts=attempts, can_restart=True)
    assert proposal["allowed"] is False
    assert "moments ago" in proposal["reason"]


def test_the_summary_reports_what_is_left():
    controller = RecoveryController(max_attempts=3)
    summary = controller.attempts_summary([RecoveryAttempt("G1", "restart")])
    assert summary["count"] == 1
    assert summary["remaining"] == 2


# -- helpers ---------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(status: int, body: str):
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
    url = f"http://127.0.0.1:{server.server_port}/health"
    return server, url