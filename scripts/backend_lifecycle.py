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
* Graceful first (SIGTERM), a bounded wait, then SIGKILL only with ``--force``.
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
        and str(identity.get("source_root", "")).startswith(str(REPO))
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
    }


# ---------------------------------------------------------------------------
# Acting
# ---------------------------------------------------------------------------

def _wait_gone(port: int, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not port_open(port):
            return True
        time.sleep(0.2)
    return False


def stop(port: int, *, force: bool = False) -> Tuple[bool, str]:
    info = describe(port)
    if info["state"] == "free":
        return True, "Nothing is listening; there is no backend to stop."
    if not info["safe_to_restart"]:
        return False, (
            f"Refusing to stop pid {info['pid']}: it is not the Vantage backend from "
            f"this checkout (state: {info['state']}). Inspect it yourself; nothing was signalled.")
    pid = int(info["pid"])
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True, f"pid {pid} had already exited."
    except PermissionError as exc:
        return False, f"Cannot signal pid {pid}: {exc}. Nothing was stopped."
    if _wait_gone(port, GRACEFUL_WAIT_S):
        return True, f"Stopped pid {pid} gracefully after SIGTERM."
    if not force:
        return False, (
            f"pid {pid} did not exit within {GRACEFUL_WAIT_S:.0f}s of SIGTERM. It is still "
            "running and nothing else was signalled. Re-run with --force to send SIGKILL.")
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError as exc:
        return False, f"SIGKILL to pid {pid} failed: {exc}."
    return (_wait_gone(port, 5.0), f"Sent SIGKILL to pid {pid}.")


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
        return start(port, home=home)
    except PermissionError as exc:
        return False, f"Cannot signal pid {pid}: {exc}. Nothing was stopped."
    if not _wait_gone(port, GRACEFUL_WAIT_S):
        if not force:
            return False, (f"pid {pid} did not exit within {GRACEFUL_WAIT_S:.0f}s. It is "
                           "still running and nothing else was signalled. Re-run with "
                           "--force to escalate after re-checking it is still ours.")
        os.kill(pid, signal.SIGKILL)
        if not _wait_gone(port, 5.0):
            return False, f"pid {pid} survived SIGKILL; the port is still held."
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


def start(port: int, home: Optional[str] = None) -> Tuple[bool, str]:
    """Start the backend from this checkout, refusing to add a second instance."""
    if port_open(port):
        info = describe(port)
        return False, (
            f"Port {port} is already in use (state: {info['state']}, pid {info['pid']}). "
            "Not starting a second instance. Use `restart` instead.")
    env = dict(os.environ)
    if home:
        env["JARVIS_HOME"] = home
    host = _bind_host(home or env.get("JARVIS_HOME"))
    # Detached so the server outlives this helper; its log is the operator's.
    log_path = Path(home or env.get("JARVIS_HOME") or Path.home() / ".jarvis-forge")
    log_path.mkdir(parents=True, exist_ok=True)
    log_file = log_path / "backend.log"
    try:
        with log_file.open("ab") as handle:
            proc = subprocess.Popen(
                [str(REPO / ".venv" / "bin" / "python"), str(REPO / "run.py"),
                 "serve", "--port", str(port), "--host", host],
                cwd=str(REPO), env=env, stdout=handle, stderr=subprocess.STDOUT,
                start_new_session=True)
    except OSError as exc:
        return False, f"Could not start the backend: {exc}"
    # Wait for the identity endpoint, not merely for the port to accept.
    deadline = time.time() + 30
    while time.time() < deadline:
        if describe(port)["state"] == "ours":
            return True, f"Started pid {proc.pid}; log: {log_file}"
        if proc.poll() is not None:
            # Say WHAT the process said. "exited during startup (code 0)" alone
            # hides the common, actionable cause -- the single-instance claim on
            # the data directory being held by another server -- and leaves a
            # person staring at a dead port with no next step.
            reason = _startup_failure_reason(log_file)
            detail = f" It said: {reason}" if reason else ""
            return False, (f"The backend exited during startup (code {proc.returncode}). "
                           f"{detail} See {log_file}.")
        time.sleep(0.25)
    return False, f"Started pid {proc.pid} but it did not answer /health/identity in 30s."


def restart(port: int, *, force: bool = False, home: Optional[str] = None) -> Tuple[bool, str]:
    ok, message = stop(port, force=force)
    if not ok:
        return False, message
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