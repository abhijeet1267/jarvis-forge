#!/usr/bin/env python3
"""Inspect, and only when safe, restart the Vantage backend.

Why this exists: a backend started the day before a routing change kept serving
old code, and nothing said so. Restarting it by hand is where the risk lives —
``pkill -f uvicorn`` would take down the companion gateway and any other project's
server too, and killing by name is how a test run silently destroys real work.

This tool refuses to guess. It talks to the candidate through
``/health/identity`` and acts only on a process that says it is
``vantage-backend`` *and* runs from this checkout:

    python scripts/backend_lifecycle.py status     # who is on the port?
    python scripts/backend_lifecycle.py restart    # only if it is ours

Rules:
* One candidate: whatever is listening on the port. No process-name matching.
* Before signalling anything, confirm the identity response names this service
  and this ``source_root``. A stranger's server is reported, never touched.
* A start writes an ownership record beside its port -- pid, start time, the
  profile (``JARVIS_HOME``) and the project root served. A later start reads it:
  the same profile and root that is answering is "already running" (no second
  process); a different profile or root is refused, naming the holder and how
  to stop it; a dead pid is a stale record and the port may be taken.
* Starting is only a success when ``/health/identity`` answers. A spawned
  process is not a healthy backend.
* Graceful first (SIGTERM), a bounded wait, then SIGKILL only with ``--force``
  (``restart`` escalates after its bounded wait, never in a loop).
* Refuse to start a second instance: if the port is already owned, say so.
* Secrets are never printed; only the identity fields the server publishes.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

REPO = Path(__file__).resolve().parents[1]
DEFAULT_PORT = 8765
EXPECTED_SERVICE = "vantage-backend"
#: How long a graceful stop may take before we report failure instead of hiding it.
GRACEFUL_WAIT_S = 15.0
#: How long a spawned backend has to answer ``/health/identity`` before the start
#: is reported as failed. A spawned process is not a healthy backend.
READY_TIMEOUT_S = 30.0
#: How long a SIGKILLed process gets to disappear before we say it survived.
KILL_WAIT_S = 5.0
#: The record a start writes lives in one per-user, port-keyed directory -- not
#: in a profile's home, which a *different* profile could not find or judge.
STATE_DIR_ENV = "VANTAGE_LIFECYCLE_DIR"


# ---------------------------------------------------------------------------
# Talking to the candidate
# ---------------------------------------------------------------------------

def _get(port: int, path: str, timeout: float = 3.0) -> Optional[Dict[str, Any]]:
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}{path}", timeout=timeout) as response:
            return json.loads(response.read() or b"{}")
    except (urllib.error.URLError, OSError, ValueError):
        return None


def process_evidence(pid: int) -> Dict[str, Any]:
    """Facts about one pid, from the OS rather than from the process's own claim.

    A legacy backend cannot answer ``/health/identity``, so the only way to prove
    it is *ours* is to look at what the operating system knows: who owns it, what
    it runs, from where, and on which address it listens. Name matching is never
    used on its own -- "python" is every python on the machine.
    """
    def _run(args: list) -> Optional[str]:
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=5, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        return result.stdout.strip() if result.returncode == 0 else None

    uid_raw = _run(["ps", "-o", "uid=", "-p", str(pid)])
    try:
        uid = int(uid_raw) if uid_raw else None
    except ValueError:
        uid = None
    cwd_raw = _run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"])
    cwd = next((line[1:] for line in (cwd_raw or "").splitlines() if line.startswith("n")), "")
    exe_raw = _run(["lsof", "-a", "-p", str(pid), "-d", "txt", "-Fn"])
    exe = next((line[1:] for line in (exe_raw or "").splitlines() if line.startswith("n")), "")
    return {
        "pid": pid,
        "uid": uid,
        "command": _run(["ps", "-o", "command=", "-p", str(pid)]) or "",
        "cwd": cwd,
        "executable": exe,
    }


def listening_pid(port: int) -> Optional[int]:
    """The pid listening on ``port``, via ``lsof`` (no name matching)."""
    try:
        result = subprocess.run(
            ["lsof", "-nP", "-tiTCP:%d" % port, "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    pids = [line.strip() for line in (result.stdout or "").splitlines() if line.strip().isdigit()]
    if not pids:
        return None
    # One listener is the normal case; more than one is a fact worth failing on.
    return int(pids[0]) if len(pids) == 1 else -1


def port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


# ---------------------------------------------------------------------------
# Ownership: which process this tool started, for which profile and root
# ---------------------------------------------------------------------------
#
# The record is per user and keyed by port, not stored inside a profile's home:
# the decision it feeds is *between* profiles, so a record a different profile
# could not see would be useless.

def _resolved_path(value: Any) -> Optional[Path]:
    if not value:
        return None
    try:
        return Path(str(value)).expanduser().resolve()
    except (OSError, RuntimeError):
        return None


def _profile_home(home: Optional[str]) -> Path:
    """The profile directory a started backend will use, as an absolute path."""
    raw = home or os.environ.get("JARVIS_HOME") or (Path.home() / ".jarvis-forge")
    return _resolved_path(raw) or Path(str(raw)).expanduser()


def _same_root(candidate: Any, expected: Path) -> bool:
    """Whether ``candidate`` names exactly ``expected`` -- not merely a prefix.

    ``startswith`` would make ``/x/Vantage-backup`` the same checkout as
    ``/x/Vantage``; two trees must never be mistaken for one.
    """
    resolved = _resolved_path(candidate)
    wanted = _resolved_path(expected)
    return resolved is not None and wanted is not None and resolved == wanted


def _state_dir() -> Path:
    override = os.environ.get(STATE_DIR_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".vantage" / "lifecycle"


def record_path(port: int) -> Path:
    return _state_dir() / f"backend-{port}.json"


def read_record(port: int) -> Optional[Dict[str, Any]]:
    """The ownership record for ``port``, or ``None`` if absent or unreadable."""
    try:
        data = json.loads(record_path(port).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_record(port: int, *, pid: int, home: str, project_root: str,
                 instance_id: Optional[str] = None,
                 started_at: Optional[float] = None) -> bool:
    """Write the ownership record atomically; ``False`` if it could not be written."""
    path = record_path(port)
    payload: Dict[str, Any] = {
        "schema": 1,
        "pid": pid,
        "port": port,
        "started_at": time.time() if started_at is None else started_at,
        "home": home,
        "project_root": project_root,
    }
    if instance_id:
        payload["instance_id"] = instance_id
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        return False
    return True


def clear_record(port: int, *, only_pid: Optional[int] = None) -> None:
    """Remove the record for ``port`` (only when it names ``only_pid``, if given)."""
    if only_pid is not None:
        record = read_record(port)
        if record is not None and record.get("pid") != only_pid:
            return
    try:
        record_path(port).unlink(missing_ok=True)
    except OSError:
        pass


def _pid_gone(pid: Any) -> bool:
    """Whether ``pid`` has positively exited.

    ``os.kill(pid, 0)`` alone is not enough: a child that exited but has not
    been reaped is still a process to the OS, which would make every stop of a
    process this tool spawned look like a failure. ``waitpid`` reaps it when it
    is ours; otherwise the signal probe is the answer.
    """
    if not isinstance(pid, int) or pid <= 0:
        return True
    try:
        reaped, _ = os.waitpid(pid, os.WNOHANG)
        return reaped == pid
    except ChildProcessError:
        pass
    except OSError:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    except OSError:
        return False
    return False


def pid_alive(pid: Any) -> bool:
    return not _pid_gone(pid)


def _wait_pid_exit(pid: int, timeout: float) -> bool:
    """Wait, bounded, until ``pid`` has exited. No unbounded poll."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _pid_gone(pid):
            return True
        time.sleep(0.1)
    return _pid_gone(pid)


def _terminate(pid: int, *, force: bool) -> Tuple[bool, str]:
    """One bounded stop attempt: SIGTERM, wait for exit, SIGKILL only after.

    Returns success only when the process is confirmed gone. ``force`` is the
    escalation permission, never a first choice.
    """
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True, f"pid {pid} had already exited."
    except PermissionError as exc:
        return False, f"Cannot signal pid {pid}: {exc}. Nothing was stopped."
    if _wait_pid_exit(pid, GRACEFUL_WAIT_S):
        return True, f"Stopped pid {pid} gracefully after SIGTERM."
    if not force:
        return False, (
            f"pid {pid} did not exit within {GRACEFUL_WAIT_S:.0f}s of SIGTERM. It is still "
            "running and nothing else was signalled. Re-run with --force to send SIGKILL.")
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True, f"pid {pid} exited just before SIGKILL could be sent."
    except OSError as exc:
        return False, f"SIGKILL to pid {pid} failed: {exc}."
    if _wait_pid_exit(pid, KILL_WAIT_S):
        return True, (f"Stopped pid {pid} with SIGKILL after the "
                      f"{GRACEFUL_WAIT_S:.0f}s graceful wait expired.")
    return False, f"pid {pid} survived SIGKILL; the port may still be held."


def legacy_verdict(evidence: Dict[str, Any], repo: Path) -> Tuple[str, str]:
    """Whether a pid that cannot answer identity is provably *our* old backend.

    Combined evidence, all required. Any one alone is insufficient:
    * same user as this process -- somebody else's server is never ours to stop;
    * a working directory inside this checkout (this is the decisive one: a
      process started from here is running this project's `run.py`);
    * a command line that runs this project's launcher (`run.py ... serve`) or a
      uvicorn app path from this repository.

    Process *name* is never used. A stranger's python that happens to be called
    "python" fails every check above, which is the point.
    """
    import os as _os

    uid = evidence.get("uid")
    if uid is not None and uid != _os.getuid():
        return "foreign", f"owned by uid {uid}, not this user"
    cwd = str(evidence.get("cwd") or "")
    repo_s = str(repo)
    if not cwd or not (cwd == repo_s or cwd.startswith(repo_s + _os.sep)):
        return "foreign", f"working directory {cwd or 'unknown'} is outside {repo_s}"
    command = str(evidence.get("command") or "")
    # The launcher is `run.py serve`; accept it wherever it appears in the command
    # line (an absolute path, a relative one, or after a `-c` snippet), because
    # position is a detail of how it was invoked, not of what it is.
    runs_launcher = "run.py" in command and "serve" in command
    # Or uvicorn pointed at this project's app module.
    runs_uvicorn_here = "jarvis.api.app" in command
    if not (runs_launcher or runs_uvicorn_here):
        return "foreign", "command line does not run this project's backend entry point"
    return "candidate", "same user, started from this checkout, running its backend entry point"


def describe(port: int) -> Dict[str, Any]:
    """Who is on the port, and whether it is the backend this checkout owns."""
    pid = listening_pid(port)
    payload = _get(port, "/health/identity")
    identity = (payload or {}).get("identity") or {}
    code = (payload or {}).get("code") or {}

    is_ours = bool(
        identity.get("service") == EXPECTED_SERVICE
        # Exact root, not a prefix: a sibling directory named
        # `Vantage-backup` is a different checkout and must not be acted on.
        and _same_root(identity.get("source_root"), REPO)
    )
    legacy: Optional[Dict[str, Any]] = None
    # A 200 that carries no identity block answers *something*, but says nothing
    # about who. Treat it exactly like silence: it cannot prove ownership, so the
    # OS evidence decides. (A stand-in server returning `{"detail":"Not Found"}`
    # for every path was otherwise classified as a different Vantage checkout.)
    if not identity:
        payload = None
    if pid is None and not port_open(port):
        state = "free"
    elif payload is None:
        # Something holds the port and does not answer as a Vantage backend.
        # It is either an *older Vantage* (no /health/identity) or a stranger.
        # The OS evidence decides, and a stranger is never "foreign" merely for
        # being unknown -- it must positively look like ours to be a candidate.
        evidence = process_evidence(pid) if isinstance(pid, int) and pid > 0 else {}
        verdict, reason = legacy_verdict(evidence, REPO)
        if verdict == "candidate":
            state = "legacy"
            legacy = {**evidence, "reason": reason}
        else:
            state = "foreign"
            legacy = {**evidence, "reason": reason}
    elif is_ours:
        state = "ours"
    else:
        state = "other-vantage-checkout"

    return {
        "port": port,
        "pid": pid,
        "state": state,
        "identity": identity,
        "code": code,
        # Unknown is not "fresh". A server that cannot report identity (an older
        # build) tells us nothing about its code age, and printing False here
        # read as a clean bill of health for exactly the stale process this tool
        # exists to catch.
        "stale": code.get("stale") if isinstance(code, dict) and "stale" in code else None,
        "legacy": legacy,
        # A legacy candidate is restartable only through the explicit migration
        # path, which revalidates before signalling; `safe_to_restart` stays for
        # the proven-ours case and `migratable` for the proven-legacy one.
        "safe_to_restart": state == "ours" and pid not in (None, -1),
        "migratable": state == "legacy" and pid not in (None, -1),
        # Who `start` believes owns the port, for this profile and this checkout.
        # Present so a refusal can name the holder instead of guessing at it.
        "owner": ownership_payload(port),
    }


# ---------------------------------------------------------------------------
# Acting
# ---------------------------------------------------------------------------

def ownership_verdict(port: int, *, home: Path, project_root: Path) -> Tuple[str, str, Optional[Dict[str, Any]]]:
    """Compare the ownership record for ``port`` with the profile being started.

    Returns ``(relation, reason, record)`` where relation is one of:

    ``same``             record names this profile, this root, and the pid now listening
    ``different-profile`` a live backend from this root, but for another JARVIS_HOME
    ``other-root``       a live backend from a different checkout
    ``changed-hands``    the record's process is alive but is not the listener
    ``unverified``       the listener's pid could not be determined
    ``stale``            the record's pid has exited (the port may be taken when free)
    ``missing``          no record at all

    Only ``same`` may be treated as "this app instance"; only ``stale``/``missing``
    may be treated as permission to take a *free* port. Holding the port is never
    permission to signal the holder.
    """
    record = read_record(port)
    if record is None:
        return "missing", "no ownership record was written for this port", None
    recorded_pid = record.get("pid")
    if not isinstance(recorded_pid, int) or recorded_pid <= 0:
        return "stale", "the ownership record has no usable pid", record
    if not pid_alive(recorded_pid):
        return "stale", f"the recorded pid {recorded_pid} has exited", record
    listener = listening_pid(port)
    if listener is None or listener == -1:
        return "unverified", "the listening pid could not be determined", record
    if listener != recorded_pid:
        return "changed-hands", (
            f"the record names pid {recorded_pid}, but pid {listener} now holds the port"), record
    if not _same_root(record.get("project_root"), project_root):
        return "other-root", (
            f"it serves {record.get('project_root') or 'an unknown project root'}, "
            f"not {project_root}"), record
    recorded_home = _resolved_path(record.get("home"))
    if recorded_home != _resolved_path(home):
        return "different-profile", (
            f"it was started for profile {record.get('home') or 'unknown'}, "
            f"not {home}"), record
    return "same", "the record names this profile and this project root", record


def ownership_payload(port: int) -> Optional[Dict[str, Any]]:
    """The record for ``port`` plus whether its pid is still alive; ``None`` if absent."""
    record = read_record(port)
    if record is None:
        return None
    return {**record, "pid_alive": pid_alive(record.get("pid"))}


def spawn_command(port: int, host: str) -> list:
    """The argv a started backend runs. A seam, so a test can serve a stub.

    The real command is this checkout's own interpreter and launcher: an
    app-initiated start and a CLI start must produce the same process shape.
    """
    return [str(REPO / ".venv" / "bin" / "python"), str(REPO / "run.py"),
            "serve", "--port", str(port), "--host", host]


def stop(port: int, *, force: bool = False) -> Tuple[bool, str]:
    """Stop the proven-ours backend on ``port`` and confirm the pid actually exited.

    Success means the process is gone, not that a signal was sent. The pid is
    always reported, so the operator knows exactly what was acted on.
    """
    info = describe(port)
    if info["state"] == "free":
        return True, "Nothing is listening; there is no backend to stop."
    if not info["safe_to_restart"]:
        return False, (
            f"Refusing to stop pid {info['pid']}: it is not the Vantage backend from "
            f"this checkout (state: {info['state']}). Inspect it yourself; nothing was signalled.")
    pid = int(info["pid"])
    ok, message = _terminate(pid, force=force)
    if ok:
        # The record is only removed when it names the process that just exited,
        # so a stop cannot erase a newer instance's claim.
        clear_record(port, only_pid=pid)
        if not port_open(port):
            message = f"{message} Port {port} is free."
    return ok, message


def migrate(port: int, *, confirm: bool = False, force: bool = False,
            home: Optional[str] = None) -> Tuple[bool, str]:
    """Replace a *provably ours* legacy backend with the current build.

    Refuses without ``confirm`` (this is an explicit act), refuses unless the
    candidate's evidence still says it is ours **at the moment of acting** (not
    merely when it was described), re-checks the pid still owns the port, and
    only then sends SIGTERM. Force is a second, separate decision.
    """
    if not confirm:
        return False, ("Migration stops a running backend. Re-run with --confirm to do "
                       "it deliberately.")
    info = describe(port)
    if info["state"] == "free":
        return start(port, home=home)
    if info["state"] == "ours":
        return restart(port, force=force, home=home)
    if info["state"] != "legacy":
        return False, (
            f"Refusing to migrate: the listener on {port} is not provably this checkout's "
            f"backend (state: {info['state']}). "
            f"{(info.get('legacy') or {}).get('reason', '')} Nothing was signalled.")

    pid = int(info["pid"])
    # Revalidate immediately before signalling: the pid could have exited and been
    # reused, or the port could have changed hands between describe() and now.
    current = listening_pid(port)
    if current != pid:
        return False, (f"The listener changed while preparing the migration (was {pid}, "
                       f"now {current}). Nothing was signalled; re-run status.")
    evidence = process_evidence(pid)
    verdict, reason = legacy_verdict(evidence, REPO)
    if verdict != "candidate":
        return False, (f"Refusing to migrate pid {pid}: ownership no longer proven "
                       f"({reason}). Nothing was signalled.")

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        clear_record(port, only_pid=pid)
        return start(port, home=home)
    except PermissionError as exc:
        return False, f"Cannot signal pid {pid}: {exc}. Nothing was stopped."
    if not _wait_pid_exit(pid, GRACEFUL_WAIT_S):
        if not force:
            return False, (f"pid {pid} did not exit within {GRACEFUL_WAIT_S:.0f}s. It is "
                           "still running and nothing else was signalled. Re-run with "
                           "--force to escalate after re-checking it is still ours.")
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as exc:
            return False, f"SIGKILL to pid {pid} failed: {exc}."
        if not _wait_pid_exit(pid, KILL_WAIT_S):
            return False, f"pid {pid} survived SIGKILL; the port is still held."
    # The old process is confirmed gone; its record must not describe the new one.
    clear_record(port, only_pid=pid)
    return start(port, home=home)


def _startup_failure_reason(log_file: Path, max_bytes: int = 8192) -> str:
    """The most useful line the failed start wrote, or "" if there is none.

    The launcher prints a plain ``Vantage: ...`` sentence for the refusals a
    person can act on (another server holds the data directory, the port is
    answered, a dependency is missing). Those are printed, not logged, so the
    tail of the file is where they are. Only the last few KiB are read: the log
    is append-only and shared across restarts.
    """
    try:
        with log_file.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            tail = handle.read().decode("utf-8", "replace")
    except OSError:
        return ""
    for line in reversed(tail.splitlines()):
        stripped = line.strip()
        if stripped.startswith("Vantage:") or stripped.startswith("Traceback ") \
                or "Error:" in stripped or "error:" in stripped:
            return stripped[:300]
    return ""

def _bind_host(home: Optional[str]) -> str:
    """The host to bind, from the project's own LAN switch.

    The backend was running with ``--host 0.0.0.0`` because LAN mode is on (the
    companion gateway, and the phone, need it). A restart must not silently narrow
    that to loopback and break pairing -- and must not silently widen a loopback
    install either. The switch is the existing source of truth, read the same way
    the backend reads it.
    """
    root = Path(home or os.environ.get("JARVIS_HOME") or Path.home() / ".jarvis-forge")
    try:
        text = (root / "lan_mode").read_text(encoding="utf-8").strip().lower()
    except OSError:
        return "127.0.0.1"
    return "0.0.0.0" if text in ("true", "1", "on", "yes") else "127.0.0.1"


def wait_until_ready(port: int, proc: "subprocess.Popen[Any]",
                     timeout: Optional[float] = None) -> Tuple[Optional[Dict[str, Any]], str]:
    """Poll ``/health/identity`` until *the process we spawned* answers, or ``timeout``.

    ``timeout`` defaults to :data:`READY_TIMEOUT_S` read at call time, so a
    caller (or a test) can shorten it without patching a function default.

    Returns the identity payload on success and ``None`` otherwise, with a
    sentence describing what happened. Bounded by one deadline, and it stops
    early the moment the child exits -- a start that has already failed must be
    reported now, not after the timeout. Readiness means the payload names this
    service, this checkout, and the spawned pid: a port that accepts a
    connection, or an unrelated server that got there first, is not this start
    succeeding.
    """
    if timeout is None:
        timeout = READY_TIMEOUT_S
    deadline = time.time() + timeout
    last_seen = ""
    while time.time() < deadline:
        payload = _get(port, "/health/identity", timeout=1.0)
        identity = (payload or {}).get("identity") or {}
        if (identity.get("service") == EXPECTED_SERVICE
                and _same_root(identity.get("source_root"), REPO)
                and identity.get("pid") == proc.pid):
            return payload, "answers /health/identity as this process"
        if identity:
            last_seen = (f" (port {port} answers as service {identity.get('service')!r} "
                         f"pid {identity.get('pid')}, not this start's pid {proc.pid})")
        if proc.poll() is not None:
            return None, f"exited during startup (code {proc.returncode}){last_seen}"
        time.sleep(0.2)
    return None, f"did not answer /health/identity in {timeout:.0f}s{last_seen}"


def _refuse_held_port(port: int, profile: Path) -> Tuple[bool, str]:
    """The decision when the port is already held: same instance, or refused.

    A process is treated as "this instance" only when the ownership record, the
    listener, the profile, and the project root all agree, and the backend
    answers identity for this checkout. Everything else is refused *without*
    signalling anything: an unrelated process is never this tool's to stop, and
    a second backend is never started over a live one.
    """
    info = describe(port)
    relation, reason, record = ownership_verdict(port, home=profile, project_root=REPO)
    record_note = ""
    if record is not None:
        record_note = (f" Ownership record: pid {record.get('pid')}, profile "
                       f"{record.get('home')}, root {record.get('project_root')}.")
    holder = f"pid {info['pid']}" if info["pid"] not in (None, -1) else "an unknown pid"

    if relation == "same" and info["state"] == "ours":
        return True, (f"Already running: pid {info['pid']} is this instance on port {port} "
                      f"(profile {profile}, root {REPO}) and answers /health/identity. "
                      "No second instance was started.")

    if relation == "same":
        return False, (
            f"Port {port} is already in use by pid {info['pid']}, which the ownership "
            f"record names, but it is not answering as this checkout's backend (state: "
            f"{info['state']}). Not starting a second instance; nothing was signalled. "
            f"Run `status` to inspect it.")

    if info["state"] == "free":
        # The listener vanished between the probe and now; the caller may start.
        return True, "the port became free"

    how = (f"Stop it from its own checkout with `.venv/bin/python scripts/backend_lifecycle.py "
           f"stop --port {port}` (it refuses to act on anything it cannot prove is its own), "
           f"or inspect `status --json` first.")
    return False, (
        f"Port {port} is already in use by {holder} ({info['state']}; {reason}), so a second "
        f"instance will not be started.{record_note} This tool did not start it and will not "
        f"stop it. {how}")


def start(port: int, home: Optional[str] = None) -> Tuple[bool, str]:
    """Start the backend, handshake for readiness, and report a real outcome.

    Never adds a second instance: a live port is resolved through
    :func:`_refuse_held_port`. A spawned process is not a success -- only the
    spawned process answering ``/health/identity`` is.
    """
    profile = _profile_home(home)
    if port_open(port):
        ok, message = _refuse_held_port(port, profile)
        if not ok or describe(port)["state"] != "free":
            return ok, message
        # A listener that vanished between the probe and describe(): fall through
        # to a normal start, which fails visibly if it was real after all.
    env = dict(os.environ)
    env["JARVIS_HOME"] = str(profile)
    host = _bind_host(str(profile))
    log_file = profile / "backend.log"
    try:
        profile.mkdir(parents=True, exist_ok=True)
        with log_file.open("ab") as handle:
            proc = subprocess.Popen(
                spawn_command(port, host),
                cwd=str(REPO), env=env, stdout=handle, stderr=subprocess.STDOUT,
                start_new_session=True)
    except OSError as exc:
        return False, f"Could not start the backend: {exc}"

    # Stamp ownership before waiting for readiness: a concurrent start must see
    # a record naming a live pid rather than conclude the port is unowned.
    write_record(port, pid=proc.pid, home=str(profile), project_root=str(REPO))

    ready, detail = wait_until_ready(port, proc)
    if ready is None:
        # A process this tool started and that failed is ours to clean up: do
        # not leave an orphan for the next launch to trip over.
        leftovers = ""
        if proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
            if not _wait_pid_exit(proc.pid, GRACEFUL_WAIT_S):
                try:
                    proc.kill()
                except OSError:
                    pass
                _wait_pid_exit(proc.pid, KILL_WAIT_S)
            leftovers = f" pid {proc.pid} was stopped."
        clear_record(port, only_pid=proc.pid)
        reason = _startup_failure_reason(log_file)
        said = f" It said: {reason}" if reason else ""
        return False, (f"The backend {detail}.{leftovers}{said} See {log_file}.")
    identity = ready.get("identity") or {}
    write_record(port, pid=proc.pid, home=str(profile), project_root=str(REPO),
                 instance_id=str(identity.get("instance_id") or "") or None)
    return True, (f"Started pid {proc.pid} for profile {profile}; it answers "
                  f"/health/identity. Log: {log_file}")


def restart(port: int, *, force: bool = False, home: Optional[str] = None) -> Tuple[bool, str]:
    """One bounded restart: stop (confirmed), then start (handshake-confirmed).

    Exactly one attempt. A failure is reported, never retried, so a backend that
    cannot start cannot become a restart loop.

    The stop escalates to SIGKILL only after the graceful wait has expired, and
    only for a process ``stop`` has just proven is this checkout's own backend:
    leaving the old process behind is what makes a restart untrustworthy, while
    killing sooner than the backend needs is what makes it destructive.
    """
    if port_open(port):
        ok, message = stop(port, force=True)
        if not ok:
            return False, message
    else:
        message = "Nothing was listening; starting a fresh backend."
    started, start_message = start(port, home=home)
    return started, f"{message} {start_message}"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("action", choices=["status", "restart", "stop", "start", "migrate"])
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--force", action="store_true",
                        help="send SIGKILL if a graceful stop times out")
    parser.add_argument("--home", default=None, help="JARVIS_HOME for a started backend")
    parser.add_argument("--json", action="store_true", help="machine-readable status")
    parser.add_argument("--confirm", action="store_true",
                        help="required for `migrate`: stops a proven-legacy backend")
    args = parser.parse_args(argv)

    if args.action == "status":
        info = describe(args.port)
        if args.json:
            print(json.dumps(info, indent=2))
        else:
            ident = info["identity"]
            print(f"port      : {info['port']}")
            print(f"state     : {info['state']}")
            print(f"pid       : {info['pid']}")
            if ident:
                print(f"instance  : {ident.get('instance_id')}")
                print(f"version   : {ident.get('app_version')}  revision {ident.get('revision')}")
                print(f"source    : {ident.get('source_root')}")
            print(f"stale code: {info['stale'] if info['stale'] is not None else 'unknown (cannot report identity)'}")
            owner = info.get("owner")
            if owner:
                alive = "alive" if owner.get("pid_alive") else "exited (stale record)"
                print(f"owner     : pid {owner.get('pid')} ({alive}), profile "
                      f"{owner.get('home')}, root {owner.get('project_root')}")
            else:
                print("owner     : no ownership record (not started by this tool)")
            if info["code"].get("note"):
                print(f"note      : {info['code']['note']}")
            if info["state"] == "legacy":
                print("\nThis is an OLDER Vantage backend from this checkout (it predates "
                      "/health/identity).\nIt can be migrated explicitly: "
                      "`migrate --confirm` stops it gracefully and starts the current build.\n"
                      f"Evidence: {(info.get('legacy') or {}).get('reason', '')}")
            elif info["state"] not in ("ours", "free"):
                print("\nThis is NOT the backend from this checkout. Nothing will be "
                      "stopped automatically; inspect it first.\n"
                      f"Evidence: {(info.get('legacy') or {}).get('reason', '')}")
        return 0 if info["state"] in ("ours", "free") else 2

    if args.action == "stop":
        ok, message = stop(args.port, force=args.force)
    elif args.action == "migrate":
        ok, message = migrate(args.port, confirm=args.confirm, force=args.force,
                              home=args.home)
    elif args.action == "start":
        ok, message = start(args.port, home=args.home)
    else:
        ok, message = restart(args.port, force=args.force, home=args.home)
    print(message)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())