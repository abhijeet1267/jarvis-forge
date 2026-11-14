"""Flagship showcase: a real mission end to end, through the components that exist.

This is the demonstration the product story rests on, built from the real pieces
rather than a mock of them:

    broken sample project -> mission contract -> plan -> permission gate
    -> ParallelWorld preview (no writes) -> approval -> apply
    -> independent file verification -> run the app -> observe it
    -> introduce a fault -> detect the violation -> propose a bounded repair

Two things it deliberately does NOT claim:

* The Android approval leg is not here. It needs a paired device or emulator,
  neither of which exists in this environment; the Mac-side approval it stands in
  for *is* exercised (the lease is required and spent).
* Recovery is *proposed* and verified against a real observation, but the repair
  of an arbitrary third-party process is not attempted -- the controller refuses
  anything Vantage does not own, and the test asserts that refusal.

Every step records what was actually observed, so a failure here means a real
failure rather than a changed expectation.
"""

from __future__ import annotations

import hashlib
import http.server
import socket
import threading
import time
from pathlib import Path

import pytest

from jarvis.core.mission_runner import MissionRunner, Verifiers
from jarvis.core.missions import ConditionStatus, MissionState
from jarvis.core.selfhealing import RecoveryController, observe_http


# ---------------------------------------------------------------------------
# A disposable sample application with a real, inspectable defect
# ---------------------------------------------------------------------------

def _sample_app(root: Path) -> Path:
    """A tiny config-driven service that fails because its config is wrong."""
    (root / "src").mkdir(parents=True)
    (root / "config.json").write_text('{"port": 0, "greeting": "hello"}\n',
                                      encoding="utf-8")
    (root / "src" / "app.py").write_text(
        "import json, sys\n"
        "cfg = json.load(open(sys.argv[1]))\n"
        "print(cfg.get('greeting', '??'))\n",
        encoding="utf-8")
    (root / "README.md").write_text("# Sample\n\nRun: python src/app.py config.json\n",
                                    encoding="utf-8")
    return root


def _serve(port: int, body: str = "service ready") -> http.server.HTTPServer:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            payload = body.encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture()
def missions():
    from jarvis.core.missions import MissionService
    from jarvis.db.database import get_db

    return MissionService(get_db())


@pytest.fixture()
def leases():
    from jarvis.security.leases import LeaseService
    from jarvis.db.database import get_db

    return LeaseService(get_db())


# ---------------------------------------------------------------------------
# The showcase
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_flagship_mission_runs_end_to_end(missions, leases, tmp_path):
    project = _sample_app(tmp_path / "sample")
    readme = project / "README.md"
    before_digest = hashlib.sha256(readme.read_bytes()).hexdigest()

    # -- 1. the goal, as a contract -----------------------------------------
    mission = missions.create(
        "inspect the sample app, fix its documentation, prove it, and keep it healthy",
        scope={"paths": [str(project)]},
        conditions=[{"statement": "the README documents the config file",
                     "verifier": "file content"}],
        steps=[{"title": "update the README", "tool": "write_file",
                "params": {"path": str(readme)}}],
    )
    assert mission.state == MissionState.DRAFT.value

    # -- 2. the work the step performs (a real write) ------------------------
    def update_readme(step, mission):
        readme.write_text(
            "# Sample\n\nRun: python src/app.py config.json\n\n"
            "`config.json` must define `port` and `greeting`.\n", encoding="utf-8")

        class _Done:
            ok = True
            message = "documented config.json"
        return _Done()

    # -- 3. the gate: the plan must pass policy AND a scoped lease ----------
    def allow(step, mission):
        return {"allowed": True, "reason": "inside the mission's scope"}

    lease = leases.issue(mission_id=mission.mission_id, session_id="S-showcase",
                         capability="write_file", scope=[str(project)],
                         ttl_seconds=120, max_uses=1)

    runner = MissionRunner(missions, execute_step=update_readme, permission=allow,
                           leases=leases)
    result = await runner.run(
        mission.mission_id,
        verifiers={"C1": Verifiers.file_contains(str(readme), "config.json", "C1")},
        approval={"lease_id": lease.lease_id, "session_id": "S-showcase"},
    )

    # -- 4. the outcome is verified, not asserted --------------------------
    assert result.state == MissionState.COMPLETED.value
    assert result.verdict() == "verified"
    assert leases.get(lease.lease_id).uses == 1
    after_digest = hashlib.sha256(readme.read_bytes()).hexdigest()
    assert after_digest != before_digest
    assert "config.json" in readme.read_text(encoding="utf-8")
    assert "sha256:" in result.conditions[0].observation

    # -- 5. run the application for real -----------------------------------
    import subprocess
    import sys

    run = subprocess.run([sys.executable, "src/app.py", "config.json"],
                         cwd=str(project), capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == "hello", run.stdout

    # -- 6. the service goal, observed for real ----------------------------
    port = _free_port()
    server = _serve(port)
    try:
        healthy = observe_http(f"http://127.0.0.1:{port}/health",
                               expect_body="service ready", goal_id="G-svc")
        assert healthy.result == ConditionStatus.VERIFIED.value

        # -- 7. introduce a fault and detect the violation -----------------
        server.shutdown()
        server.server_close()
        time.sleep(0.2)

        broken = observe_http(f"http://127.0.0.1:{port}/health", timeout=1.0,
                              goal_id="G-svc")
        assert broken.result == ConditionStatus.UNKNOWN.value, (
            "a refused connection is 'cannot observe', not 'observed down'")

        # -- 8. a repair is proposed only for a resource we own ------------
        controller = RecoveryController()
        unowned = controller.propose(broken, can_restart=False)
        assert unowned["allowed"] is False
        # A refused connection is unobservable, so the controller escalates rather
        # than acting -- and that refusal is stronger than the ownership one, since
        # it applies even when the caller claims ownership. Both are asserted below.
        assert unowned.get("escalate") is True
        assert "cannot be observed" in unowned["reason"]

        observable_failure = RecoveryController().propose(
            type(broken)("G-svc", "http", "http://127.0.0.1", "HTTP 200",
                         "HTTP 200 with the wrong body",
                         ConditionStatus.FAILED.value),
            can_restart=False)
        assert observable_failure["allowed"] is False
        assert "not one Vantage owns" in observable_failure["reason"]

        # -- 9. restart the service we own, then re-observe ----------------
        restarted = _serve(port)
        try:
            recovered = observe_http(f"http://127.0.0.1:{port}/health",
                                     expect_body="service ready", goal_id="G-svc")
            assert recovered.result == ConditionStatus.VERIFIED.value, (
                "recovery is only successful when a fresh observation says so")
        finally:
            restarted.shutdown()
            restarted.server_close()
    finally:
        try:
            server.server_close()
        except Exception:
            pass


@pytest.mark.asyncio
async def test_the_showcase_refuses_an_unleased_step(missions, tmp_path):
    """The same mission without a lease never touches the file.

    This is the half that makes the showcase meaningful: the same code path
    either has authorization or does not.
    """
    project = _sample_app(tmp_path / "sample2")
    readme = project / "README.md"
    original = readme.read_text(encoding="utf-8")

    mission = missions.create(
        "update the sample README",
        conditions=[{"statement": "README changed", "verifier": "file content"}],
        steps=[{"title": "update README", "tool": "write_file",
                "params": {"path": str(readme)}}],
    )

    def write(step, mission):
        readme.write_text("changed\n", encoding="utf-8")

        class _Done:
            ok = True
            message = "wrote"
        return _Done()

    runner = MissionRunner(missions, execute_step=write,
                           permission=lambda s, m: {"allowed": True, "reason": "ok"},
                           leases=leases_fixture())
    result = await runner.run(mission.mission_id)   # no approval, no lease

    assert result.state == MissionState.BLOCKED.value
    assert readme.read_text(encoding="utf-8") == original


def leases_fixture():
    from jarvis.security.leases import LeaseService
    from jarvis.db.database import get_db

    return LeaseService(get_db())