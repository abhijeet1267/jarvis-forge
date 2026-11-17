"""Backend identity, staleness, and the safe-restart decision.

These pin the properties that make a stale backend diagnosable and a restart
safe. The incident: a backend started a day before a routing change served old
code, and the macOS client treated *any* HTTP response on the port as "the
backend is running", so nothing ever noticed.

The restart tool's most important line is the one that refuses. These tests
exercise the decision function directly, then a real subprocess once, so the
refusal is proven rather than asserted in prose.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import backend_lifecycle as lifecycle  # noqa: E402

#: Bound on waiting for a stub to be reaped after the tool said it stopped it.
GRACEFUL_TEST_WAIT = lifecycle.GRACEFUL_WAIT_S + 5


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

def test_identity_names_the_service_and_the_checkout():
    from jarvis.runtime import identity

    info = identity.identity()
    assert info["service"] == "vantage-backend"
    assert info["instance_id"].startswith("BKT-")
    assert info["pid"] > 0
    # The repository root, not the package directory: two checkouts must be
    # distinguishable, and `.../jarvis-forge/jarvis` is not the checkout.
    assert Path(info["source_root"]) == REPO
    assert info["app_version"]


def test_identity_is_stable_within_one_process_and_has_no_secrets():
    from jarvis.runtime import identity

    first, second = identity.identity(), identity.identity()
    assert first["instance_id"] == second["instance_id"]

    text = json.dumps(first).lower()
    for forbidden in ("api_key", "token", "password", "secret", "authorization"):
        assert forbidden not in text, f"identity leaked a {forbidden!r} field"


def test_a_revision_is_a_commit_or_an_honest_unknown():
    from jarvis.runtime import identity

    revision = identity.identity()["revision"]
    # Never a fabricated value: either a real short sha (optionally marked
    # dirty) or None when there is no repository to ask.
    if revision is not None:
        head = revision.split("+")[0]
        assert len(head) >= 7 and all(c in "0123456789abcdef" for c in head), revision


def test_source_newer_than_the_process_is_reported_as_stale(tmp_path, monkeypatch):
    """A file edited after the process started must make it report stale.

    The scan result is pinned rather than read from the disk. Measured: this
    test depended on some real source file being newer than "an hour ago", so
    on a quiet tree or a restored mtime it failed while the behaviour it
    checks was perfectly intact. Pinning the input keeps the assertion about
    the *decision* and makes the test independent of when it happens to run.
    """
    from jarvis.runtime import identity

    monkeypatch.setattr(identity, "_STARTED_AT", 1_000_000.0)   # long ago
    monkeypatch.setattr(identity, "newest_source_mtime", lambda: 2_000_000.0)
    result = identity.staleness()
    assert result["stale"] is True
    assert "restart" in result["note"].lower()

    # And the other direction, so a hard-wired True cannot pass this.
    monkeypatch.setattr(identity, "newest_source_mtime", lambda: 500_000.0)
    assert identity.staleness()["stale"] is False


def test_the_staleness_scan_ignores_files_the_backend_never_imports(tmp_path, monkeypatch):
    """Editing a test or the Swift app must not tell the user to restart.

    Measured 2026-10-28: editing tests/test_backend_lifecycle.py made the live
    backend report ``stale: true``. The scan walked the whole checkout, which
    includes tests/ and macos/ -- neither is imported by this process.
    """
    from jarvis.runtime import identity

    baseline = identity.newest_source_mtime()
    assert baseline is not None

    # A file outside the package, made newer than everything the scan covers.
    decoy = identity._REPO_ROOT / "tests" / f"test_decoy_{tmp_path.name}.py"
    decoy.write_text("# not imported by the backend\n", encoding="utf-8")
    try:
        import os
        stamp = baseline + 3600
        os.utime(decoy, (stamp, stamp))
        assert identity.newest_source_mtime() == baseline, (
            "a tests/ file outside the package raised the reported source time")
    finally:
        decoy.unlink(missing_ok=True)

# ---------------------------------------------------------------------------
# The restart decision
# ---------------------------------------------------------------------------

def _fake_identity(tmp_path: Path, *, service="vantage-backend", root=None):
    return {
        "identity": {"service": service, "source_root": str(root or REPO), "pid": 123},
        "code": {"stale": True, "note": "older than the code on disk"},
    }


def test_a_backend_from_this_checkout_is_ours(monkeypatch, tmp_path):
    monkeypatch.setattr(lifecycle, "listening_pid", lambda port: 123)
    monkeypatch.setattr(lifecycle, "_get", lambda port, path, timeout=3.0: _fake_identity(tmp_path))
    info = lifecycle.describe(8765)
    assert info["state"] == "ours"
    assert info["safe_to_restart"] is True
    assert info["stale"] is True


def test_a_different_service_on_the_port_is_never_ours(monkeypatch, tmp_path):
    monkeypatch.setattr(lifecycle, "listening_pid", lambda port: 123)
    monkeypatch.setattr(lifecycle, "_get",
                        lambda port, path, timeout=3.0: _fake_identity(tmp_path, service="other"))
    info = lifecycle.describe(8765)
    assert info["state"] == "other-vantage-checkout"
    assert info["safe_to_restart"] is False


def test_a_server_that_does_not_answer_identity_is_foreign(monkeypatch):
    """The old backend had no /health/identity at all -- exactly this case."""
    monkeypatch.setattr(lifecycle, "listening_pid", lambda port: 999)
    monkeypatch.setattr(lifecycle, "_get", lambda port, path, timeout=3.0: None)
    monkeypatch.setattr(lifecycle, "port_open", lambda port: True)
    info = lifecycle.describe(8765)
    assert info["state"] == "foreign"
    assert info["safe_to_restart"] is False


def test_another_checkout_with_the_same_service_name_is_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(lifecycle, "listening_pid", lambda port: 123)
    monkeypatch.setattr(lifecycle, "_get", lambda port, path, timeout=3.0:
                        _fake_identity(tmp_path, root=tmp_path / "somewhere-else"))
    info = lifecycle.describe(8765)
    assert info["safe_to_restart"] is False


def test_stop_refuses_a_foreign_server_without_signalling(monkeypatch):
    signalled = []
    monkeypatch.setattr(lifecycle, "describe", lambda port: {
        "port": port, "pid": 4242, "state": "foreign",
        "identity": {}, "code": {}, "stale": False, "safe_to_restart": False,
    })
    monkeypatch.setattr(lifecycle.os, "kill", lambda pid, sig: signalled.append(pid))

    ok, message = lifecycle.stop(8765)
    assert ok is False
    assert "refusing" in message.lower()
    assert signalled == [], "nothing may be signalled for a foreign process"


def test_start_refuses_to_add_a_second_instance(monkeypatch):
    monkeypatch.setattr(lifecycle, "port_open", lambda port: True)
    monkeypatch.setattr(lifecycle, "describe", lambda port: {
        "port": port, "pid": 1, "state": "ours", "identity": {}, "code": {},
        "stale": False, "safe_to_restart": True,
    })
    # No ownership record: the listener may well be ours, but this tool cannot
    # prove it started this instance, so it must not claim "already running".
    monkeypatch.setattr(lifecycle, "read_record", lambda port: None)
    ok, message = lifecycle.start(8765)
    assert ok is False
    assert "already in use" in message
    assert "second instance" in message


# ---------------------------------------------------------------------------
# One real run: the identity endpoint answers without a credential
# ---------------------------------------------------------------------------

@pytest.fixture()
def live_backend(tmp_path):
    """A real backend on an ephemeral port, stopped again on the way out."""
    import os
    import shutil
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    home = tmp_path / "home"
    env = dict(os.environ, JARVIS_HOME=str(home), JARVIS_LOG_LEVEL="WARNING")
    log = (tmp_path / "backend.log").open("wb")
    proc = subprocess.Popen(
        [str(REPO / ".venv" / "bin" / "python"), str(REPO / "run.py"),
         "serve", "--port", str(port), "--host", "127.0.0.1"],
        cwd=str(REPO), env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        deadline = time.time() + 40
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/health/identity", timeout=2) as r:
                    yield port, json.loads(r.read())
                    return
            except Exception:
                if proc.poll() is not None:
                    pytest.fail("backend exited during startup")
                time.sleep(0.25)
        pytest.fail("backend did not answer /health/identity")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


def test_identity_answers_without_a_token(live_backend):
    """The question "what is this?" must be answerable before authentication.

    If it required a token, a stale backend and a wrong token would look the
    same -- which is precisely how the stale process went unnoticed.
    """
    port, payload = live_backend
    assert payload["identity"]["service"] == "vantage-backend"
    assert payload["identity"]["pid"] > 0
    assert Path(payload["identity"]["source_root"]) == REPO
    assert "stale" in payload["code"]


def test_the_full_health_report_still_requires_a_token(live_backend):
    """Identity is open; the report is not. Do not widen the authenticated one."""
    import urllib.error

    port, _ = live_backend
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3)
    assert exc.value.code == 401

def test_an_unidentifiable_server_reports_unknown_staleness_not_fresh(monkeypatch):
    """"Cannot tell" must not print as "not stale".

    An older build has no /health/identity, so its code age is unknown. Reporting
    ``stale: False`` for it gave exactly the process this tool exists to catch a
    clean bill of health.
    """
    monkeypatch.setattr(lifecycle, "listening_pid", lambda port: 321)
    monkeypatch.setattr(lifecycle, "_get", lambda port, path, timeout=3.0: None)
    monkeypatch.setattr(lifecycle, "port_open", lambda port: True)

    info = lifecycle.describe(8765)
    assert info["state"] == "foreign"
    assert info["stale"] is None


def test_a_known_stale_backend_reports_true(monkeypatch):
    monkeypatch.setattr(lifecycle, "listening_pid", lambda port: 321)
    monkeypatch.setattr(lifecycle, "_get", lambda port, path, timeout=3.0: {
        "identity": {"service": "vantage-backend", "source_root": str(REPO), "pid": 321},
        "code": {"stale": True, "note": "older"},
    })
    info = lifecycle.describe(8765)
    assert info["state"] == "ours"
    assert info["stale"] is True


# ---------------------------------------------------------------------------
# Legacy migration: proof of ownership, then a bounded graceful replacement
# ---------------------------------------------------------------------------

def test_our_own_old_backend_is_a_migration_candidate():
    """A process from this checkout that predates /health/identity is *ours*.

    This is the live case: pid 88264 runs `run.py serve` with cwd = this repo.
    Refusing it forever would leave the user with no supported path, so the
    evidence — same uid, this working directory, this entry point — makes it a
    candidate rather than a stranger.
    """
    verdict, reason = lifecycle.legacy_verdict(
        {"uid": __import__("os").getuid(),
         "cwd": str(REPO),
         "command": ".venv/bin/python run.py serve --port 8765 --host 0.0.0.0",
         "executable": "/usr/bin/python3"},
        REPO)
    assert verdict == "candidate", reason


def test_another_users_process_is_never_a_candidate():
    verdict, reason = lifecycle.legacy_verdict(
        {"uid": 0, "cwd": str(REPO), "command": "python run.py serve"}, REPO)
    assert verdict == "foreign"
    assert "uid" in reason


def test_a_process_from_another_directory_is_never_a_candidate():
    verdict, reason = lifecycle.legacy_verdict(
        {"uid": __import__("os").getuid(), "cwd": "/tmp/some-other-project",
         "command": "python run.py serve"}, REPO)
    assert verdict == "foreign"
    assert "directory" in reason


def test_a_python_from_this_directory_that_is_not_the_backend_is_refused():
    """"It is python and it is in the repo" is not enough."""
    verdict, reason = lifecycle.legacy_verdict(
        {"uid": __import__("os").getuid(), "cwd": str(REPO),
         "command": "python scripts/unrelated_tool.py"}, REPO)
    assert verdict == "foreign"
    assert "entry point" in reason


def test_migrate_refuses_without_confirm(monkeypatch):
    called = []
    monkeypatch.setattr(lifecycle.os, "kill", lambda pid, sig: called.append(pid))
    ok, message = lifecycle.migrate(8765, confirm=False)
    assert ok is False
    assert "--confirm" in message
    assert called == []


def test_migrate_refuses_a_foreign_listener(monkeypatch):
    called = []
    monkeypatch.setattr(lifecycle, "describe", lambda port: {
        "port": port, "pid": 7, "state": "foreign", "identity": {}, "code": {},
        "stale": None, "legacy": {"reason": "another user"}, "safe_to_restart": False,
        "migratable": False,
    })
    monkeypatch.setattr(lifecycle.os, "kill", lambda pid, sig: called.append(pid))
    ok, message = lifecycle.migrate(8765, confirm=True)
    assert ok is False
    assert "Refusing" in message
    assert called == []


def test_migrate_revalidates_before_signalling(monkeypatch):
    """The pid could be gone or the port reused between describe() and the kill."""
    called = []
    monkeypatch.setattr(lifecycle, "describe", lambda port: {
        "port": port, "pid": 11, "state": "legacy", "identity": {}, "code": {},
        "stale": None, "legacy": {"reason": "ours"}, "safe_to_restart": False,
        "migratable": True,
    })
    monkeypatch.setattr(lifecycle, "listening_pid", lambda port: 999)   # changed hands
    monkeypatch.setattr(lifecycle.os, "kill", lambda pid, sig: called.append(pid))
    ok, message = lifecycle.migrate(8765, confirm=True)
    assert ok is False
    assert "changed" in message
    assert called == []


def test_migrate_refuses_when_evidence_flips_before_the_signal(monkeypatch):
    called = []
    monkeypatch.setattr(lifecycle, "describe", lambda port: {
        "port": port, "pid": 12, "state": "legacy", "identity": {}, "code": {},
        "stale": None, "legacy": {"reason": "ours"}, "safe_to_restart": False,
        "migratable": True,
    })
    monkeypatch.setattr(lifecycle, "listening_pid", lambda port: 12)
    monkeypatch.setattr(lifecycle, "process_evidence", lambda pid: {
        "uid": 0, "cwd": str(REPO), "command": "python run.py serve"})
    monkeypatch.setattr(lifecycle.os, "kill", lambda pid, sig: called.append(pid))
    ok, message = lifecycle.migrate(8765, confirm=True)
    assert ok is False
    assert "no longer proven" in message
    assert called == []


def test_bind_host_follows_the_lan_switch(tmp_path):
    """A restart must not silently narrow or widen the binding."""
    (tmp_path / "lan_mode").write_text("true\n")
    assert lifecycle._bind_host(str(tmp_path)) == "0.0.0.0"
    (tmp_path / "lan_mode").write_text("false\n")
    assert lifecycle._bind_host(str(tmp_path)) == "127.0.0.1"
    # No switch at all is the loopback default, not a guess at LAN.
    (tmp_path / "lan_mode").unlink()
    assert lifecycle._bind_host(str(tmp_path)) == "127.0.0.1"

def test_a_failed_start_names_what_the_process_said(tmp_path, monkeypatch):
    """'exited during startup (code 0)' alone hides the actionable cause.

    The common real case is the single-instance claim: another Vantage server
    already holds the data directory. The launcher prints that refusal, and the
    lifecycle tool must surface it rather than leave a dead port unexplained.
    """
    log = tmp_path / "backend.log"
    log.write_text(
        "INFO starting\n"
        "Vantage: another Vantage server (pid 4242) is already running against "
        f"{tmp_path} on port 8765. Stop it before starting a second one.\n",
        encoding="utf-8")
    reason = lifecycle._startup_failure_reason(log)
    assert "pid 4242" in reason
    assert reason.startswith("Vantage:")

def test_a_failure_reason_is_empty_when_the_log_says_nothing_useful(tmp_path):
    log = tmp_path / "backend.log"
    log.write_text("INFO nothing to see here\n", encoding="utf-8")
    assert lifecycle._startup_failure_reason(log) == ""

def test_a_missing_log_is_not_an_exception(tmp_path):
    assert lifecycle._startup_failure_reason(tmp_path / "absent.log") == ""

def test_only_the_tail_of_a_large_log_is_read(tmp_path):
    """The log is append-only across restarts; the newest lines are the failure."""
    log = tmp_path / "backend.log"
    log.write_text(("old noise line\n" * 2000)
                   + "Vantage: nothing is listening on 127.0.0.1:8765 -- boom\n",
                   encoding="utf-8")
    reason = lifecycle._startup_failure_reason(log, max_bytes=4096)
    assert "boom" in reason


# ---------------------------------------------------------------------------
# Ownership records: decide from a stamp, never from a guess
# ---------------------------------------------------------------------------
#
# Each test here runs against a scratch JARVIS_HOME and a private record
# directory, so nothing reads or writes the developer's real lifecycle state.

@pytest.fixture()
def lifecycle_state(tmp_path, monkeypatch):
    """A private ownership-record directory and an isolated profile home."""
    state = tmp_path / "state"
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv(lifecycle.STATE_DIR_ENV, str(state))
    monkeypatch.setenv("JARVIS_HOME", str(home))
    return state, home


def _dead_pid() -> int:
    """A pid that has positively exited (a reaped child), so no process owns it."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=10)
    return proc.pid


def _free_port() -> int:
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_a_record_round_trips_with_pid_profile_and_root(lifecycle_state):
    _, home = lifecycle_state
    port = _free_port()
    assert lifecycle.write_record(port, pid=os.getpid(), home=str(home),
                                  project_root=str(REPO))
    record = lifecycle.read_record(port)
    assert record["pid"] == os.getpid()
    assert Path(record["home"]) == home
    assert Path(record["project_root"]) == REPO
    assert record["started_at"] > 0


def test_a_dead_pids_record_is_stale(lifecycle_state):
    _, home = lifecycle_state
    port = _free_port()
    lifecycle.write_record(port, pid=_dead_pid(), home=str(home), project_root=str(REPO))
    relation, reason, _ = lifecycle.ownership_verdict(
        port, home=home, project_root=REPO)
    assert relation == "stale"
    assert "exited" in reason


def test_a_proven_same_instance_is_reported_as_same(lifecycle_state, monkeypatch):
    _, home = lifecycle_state
    port = _free_port()
    monkeypatch.setattr(lifecycle, "listening_pid", lambda p: os.getpid())
    lifecycle.write_record(port, pid=os.getpid(), home=str(home), project_root=str(REPO))
    relation, _, _ = lifecycle.ownership_verdict(port, home=home, project_root=REPO)
    assert relation == "same"


def test_a_different_profile_is_distinguished(lifecycle_state, monkeypatch):
    """Same backend, same checkout, another JARVIS_HOME: not this instance."""
    _, home = lifecycle_state
    port = _free_port()
    monkeypatch.setattr(lifecycle, "listening_pid", lambda p: os.getpid())
    lifecycle.write_record(port, pid=os.getpid(), home=str(home / "other"),
                           project_root=str(REPO))
    relation, reason, _ = lifecycle.ownership_verdict(port, home=home, project_root=REPO)
    assert relation == "different-profile"
    assert "profile" in reason


def test_a_different_project_root_is_distinguished(lifecycle_state, monkeypatch):
    _, home = lifecycle_state
    port = _free_port()
    monkeypatch.setattr(lifecycle, "listening_pid", lambda p: os.getpid())
    lifecycle.write_record(port, pid=os.getpid(), home=str(home),
                           project_root=str(home / "Vantage-backup"))
    relation, reason, _ = lifecycle.ownership_verdict(port, home=home, project_root=REPO)
    assert relation == "other-root"
    assert "not" in reason


def test_a_sibling_directory_with_the_repo_as_a_prefix_is_not_the_same_root(tmp_path):
    """``startswith`` would call ``.../Vantage-backup`` this checkout. It is not."""
    sibling = Path(str(REPO) + "-backup")
    assert not lifecycle._same_root(str(sibling), REPO)
    assert lifecycle._same_root(str(REPO), REPO)


def test_a_pid_that_no_longer_holds_the_port_has_changed_hands(lifecycle_state, monkeypatch):
    _, home = lifecycle_state
    port = _free_port()
    monkeypatch.setattr(lifecycle, "listening_pid", lambda p: os.getpid() + 99999)
    lifecycle.write_record(port, pid=os.getpid(), home=str(home), project_root=str(REPO))
    relation, reason, _ = lifecycle.ownership_verdict(port, home=home, project_root=REPO)
    assert relation == "changed-hands"
    assert str(os.getpid()) in reason


def test_clearing_a_record_only_removes_the_pid_that_owns_it(lifecycle_state):
    _, home = lifecycle_state
    port = _free_port()
    lifecycle.write_record(port, pid=os.getpid(), home=str(home), project_root=str(REPO))
    lifecycle.clear_record(port, only_pid=os.getpid() + 1)
    assert lifecycle.read_record(port) is not None, "a newer instance's record was erased"
    lifecycle.clear_record(port, only_pid=os.getpid())
    assert lifecycle.read_record(port) is None


# ---------------------------------------------------------------------------
# One real stub server on a disposable port, driven through start/stop/restart
# ---------------------------------------------------------------------------
#
# These spawn a tiny server the test controls. It answers /health/identity with
# the pid it was told to report, so the tool's "is this MY process" handshake is
# exercised for real without the 40s cost of booting the whole backend.

STUB_SERVER = r"""
import json, os, sys
from http.server import BaseHTTPRequestHandler, HTTPServer

port = int(sys.argv[1])
report_pid = int(sys.argv[2])
root = sys.argv[3]
instance = sys.argv[4]

class H(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass
    def do_GET(self):
        if self.path == "/health/identity":
            body = json.dumps({
                "identity": {"service": "vantage-backend", "instance_id": instance,
                             "pid": report_pid, "source_root": root},
                "code": {"stale": False},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

HTTPServer(("127.0.0.1", port), H).serve_forever()
"""


def _spawn_stub(port: int, root: str = "", report_pid: int = -1) -> subprocess.Popen:
    """Run the stub in the foreground of a child, reporting its own pid by default."""
    code = STUB_SERVER
    argv = [sys.executable, "-c", code, str(port),
            str(report_pid), root or str(REPO), "BKT-stub"]
    return subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _wait_listening(port: int, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if lifecycle.port_open(port):
            return
        time.sleep(0.05)
    raise AssertionError(f"stub server never listened on {port}")


def test_starting_when_the_port_is_free_reports_ready(lifecycle_state, tmp_path, monkeypatch):
    """A start is only a success once the spawned process answers identity."""
    state, home = lifecycle_state
    port = _free_port()
    # The stub must report its own pid: the handshake accepts only a payload
    # naming the very pid start() spawned.
    stub = tmp_path / "stub_ready.py"
    stub.write_text(
        "import json, os, sys\n"
        "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
        "port = int(sys.argv[1]); root = sys.argv[2]\n"
        "class H(BaseHTTPRequestHandler):\n"
        "    def log_message(self, *a): pass\n"
        "    def do_GET(self):\n"
        "        if self.path != '/health/identity':\n"
        "            self.send_response(404); self.end_headers(); return\n"
        "        body = json.dumps({'identity': {'service': 'vantage-backend',\n"
        "            'instance_id': 'BKT-ready', 'pid': os.getpid(), 'source_root': root},\n"
        "            'code': {'stale': False}}).encode()\n"
        "        self.send_response(200)\n"
        "        self.send_header('Content-Type', 'application/json')\n"
        "        self.send_header('Content-Length', str(len(body)))\n"
        "        self.end_headers(); self.wfile.write(body)\n"
        "HTTPServer(('127.0.0.1', port), H).serve_forever()\n",
        encoding="utf-8")
    monkeypatch.setattr(lifecycle, "spawn_command",
                        lambda p, host: [sys.executable, str(stub), str(p), str(REPO)])
    ok, message = lifecycle.start(port, home=str(home))
    try:
        assert ok is True, message
        assert "answers /health/identity" in message
        record = lifecycle.read_record(port)
        assert record is not None, "a successful start must leave an ownership record"
        assert Path(record["home"]) == home
        assert Path(record["project_root"]) == REPO
        assert record["instance_id"] == "BKT-ready"
    finally:
        lifecycle.stop(port, force=True)


def test_the_same_instance_holding_the_port_is_reported_and_not_duplicated(
        lifecycle_state, tmp_path, monkeypatch):
    """Second launch while the first holds the port: "already running", no spawn."""
    state, home = lifecycle_state
    port = _free_port()
    stub = _spawn_stub(port)
    _wait_listening(port)
    try:
        # The stub is a live process holding the port; stamp it as this profile's.
        lifecycle.write_record(port, pid=stub.pid, home=str(home),
                               project_root=str(REPO))
        spawned = []
        monkeypatch.setattr(lifecycle, "spawn_command",
                            lambda p, host: spawned.append(p) or ["false"])
        ok, message = lifecycle.start(port, home=str(home))
        assert ok is True, message
        assert "already running" in message.lower()
        assert str(stub.pid) in message
        assert spawned == [], "a second instance was started over a live one"
    finally:
        stub.kill()
        stub.wait(timeout=GRACEFUL_TEST_WAIT)


def test_a_foreign_process_holding_the_port_is_refused_and_not_killed(
        lifecycle_state, tmp_path, monkeypatch):
    """The single most important rule: an unrelated listener is never signalled."""
    state, home = lifecycle_state
    port = _free_port()
    foreign = subprocess.Popen(
        [sys.executable, "-c",
         "import socket,sys,time\n"
         "s=socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
         "s.bind(('127.0.0.1', int(sys.argv[1]))); s.listen(5)\n"
         "time.sleep(120)\n",
         str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _wait_listening(port)
    signalled = []
    real_kill = os.kill
    try:
        monkeypatch.setattr(lifecycle.os, "kill",
                            lambda pid, sig: signalled.append((pid, sig)))
        spawned = []
        monkeypatch.setattr(lifecycle, "spawn_command",
                            lambda p, host: spawned.append(p) or ["false"])
        ok, message = lifecycle.start(port, home=str(home))
        assert ok is False, message
        assert "already in use" in message
        assert str(foreign.pid) in message, "the refusal must name the holder"
        assert "will not stop it" in message
        assert spawned == [], "a second instance was started over a foreign listener"
        assert signalled == [], "a process this tool did not start was signalled"
        # And stop() must refuse it too, by the same evidence.
        ok, message = lifecycle.stop(port)
        assert ok is False
        assert "Refusing" in message
        assert signalled == []
        assert foreign.poll() is None, "the foreign process was terminated"
    finally:
        if foreign.poll() is None:
            real_kill(foreign.pid, 9)
        foreign.wait(timeout=10)


def test_stop_confirms_exit_and_reports_the_pid(lifecycle_state, tmp_path, monkeypatch):
    state, home = lifecycle_state
    port = _free_port()
    stub = _spawn_stub(port)
    _wait_listening(port)
    lifecycle.write_record(port, pid=stub.pid, home=str(home), project_root=str(REPO))
    ok, message = lifecycle.stop(port)
    stub.wait(timeout=GRACEFUL_TEST_WAIT)
    assert ok is True, message
    assert str(stub.pid) in message, "the report must name the pid it stopped"
    assert "free" in message.lower() or "Stopped" in message
    assert stub.returncode is not None, "stop reported success but the process is alive"
    assert not lifecycle.port_open(port), "stop reported success but the port is still held"
    assert lifecycle.read_record(port) is None, "the stopped instance's record was left behind"


def test_stopping_something_not_running_is_a_clean_no_op(lifecycle_state):
    port = _free_port()
    ok, message = lifecycle.stop(port, force=True)
    assert ok is True
    assert "Nothing is listening" in message


def test_a_stale_record_does_not_block_a_fresh_start(lifecycle_state, tmp_path, monkeypatch):
    """A dead pid's record must not be read as "already running"."""
    state, home = lifecycle_state
    port = _free_port()
    lifecycle.write_record(port, pid=_dead_pid(), home=str(home), project_root=str(REPO))
    stub = tmp_path / "stub_stale.py"
    stub.write_text(
        "import json, os, sys\n"
        "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
        "port = int(sys.argv[1]); root = sys.argv[2]\n"
        "class H(BaseHTTPRequestHandler):\n"
        "    def log_message(self, *a): pass\n"
        "    def do_GET(self):\n"
        "        if self.path != '/health/identity':\n"
        "            self.send_response(404); self.end_headers(); return\n"
        "        body = json.dumps({'identity': {'service': 'vantage-backend',\n"
        "            'instance_id': 'BKT-fresh', 'pid': os.getpid(), 'source_root': root},\n"
        "            'code': {'stale': False}}).encode()\n"
        "        self.send_response(200)\n"
        "        self.send_header('Content-Length', str(len(body)))\n"
        "        self.end_headers(); self.wfile.write(body)\n"
        "HTTPServer(('127.0.0.1', port), H).serve_forever()\n",
        encoding="utf-8")
    monkeypatch.setattr(lifecycle, "spawn_command",
                        lambda p, host: [sys.executable, str(stub), str(p), str(REPO)])
    ok, message = lifecycle.start(port, home=str(home))
    try:
        assert ok is True, message
        record = lifecycle.read_record(port)
        assert record is not None and record["instance_id"] == "BKT-fresh"
    finally:
        lifecycle.stop(port, force=True)


def test_restart_replaces_the_instance_and_reports_both_halves(
        lifecycle_state, tmp_path, monkeypatch):
    state, home = lifecycle_state
    port = _free_port()
    stub = tmp_path / "stub_restart.py"
    stub.write_text(
        "import json, os, sys\n"
        "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
        "port = int(sys.argv[1]); root = sys.argv[2]\n"
        "class H(BaseHTTPRequestHandler):\n"
        "    def log_message(self, *a): pass\n"
        "    def do_GET(self):\n"
        "        if self.path != '/health/identity':\n"
        "            self.send_response(404); self.end_headers(); return\n"
        "        body = json.dumps({'identity': {'service': 'vantage-backend',\n"
        "            'instance_id': 'BKT-' + str(os.getpid()), 'pid': os.getpid(),\n"
        "            'source_root': root}, 'code': {'stale': False}}).encode()\n"
        "        self.send_response(200)\n"
        "        self.send_header('Content-Length', str(len(body)))\n"
        "        self.end_headers(); self.wfile.write(body)\n"
        "HTTPServer(('127.0.0.1', port), H).serve_forever()\n",
        encoding="utf-8")
    old = _spawn_stub(port)
    _wait_listening(port)
    lifecycle.write_record(port, pid=old.pid, home=str(home), project_root=str(REPO))
    monkeypatch.setattr(lifecycle, "spawn_command",
                        lambda p, host: [sys.executable, str(stub), str(p), str(REPO)])
    ok, message = lifecycle.restart(port, home=str(home))
    new_record = lifecycle.read_record(port)
    try:
        assert ok is True, message
        old.wait(timeout=GRACEFUL_TEST_WAIT)
        assert old.returncode is not None, "the old instance survived the restart"
        assert "Stopped pid" in message
        assert new_record is not None
        assert new_record["pid"] != old.pid, "restart re-stamped the old pid"
        assert lifecycle.port_open(port)
    finally:
        lifecycle.stop(port, force=True)


def test_restart_reports_failure_once_and_does_not_loop(
        lifecycle_state, tmp_path, monkeypatch):
    """A start that cannot come up is one failure, reported -- never a retry loop."""
    state, home = lifecycle_state
    port = _free_port()
    attempts = []

    def _always_fails(p, host):
        attempts.append(p)
        return [sys.executable, "-c", "import sys; sys.stderr.write('Vantage: boom\\n'); "
                                      "sys.exit(3)"]

    monkeypatch.setattr(lifecycle, "spawn_command", _always_fails)
    old = _spawn_stub(port)
    _wait_listening(port)
    lifecycle.write_record(port, pid=old.pid, home=str(home), project_root=str(REPO))
    ok, message = lifecycle.restart(port, home=str(home))
    assert ok is False
    assert "exited during startup" in message
    assert "boom" in message
    assert len(attempts) == 1, f"restart retried the start {len(attempts)} times"
    assert lifecycle.read_record(port) is None, "a failed start left an ownership record"
    old.wait(timeout=GRACEFUL_TEST_WAIT)


def test_a_start_that_never_answers_is_a_failure_not_a_success(
        lifecycle_state, tmp_path, monkeypatch):
    """A spawned process that holds the port but cannot name itself is not ready."""
    state, home = lifecycle_state
    port = _free_port()
    # Bind the port and sit there, never answering /health/identity.
    script = ("import socket, sys, time\n"
              "s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
              "s.bind(('127.0.0.1', int(sys.argv[1]))); s.listen(5)\n"
              "time.sleep(60)\n")
    monkeypatch.setattr(lifecycle, "spawn_command",
                        lambda p, host: [sys.executable, "-c", script, str(p)])
    monkeypatch.setattr(lifecycle, "READY_TIMEOUT_S", 2.0)
    ok, message = lifecycle.start(port, home=str(home))
    try:
        assert ok is False, message
        assert "did not answer /health/identity" in message
        assert "was stopped" in message, "the failed start left its process running"
        assert not lifecycle.port_open(port)
        assert lifecycle.read_record(port) is None
    finally:
        if lifecycle.port_open(port):
            lifecycle.stop(port, force=True)
