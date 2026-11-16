"""Outcome Assurance: observe what actually happened, past the filesystem.

The mission runner already verifies a job by re-reading the world after the
steps ran -- but every observer it had was a *file* observer (a file contains a
needle, hashes to a value, is absent, matches another file). An effect anywhere
else -- a page answered, a command exited cleanly, an application is in front,
a port is listening -- could only be recorded UNKNOWN. UNKNOWN is honest, but it
is also the whole answer whenever the thing being verified is not a file.

This module adds observers for those. The rule every one keeps is the same rule
the file observers keep, and it is why these are trustworthy at all:

* **An observer reads the world, never the executor's own message.** It does not
  accept *which tool ran* or *what the tool said*; it is handed the thing to
  look at (a URL, an argv, a process name) and reports only what it now sees.
* **It never guesses.** A timeout, a permissions error, a missing binary is
  UNKNOWN with the reason, not a failure dressed as a success -- and never
  VERIFIED from a non-answer. "The check could not run" and "the check ran and
  the world is wrong" are different sentences and stay different.
* **It is declarative.** A mission row names a kind and its fields as JSON. The
  runner resolves that to one of these observers by name; a condition cannot
  smuggle in a callable. This is the same surface ``_verifier_from_spec``
  already reads, extended -- not a second verification path.

What this is not: a general "run any command and trust it" verifier. The command
observer runs an **allowlisted, argv-only** command with a timeout and matches
its stdout. Anything else is refused with a reason, because an observer that
will run whatever a condition asks is an execution path wearing a check's name.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from ..core.missions import ConditionStatus
from ..logging_setup import get_logger

log = get_logger("core.outcome_assurance")

#: The kinds this module can observe. ``_verifier_from_spec`` in the runner
#: resolves a condition's ``kind`` against this set; a kind not here yields the
#: existing UNKNOWN rather than a silent pass.
KINDS: Tuple[str, ...] = (
    "url_body",          # http(s): the body contains (or does not contain) text
    "url_status",        # http(s): the status code is one of a set
    "command_output",    # an allowlisted argv exits 0 and its stdout contains text
    "process_running",   # a process name is present in the process table
    "port_listening",    # a local TCP port accepts a connection
    "app_frontmost",     # macOS: the frontmost application is (or is not) a name
)

#: The only argv a ``command_output`` observer will run. An observer that runs
#: whatever a condition names would be an execution path, not an observation, so
#: this is an allowlist rather than a denylist. Each entry is a whole argv
#: prefix; extra arguments are allowed only after it.
ALLOWED_COMMANDS: Dict[str, Tuple[str, ...]] = {
    "echo": ("/bin/echo",),
    "test_file": ("/bin/test", "-e"),
    "uname": ("/usr/bin/uname",),
    "git_status": ("/usr/bin/git", "status", "--porcelain"),
    "ls": ("/bin/ls",),
}

#: A single observation is a look, not a job. Two seconds is long enough for a
#: local page or command and short enough that a mission's final verification
#: cannot hang on a wedged one.
DEFAULT_TIMEOUT_S = 2.0

#: What a verification observation reports back, independent of the enum name.
@dataclass
class Observation:
    status: str        # verified | failed | unknown
    detail: str        # what the observer actually saw, in a sentence

    def to_verification(self, condition_id: str):
        # `Verification` and `ConditionStatus` live in the runner/missions
        # modules, which import this one inside `_verifier_from_spec`; importing
        # them at module load would be a cycle. Resolved at call time instead.
        from .mission_runner import Verification as MissionVerification

        status = {
            "verified": ConditionStatus.VERIFIED,
            "failed": ConditionStatus.FAILED,
        }.get(self.status, ConditionStatus.UNKNOWN)
        return MissionVerification(condition_id, status, self.detail)


_VERIFIED, _FAILED, _UNKNOWN = "verified", "failed", "unknown"


def _blocked_by_egress(url: str) -> Optional[Observation]:
    """Refuse a URL the egress firewall does not admit, as UNKNOWN.

    An observation of an address this Mac may not contact is not a failed check;
    it is a check that could not be made. Reporting FAILED would blame the
    mission for the firewall's decision, and reporting VERIFIED would invent a
    reading. UNKNOWN with the reason is the only honest state.
    """
    try:
        from ..security.egress import EgressRefused, require

        require(url)
        return None
    except EgressRefused as exc:
        return Observation(_UNKNOWN, f"the egress firewall refused the check: {exc}")
    except Exception as exc:  # pragma: no cover - defensive
        return Observation(_UNKNOWN, f"the egress firewall could not decide: {exc}")


# -- the observers -----------------------------------------------------------

def observe_url(url: str, *, needle: str = "", absent: bool = False,
                timeout: float = DEFAULT_TIMEOUT_S) -> Observation:
    """Read a URL's body and report whether it contains ``needle``.

    ``absent=True`` asks the opposite question (the page no longer contains it),
    which is how "the old banner is gone" is verified without trusting the deploy
    tool that removed it.
    """
    import urllib.request

    from ..net import DeadlineExceeded, EgressRefused, http_read

    refusal = _blocked_by_egress(url)
    if refusal is not None:
        return refusal
    try:
        raw = http_read(urllib.request.Request(url, headers={"User-Agent": "Vantage/1.0"}),
                        total=timeout, what=url)
    except DeadlineExceeded as exc:
        return Observation(_UNKNOWN, f"the page did not answer in {exc.seconds:g}s")
    except EgressRefused as exc:  # raced a privacy-mode flip
        return Observation(_UNKNOWN, f"the egress firewall refused the check: {exc}")
    except Exception as exc:
        return Observation(_UNKNOWN, f"the page could not be read: {exc}")

    text = raw.decode("utf-8", "replace")
    present = needle in text
    if absent:
        if present:
            return Observation(_FAILED, f"{url} still contains {needle!r}")
        return Observation(_VERIFIED, f"{url} answers and no longer contains {needle!r}")
    if present:
        return Observation(_VERIFIED, f"{url} contains {needle!r} ({len(raw)} bytes)")
    return Observation(_FAILED, f"{url} answered but does not contain {needle!r}")


def observe_url_status(url: str, *, codes: Sequence[int] = (), timeout: float = DEFAULT_TIMEOUT_S) -> Observation:
    """Read a URL and report whether its HTTP status is one of ``codes``.

    A connection that is refused, or an address the firewall blocks, is UNKNOWN
    rather than a failed status -- there was no status to compare.
    """
    import urllib.error
    import urllib.request

    from ..net import DeadlineExceeded, EgressRefused, http_read

    refusal = _blocked_by_egress(url)
    if refusal is not None:
        return refusal
    wanted = [int(c) for c in (codes or ())]
    try:
        http_read(urllib.request.Request(url, headers={"User-Agent": "Vantage/1.0"}),
                  total=timeout, what=url)
        status = 200
    except urllib.error.HTTPError as exc:
        status = int(getattr(exc, "code", 0) or 0)
    except DeadlineExceeded as exc:
        return Observation(_UNKNOWN, f"the page did not answer in {exc.seconds:g}s")
    except EgressRefused as exc:
        return Observation(_UNKNOWN, f"the egress firewall refused the check: {exc}")
    except Exception as exc:
        return Observation(_UNKNOWN, f"the page could not be reached: {exc}")

    if not wanted:
        return Observation(_UNKNOWN, "the condition names no status codes to match")
    if status in wanted:
        return Observation(_VERIFIED, f"{url} answered {status}")
    return Observation(_FAILED, f"{url} answered {status}, expected one of {wanted}")


def observe_command(key: str, *, needle: str = "", args: Sequence[str] = (),
                    timeout: float = DEFAULT_TIMEOUT_S) -> Observation:
    """Run one allowlisted argv and report whether its stdout contains ``needle``.

    A non-zero exit is FAILED with the stderr text; a binary that is missing or a
    command that times out is UNKNOWN. The command must be a named entry in
    :data:`ALLOWED_COMMANDS` -- an unknown name is refused, not guessed at.
    """
    def _run() -> Observation:
        import subprocess

        prefix = ALLOWED_COMMANDS.get(str(key or "").strip())
        if prefix is None:
            return Observation(
                _UNKNOWN,
                f"{key!r} is not an allowlisted command; an observer will not run "
                f"arbitrary programs (known: {sorted(ALLOWED_COMMANDS)})")
        argv = list(prefix) + [str(a) for a in (args or ())]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            return Observation(_UNKNOWN, f"{argv[0]} is not installed")
        except subprocess.TimeoutExpired:
            return Observation(_UNKNOWN, f"{' '.join(argv)} did not finish in {timeout:g}s")
        except OSError as exc:
            return Observation(_UNKNOWN, f"{' '.join(argv)} could not be started: {exc}")

        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()[:200]
            return Observation(_FAILED, f"{' '.join(argv)} exited {proc.returncode}"
                                        + (f": {detail}" if detail else ""))
        out = proc.stdout or ""
        if needle and needle not in out:
            return Observation(_FAILED, f"{' '.join(argv)} ran but its output does not "
                                        f"contain {needle!r}")
        return Observation(_VERIFIED, f"{' '.join(argv)} exited 0"
                                      + (f" and printed {needle!r}" if needle else ""))

    return _run()


def observe_process(name: str, *, absent: bool = False, timeout: float = DEFAULT_TIMEOUT_S) -> Observation:
    """Report whether a process named ``name`` is in the process table.

    ``ps`` is used rather than a library so this reads the same table a person
    would. A read that fails is UNKNOWN, never "not running" -- an unread table
    is not evidence of absence.
    """
    def _run() -> Observation:
        import subprocess

        try:
            proc = subprocess.run(["/bin/ps", "-axo", "comm="], capture_output=True,
                                  text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return Observation(_UNKNOWN, f"the process table could not be read: {exc}")
        if proc.returncode != 0:
            return Observation(_UNKNOWN, "the process table could not be read")
        table = proc.stdout or ""
        needle = (name or "").strip()
        if not needle:
            return Observation(_UNKNOWN, "the condition names no process")
        present = needle.lower() in table.lower()
        if absent:
            if present:
                return Observation(_FAILED, f"{needle} is still running")
            return Observation(_VERIFIED, f"{needle} is no longer running")
        if present:
            return Observation(_VERIFIED, f"{needle} is running")
        return Observation(_FAILED, f"{needle} is not in the process table")

    return _run()


def observe_port(port: int, *, host: str = "127.0.0.1",
                 timeout: float = DEFAULT_TIMEOUT_S) -> Observation:
    """Report whether a local TCP port accepts a connection right now."""
    import socket

    try:
        port_int = int(port)
    except (TypeError, ValueError):
        return Observation(_UNKNOWN, "the condition names no port")
    if not (0 < port_int < 65536):
        return Observation(_UNKNOWN, f"{port_int} is not a usable port")
    try:
        with socket.create_connection((host, port_int), timeout=timeout):
            return Observation(_VERIFIED, f"{host}:{port_int} accepts a connection")
    except OSError as exc:
        return Observation(_FAILED, f"{host}:{port_int} refused a connection: {exc}")


def observe_app_frontmost(name: str, *, absent: bool = False,
                          timeout: float = DEFAULT_TIMEOUT_S) -> Observation:
    """macOS: report whether ``name`` is the application in front.

    This is an *observation*, not a control: it asks System Events and refuses to
    guess when Accessibility permission is missing. Missing permission is
    UNKNOWN, never a failed match.
    """
    if not (name or "").strip():
        return Observation(_UNKNOWN, "the condition names no application")
    script = 'tell application "System Events" to name of first application process whose frontmost is true'

    def _run() -> Observation:
        import subprocess

        try:
            proc = subprocess.run(["/usr/bin/osascript", "-e", script],
                                  capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return Observation(_UNKNOWN, f"the frontmost application could not be read: {exc}")
        detail = (proc.stderr or proc.stdout or "").strip()
        if proc.returncode != 0:
            if "-1712" in detail:
                return Observation(_UNKNOWN, "System Events did not answer in time "
                                             "(an Accessibility prompt may be waiting)")
            if "-1743" in detail or "not authorized" in detail.lower():
                return Observation(_UNKNOWN, "reading the frontmost application needs "
                                             "Accessibility permission")
            return Observation(_UNKNOWN, f"the frontmost application could not be read: {detail[:160]}")
        front = (proc.stdout or "").strip().splitlines()[0] if proc.stdout.strip() else ""
        if not front:
            return Observation(_UNKNOWN, "System Events named no frontmost application")
        match = front.lower() == name.strip().lower()
        if absent:
            if match:
                return Observation(_FAILED, f"{name} is still in front")
            return Observation(_VERIFIED, f"{front} is in front, not {name}")
        if match:
            return Observation(_VERIFIED, f"{name} is the application in front")
        return Observation(_FAILED, f"{front} is in front, not {name}")

    return _run()


# -- resolving a declarative condition ---------------------------------------

def observer_for_spec(spec: str) -> Optional[Callable[[], Observation]]:
    """Turn a condition's JSON spec into a zero-argument observation, or None.

    None means "this build cannot observe that", which the runner records as
    UNKNOWN -- the same contract ``_verifier_from_spec`` already has for the file
    observers. A spec naming an unknown ``kind`` returns None rather than a
    verifier that would pass everything.
    """
    if not spec:
        return None
    try:
        parsed = json.loads(spec)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    kind = str(parsed.get("kind") or "")

    if kind == "url_body":
        url = str(parsed.get("url") or "")
        if not url:
            return None
        return lambda: observe_url(url, needle=str(parsed.get("needle") or ""),
                                   absent=bool(parsed.get("absent")),
                                   timeout=float(parsed.get("timeout") or DEFAULT_TIMEOUT_S))
    if kind == "url_status":
        url = str(parsed.get("url") or "")
        if not url:
            return None
        return lambda: observe_url_status(
            url, codes=[int(c) for c in (parsed.get("codes") or ())],
            timeout=float(parsed.get("timeout") or DEFAULT_TIMEOUT_S))
    if kind == "command_output":
        key = str(parsed.get("command") or "")
        if not key:
            return None
        return lambda: observe_command(
            key, needle=str(parsed.get("needle") or ""),
            args=[str(a) for a in (parsed.get("args") or ())],
            timeout=float(parsed.get("timeout") or DEFAULT_TIMEOUT_S))
    if kind == "process_running":
        name = str(parsed.get("process") or "")
        if not name:
            return None
        return lambda: observe_process(
            name, absent=bool(parsed.get("absent")),
            timeout=float(parsed.get("timeout") or DEFAULT_TIMEOUT_S))
    if kind == "port_listening":
        try:
            port = int(parsed.get("port"))
        except (TypeError, ValueError):
            return None
        return lambda: observe_port(
            port, host=str(parsed.get("host") or "127.0.0.1"),
            timeout=float(parsed.get("timeout") or DEFAULT_TIMEOUT_S))
    if kind == "app_frontmost":
        name = str(parsed.get("app") or "")
        if not name:
            return None
        return lambda: observe_app_frontmost(
            name, absent=bool(parsed.get("absent")),
            timeout=float(parsed.get("timeout") or DEFAULT_TIMEOUT_S))
    return None


def verifier_from_spec(spec: str, condition_id: str):
    """A ``Verifier`` for the mission runner, backed by an assurance observer.

    The result is UNKNOWN without an observation when this build cannot read the
    world the spec names -- never a pass.
    """
    observation = observer_for_spec(spec)

    def verify(mission) -> Any:  # noqa: ANN001 - the runner's Mission type
        if observation is None:
            return Observation(
                _UNKNOWN,
                "this build has no observer for that condition",
            ).to_verification(condition_id)
        try:
            return observation().to_verification(condition_id)
        except Exception as exc:  # an observer must never crash a verification pass
            return Observation(_UNKNOWN, f"the observer raised: {exc}").to_verification(
                condition_id)

    return verify


def supported_kinds() -> List[Dict[str, Any]]:
    """What this build can observe, for a screen that lists it."""
    return [
        {"kind": "url_body", "question": "does this page's body contain (or lack) this text?",
         "carries": "a GET to the URL"},
        {"kind": "url_status", "question": "is the HTTP status one of these codes?",
         "carries": "a GET to the URL"},
        {"kind": "command_output", "question": "does this allowlisted command exit 0 and say this?",
         "carries": f"one of {sorted(ALLOWED_COMMANDS)}"},
        {"kind": "process_running", "question": "is this process in the table (or gone)?",
         "carries": "a `ps` read"},
        {"kind": "port_listening", "question": "does this local port accept a connection?",
         "carries": "a loopback connect"},
        {"kind": "app_frontmost", "question": "is this application in front (or not)?",
         "carries": "one System Events query, permission-gated"},
    ]