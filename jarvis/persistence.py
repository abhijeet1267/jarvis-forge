"""Host persistence: a login item for the laptop process, and nothing more than that.

The product has one shape for the server process: the desktop panel starts it and the panel's
death stops it. That was the right fix for Phase 40 (an orphan server holding the wifi port open
with no window able to close it is worse than no server), and it is also the reason a phone in
somebody's hand is talking to a machine that has to be sitting open. Phase 56 hit the same wall
from the other side: a macOS privacy prompt cannot be raised by a helper binary, only by the app
process, so the permission ask has nowhere to live until a long-running host exists.

Three rules make this safe enough to ship, and each of them has already cost this project a bug:

1. **No secret in the launch config.** A LaunchAgent plist is a world-readable file that outlives
   the session. It names an interpreter, a program, and the three variables the server needs. The
   token stays in ``<home>/api_token`` at mode 0600, where it already is (the Phase 45a precedent:
   the key lives beside the data, never inside the bundle).
2. **The watchdog is not inherited.** ``jarvis.process`` arms a guardian from
   ``JARVIS_GUARDIAN_PID`` and exits within two seconds when that pid is gone. A service started by
   launchd is parented by launchd, so carrying the variable over would make the new service kill
   itself on every login. ``render_plist`` therefore writes an explicit, closed list of variables
   rather than passing the environment through.
3. **The answer comes from re-reading.** ``launchctl`` returning 0 is not proof a service is
   loaded, and a plist on disk is not proof anything is running. Every ``enable()`` /
   ``disable()`` ends in a fresh ``status()``.

Why this module writes the file itself instead of using the file tools: ``Library/LaunchAgents``
is in ``PROTECTED_SUBPATHS`` on purpose -- it is code that runs without the user present -- and
that guard must keep refusing an ordinary write, sentence or not. Widening the guard so a tool
could write here would hand every sentence the power to install a login item. So persistence is
its own, confirmation-gated capability, and the guard is untouched (both facts are tested).
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import socket
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from .config import Settings, get_settings
from .logging_setup import get_logger

log = get_logger("jarvis.persistence")

#: Durable state. The bundle identifier kept its original name through the Vantage rename for the
#: same reason, and for the same consequence: renaming this orphans an installed agent that keeps
#: running under the old label with nothing left pointing at it.
AGENT_LABEL = "com.jarvisforge.vantage"

LAUNCHCTL = "/bin/launchctl"

#: Patched by tests to stand in for the running OS.
PLATFORM = sys.platform

_CLAIM_FILE = "server.claim"
_PID_RE = re.compile(r"pid\s*=\s*(\d+)")

#: How long a teardown is allowed to take to become visible. A few hundred milliseconds in
#: practice; three seconds so a slow machine is not read as a refusal.
DEFAULT_SETTLE_S = 3.0


def agent_path(agents_dir: Optional[Path] = None) -> Path:
    """Where the agent lives: the user's own folder, never a privileged one.

    ``~/Library/LaunchAgents`` runs as the user, at login, with no elevation. A Daemon would run
    as root, which is not something a personal assistant needs and is not something to discover
    later.
    """
    base = Path(agents_dir) if agents_dir else Path.home() / "Library" / "LaunchAgents"
    return base / f"{AGENT_LABEL}.plist"


def render_plist(*, python: str, project_dir: str, home: str, host: str,
                 port: int, label: str = AGENT_LABEL) -> str:
    """The launchd job description, written from a closed list of fields.

    Two omissions are load-bearing and tested: no token (see the module docstring) and no
    ``JARVIS_GUARDIAN_PID`` -- a stale guardian pid is a service that exits by design two seconds
    after it starts. ``PYTHONDONTWRITEBYTECODE`` is here for the same reason the launcher sets it:
    a ``.pyc`` written inside a signed bundle breaks its seal, which is what "Vantage is damaged"
    means.
    """
    program = str(Path(project_dir) / "run.py")
    body: Dict[str, Any] = {
        "Label": label,
        "ProgramArguments": [str(python), program],
        "WorkingDirectory": str(project_dir),
        "RunAtLoad": True,
        # Restart after a crash; stay down after a clean exit. KeepAlive=true would turn a port
        # conflict into a restart loop, because a failed bind exits non-zero.
        "KeepAlive": {"SuccessfulExit": False},
        "StandardOutPath": str(Path(home) / "persistence.out.log"),
        "StandardErrorPath": str(Path(home) / "persistence.err.log"),
        "EnvironmentVariables": {
            "JARVIS_HOME": str(home),
            "JARVIS_HOST": str(host),
            "JARVIS_PORT": str(port),
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    }
    return plistlib.dumps(body, fmt=plistlib.FMT_XML).decode("utf-8")


def _default_runner(argv: list[str]) -> Tuple[int, str, str]:
    """Run launchd's own CLI with no shell, so no argument can be interpolated."""
    import subprocess

    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=15.0)
    except FileNotFoundError:
        return 127, "", f"{argv[0] if argv else ''} is not installed"
    except OSError as exc:
        return 126, "", f"could not run {argv[0] if argv else ''}: {exc}"
    return proc.returncode, proc.stdout, proc.stderr


def _domain() -> str:
    return f"gui/{os.getuid()}"


# ---------------------------------------------------------------------------
# one machine, one server
# ---------------------------------------------------------------------------


def claim_path(home: Path) -> Path:
    return Path(home) / _CLAIM_FILE


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # alive, just not ours to signal
    return True


def read_claim(home: Path) -> Optional[Dict[str, Any]]:
    """Who says it is serving this data directory right now, or None.

    A corrupt claim is treated as absent rather than fatal: a machine that cannot read its own
    lock file should still be able to start after an unclean shutdown.
    """
    try:
        raw = claim_path(home).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def acquire_claim(home: Path, *, pid: int, host: str, port: int) -> Tuple[bool, str]:
    """Claim the data directory, or refuse and name whoever holds it.

    The device row cannot duplicate -- ``device_id`` is a primary key -- which is exactly why this
    is needed: two servers on one directory do not fail loudly, they run two schedulers over one
    SQLite file and both look healthy.
    """
    home = Path(home)
    held = read_claim(home)
    holder = int(held.get("pid") or 0) if held else 0
    if holder and holder != int(pid) and _alive(holder):
        return False, (
            f"another Vantage server (pid {holder}) is already running against {home} "
            f"on port {held.get('port')}. Stop it before starting a second one."
        )
    home.mkdir(parents=True, exist_ok=True)
    body = {
        "pid": int(pid),
        "host": str(host),
        "port": int(port),
        "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    tmp = claim_path(home).with_suffix(".tmp")
    tmp.write_text(json.dumps(body), encoding="utf-8")
    tmp.replace(claim_path(home))
    return True, ""


def release_claim(home: Path, *, pid: int) -> bool:
    """Drop the claim, but only our own -- a restart must not erase the new holder's lock."""
    held = read_claim(home)
    if not held:
        return False
    if int(held.get("pid") or 0) != int(pid):
        return False
    try:
        claim_path(home).unlink()
    except OSError:
        return False
    return True


def port_is_free(host: str, port: int, timeout: float = 0.35) -> bool:
    """Can this machine answer on ``host:port``? Unknown when the question cannot be asked.

    Measured, not assumed: uvicorn does not raise on a busy port. It runs the whole application
    -- scheduler included, which is the part that must not run twice -- prints "Application startup
    complete", and only then fails to bind and exits 3. So the port has to be asked first.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(timeout)
    try:
        probe.connect((host, int(port)))
    except OSError:
        probe.close()
        # Nothing listening. On 127.0.0.1 that is the whole question; on a wifi address a
        # refused connection still means no server answered on this machine.
        return True
    probe.close()
    return False


def serve_with_claim(home: Path, *, pid: int, host: str, port: int,
                     serve: Callable[..., Any]) -> Tuple[bool, int]:
    """Run the server under a single-instance claim, and translate the exit for launchd.

    ``run.py`` keeps no lifecycle logic of its own, so the guard cannot be bypassed by the one
    caller that matters. The two exit codes are the interesting part, because
    ``KeepAlive = {SuccessfulExit = false}`` keys on them:

    * **another instance holds the directory, or the port is already answered** -> ``(False, 0)``:
      nothing to do, and a non-zero exit would have launchd retry forever against a lock it
      cannot take.
    * **the bind fails anyway** -> ``(True, 0)``: same reasoning, one layer down.
    * **any other exception** propagates, so a real fault still shows up as a real fault -- and
      launchd is right to restart that one.

    In every case the claim is released before this returns, including on the way out of a
    crash: a lock left behind by a dead process is reclaimed by the next start, but a lock left
    behind by a *live* one would stop the laptop answering at all.
    """
    held, why = acquire_claim(home, pid=pid, host=host, port=port)
    if not held:
        log.info("second instance refused", extra_fields={"reason": why})
        print(f"Vantage: {why}", flush=True)
        return False, 0
    if not port_is_free(host, port):
        message = (f"something is already answering on {host}:{port}, and it is not this "
                   "process. Not starting a second scheduler.")
        log.info("port already answered", extra_fields={"host": host, "port": port})
        print(f"Vantage: {message}", flush=True)
        release_claim(home, pid=pid)
        return False, 0
    try:
        serve(host=host, port=port)
    except OSError as exc:
        log.warning("bind failed", extra_fields={"error": str(exc)})
        print(f"Vantage: nothing is listening on {host}:{port} -- {exc}", flush=True)
        return True, 0
    finally:
        release_claim(home, pid=pid)
    return True, 0


# ---------------------------------------------------------------------------
# the service
# ---------------------------------------------------------------------------


class HostPersistenceService:
    """Install, remove and report the login item -- answering from launchd, never from the request.

    Every collaborator is injectable because the truth this class reports is about the machine:
    ``runner`` is launchd, ``probe`` is whatever else may already be serving this directory, and
    ``agents_dir`` keeps a test out of the real ~/Library.
    """

    def __init__(self,
                 settings: Optional[Settings] = None,
                 *,
                 agents_dir: Optional[Path] = None,
                 runner: Optional[Callable[[list[str]], Tuple[int, str, str]]] = None,
                 probe: Optional[Callable[[], Optional[Dict[str, Any]]]] = None,
                 project_dir: Optional[str] = None,
                 python: Optional[str] = None) -> None:
        self.settings = settings or get_settings()
        self.agents_dir = Path(agents_dir) if agents_dir else None
        self.runner = runner or _default_runner
        self.probe = probe if probe is not None else self._default_probe
        self.project_dir = Path(project_dir) if project_dir else _source_root()
        self.python = str(python) if python else sys.executable

    # -- where things live ---------------------------------------------------

    def path(self) -> Path:
        return agent_path(self.agents_dir)

    @staticmethod
    def supported() -> bool:
        return PLATFORM == "darwin"

    def _default_probe(self) -> Optional[Dict[str, Any]]:
        """A live server for a *different* program, if one is serving our data directory.

        Our own server (the panel's child) is not a conflict: the agent takes over at the next
        login, and the message says so. A foreign one is, because then two schedulers would share
        this directory.
        """
        held = read_claim(self.settings.home)
        if not held:
            return None
        pid = int(held.get("pid") or 0)
        if not pid or not _alive(pid):
            return None
        if int(held.get("port") or 0) != int(self.settings.port):
            return None
        return {"pid": pid, "source": f"pid {pid} already serving port {held.get('port')}"}

    # -- what launchd says ---------------------------------------------------

    def _launchd_state(self) -> Tuple[bool, int]:
        """Ask launchd about *this job*, and believe only what it says about this job.

        Measured on the machine: ``launchctl print gui/<uid> <label>`` does not fail -- it prints
        the whole domain ("service count = 444") and exits 0, which read as ``loaded`` and could
        lift an unrelated service's pid into this one's report. And ``print`` for an absent job
        answers "Bad request. Could not find service" with exit code 0 as well. So the target is
        one argument, and the answer has to name this label.
        """
        code, out, err = self.runner([LAUNCHCTL, "print", f"{_domain()}/{AGENT_LABEL}"])
        text = f"{out}\n{err}"
        if code != 0 or AGENT_LABEL not in text:
            return False, 0
        if "could not find" in text.lower() or "bad request" in text.lower():
            return False, 0
        found = _PID_RE.search(out or "")
        return True, int(found.group(1)) if found else 0

    def status(self) -> Dict[str, Any]:
        """Read the machine. Nothing here is remembered between calls, because the user can
        change any of it from System Settings or by deleting the file."""
        path = self.path()
        installed = path.exists()
        loaded, pid = self._launchd_state()
        if not self.supported():
            state = "unsupported"
            detail = f"launchd is a macOS facility; this is {PLATFORM}"
        elif not installed and not loaded:
            state = "not_installed"
            detail = "Vantage runs only while its window is open."
        elif installed and not loaded:
            state = "installed_not_loaded"
            detail = ("the file is in place but launchd has no job for it, so nothing starts at "
                      "login -- it was written, not accepted")
        elif loaded and not installed:
            state = "loaded_without_file"
            detail = ("launchd is running a job whose definition file is gone; it stays up until "
                      "the next login and then does not come back")
        else:
            state = "loaded"
            detail = "Vantage starts at login and is relaunched if it crashes."
        return {
            "supported": self.supported(),
            "label": AGENT_LABEL,
            "path": str(path),
            "installed": installed,
            "loaded": loaded,
            "pid": pid,
            "state": state,
            "detail": detail,
            "answered_by": f"{LAUNCHCTL} print {_domain()} {AGENT_LABEL}",
        }

    # -- the writes ----------------------------------------------------------

    def _preflight(self) -> str:
        """Return a refusal reason, or "" when an install would have a chance of working."""
        if not self.supported():
            return (f"macOS is the only platform with a login item here, and this is {PLATFORM}. "
                    "Nothing was installed.")
        if not Path(self.python).exists():
            return (f"the Python interpreter Vantage would start is not there ({self.python}), "
                    "so the service could never launch. Nothing was installed.")
        if not (self.project_dir / "run.py").exists():
            return (f"the Vantage program is not at {self.project_dir}, so the service would "
                    "start nothing. Nothing was installed.")
        return ""

    def install_file_only(self) -> Path:
        """Write the definition without asking launchd to load it.

        Not a product action -- it exists so the difference between "a file is on disk" and
        "launchd accepted a job" can be observed, which is the difference the status reporting
        depends on.
        """
        path = self.path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self._render(), encoding="utf-8")
        return path

    def _render(self) -> str:
        return render_plist(python=str(self.python), project_dir=str(self.project_dir),
                            home=str(self.settings.home), host=str(self.settings.host),
                            port=int(self.settings.port))

    def enable(self) -> Dict[str, Any]:
        problem = self._preflight()
        if problem:
            return {"ok": False, "error": problem, "state": "unsupported"
                    if not self.supported() else "not_installed"}

        foreign = self.probe()
        if foreign:
            return {"ok": False, "error": (
                f"something else is already answering on this machine's port -- {foreign['source']}"
                ". Installing a login item on top of it would start a second scheduler over the "
                "same data. Stop that one first."), "state": "not_installed"}

        path = self.install_file_only()
        code, _out, err = self.runner([LAUNCHCTL, "bootstrap", _domain(), str(path)])
        after = self.status()
        if after["state"] in ("loaded", "running"):
            return {"ok": True, "state": after["state"], "pid": after["pid"],
                    "detail": after["detail"], "path": str(path)}

        # launchd refused it: leaving the file would be a claim about the next login that is
        # false, and the machine would only find out at the worst possible moment.
        try:
            path.unlink()
        except OSError:
            pass
        reason = (err or "").strip() or f"exit code {code}"
        return {"ok": False, "error": f"launchd refused the login item: {reason}",
                "state": self.status()["state"]}

    def _wait_for_teardown(self, settle: float) -> Tuple[bool, int]:
        """Give launchd a bounded window to finish, then read it again.

        Measured live: ``bootout`` returned 0 while the job was still in the table, and the job
        was gone a moment later. One immediate re-read therefore reported a refusal about a job
        that had in fact stopped, and left the plist behind. The window bounds the *wait*; it does
        not decide the *answer* -- a job that outlasts it is still reported as loaded, with the
        file left exactly where it is.
        """
        deadline = time.monotonic() + max(0.0, settle)
        loaded, pid = self._launchd_state()
        while loaded and time.monotonic() < deadline:
            time.sleep(0.15)
            loaded, pid = self._launchd_state()
        return loaded, pid

    def disable(self, *, settle: float = DEFAULT_SETTLE_S) -> Dict[str, Any]:
        if not self.supported():
            return {"ok": False, "error": (
                f"macOS is the only platform with a login item here, and this is {PLATFORM}.")}

        path = self.path()
        if not path.exists():
            loaded, pid = self._launchd_state()
            if not loaded:
                return {"ok": True, "state": "not_installed",
                        "detail": "No login item was installed, so there was nothing to remove.",
                        "pid": 0}
            return {"ok": False, "error": (
                f"launchd is still running the job (pid {pid}) with no definition file to remove; "
                "it ends at the next login."), "state": "loaded_without_file", "pid": pid}

        code, _out, err = self.runner([LAUNCHCTL, "bootout", f"{_domain()}/{AGENT_LABEL}"])
        loaded, pid = self._wait_for_teardown(settle)
        if loaded:
            # Only a job launchd still recognises counts as a refusal. "Could not find service"
            # is the expected answer when it was never loaded, and treating that as failure would
            # make the file impossible to clean up.
            return {"ok": False, "error": (
                f"launchd still reports the job loaded (pid {pid})"
                + (f": {(err or '').strip()}" if (err or '').strip() else f" (exit {code})")
                + ". Nothing was removed."), "state": "loaded", "pid": pid}

        try:
            path.unlink()
        except OSError as exc:
            return {"ok": False, "error": f"the job is stopped but the file could not be removed: {exc}",
                    "state": self.status()["state"]}

        after = self.status()
        return {"ok": True, "state": after["state"], "detail": after["detail"], "pid": 0}


def _source_root() -> Path:
    """The directory holding ``run.py``: the checkout in development, the staged copy in a bundle."""
    return Path(__file__).resolve().parents[1]


def service_from_settings(settings: Optional[Settings] = None) -> HostPersistenceService:
    return HostPersistenceService(settings=settings)
