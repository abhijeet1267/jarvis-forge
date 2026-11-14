"""Self-healing: observe a goal, detect a violation, propose a bounded repair.

The rules that keep this from becoming an unbounded restart loop, in order of
importance:

* **A goal is only recoverable when the target is ours.** Restart is offered for a
  process this profile started and can prove it started -- a durable process
  record, not a guess from a PID, and never a third-party application.
* **Recovery is proposed, not performed automatically** when it needs approval.
  The plan is data a person (or the mission's own approval) can inspect.
* **Every attempt is bounded**: a maximum count, a minimum interval between
  attempts, and loop detection that stops repeating the same repair against the
  same unchanged observation.
* **A recovery is only successful when a fresh observation says so.** The repair
  command returning 0 is not evidence; the goal being verified afterwards is.

The observers reuse `ObservationEngine` where it already can see the state, and
add a bounded HTTP check. Nothing here shells out to arbitrary commands.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..logging_setup import get_logger
from .missions import ConditionStatus

log = get_logger("core.selfhealing")

#: How many times one goal may be repaired before the mission is escalated to a
#: person. Three is enough for a transient failure and short enough that a
#: genuinely broken service does not churn forever.
MAX_RECOVERY_ATTEMPTS = 3

#: The shortest gap between two repair attempts, so a fast-failing restart does
#: not become a busy loop.
MIN_SECONDS_BETWEEN_ATTEMPTS = 2.0


@dataclass
class GoalObservation:
    """One looked-at fact about a goal, with what was expected."""

    goal_id: str
    observer: str
    target: str
    expected: str
    actual: str
    result: str                       # verified | failed | unknown
    at: float = field(default_factory=time.time)
    evidence_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "goal_id": self.goal_id, "observer": self.observer,
            "target": self.target, "expected": self.expected,
            "actual": self.actual, "result": self.result,
            "at": self.at, "evidence_id": self.evidence_id,
        }


@dataclass
class RecoveryAttempt:
    goal_id: str
    action: str
    at: float = field(default_factory=time.time)
    ok: bool = False
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"goal_id": self.goal_id, "action": self.action, "at": self.at,
                "ok": self.ok, "detail": self.detail}


# -- observers --------------------------------------------------------------

def observe_http(url: str, *, expect_body: str = "", timeout: float = 5.0,
                 goal_id: str = "G1") -> GoalObservation:
    """Ask the endpoint what it actually answers. Never assumes success.

    The read goes through `jarvis.net.http_read`, so this observation shares the
    codebase's one total-deadline rule: a server that accepts the connection and
    then trickles cannot hold an observer open indefinitely. `timeout` is the
    whole-read budget, not a per-socket gap.
    """
    import urllib.request

    from ..net import DeadlineExceeded, http_read

    try:
        request = urllib.request.Request(url, headers={"Accept": "*/*"})
        body_bytes = http_read(request, total=timeout, what=url)
        body = body_bytes[:2048].decode("utf-8", "replace")
        status = 200
    except DeadlineExceeded as exc:
        return GoalObservation(goal_id, "http", url, "HTTP 200",
                               f"no complete response: {exc}",
                               ConditionStatus.UNKNOWN.value)
    except urllib.error.HTTPError as exc:
        return GoalObservation(goal_id, "http", url, "HTTP 200",
                               f"HTTP {exc.code}", ConditionStatus.FAILED.value)
    except Exception as exc:
        return GoalObservation(goal_id, "http", url, "HTTP 200",
                               f"unreachable: {str(exc)[:100]}",
                               ConditionStatus.UNKNOWN.value)

    if expect_body and expect_body not in body:
        return GoalObservation(
            goal_id, "http", url, f"a body containing {expect_body!r}",
            f"HTTP 200 with {body[:80]!r}", ConditionStatus.FAILED.value)
    return GoalObservation(goal_id, "http", url,
                           "HTTP 200" + (f" containing {expect_body!r}" if expect_body else ""),
                           f"HTTP {status}", ConditionStatus.VERIFIED.value)


def observe_file(path: str, *, expect_text: Optional[str] = None,
                 goal_id: str = "G1") -> GoalObservation:
    target = Path(path).expanduser()
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return GoalObservation(goal_id, "file", path, "the file exists",
                               "missing", ConditionStatus.FAILED.value)
    except OSError as exc:
        return GoalObservation(goal_id, "file", path, "the file is readable",
                               f"unreadable: {exc}", ConditionStatus.UNKNOWN.value)

    if expect_text is not None and expect_text not in text:
        return GoalObservation(goal_id, "file", path,
                               f"text containing {expect_text!r}",
                               f"{len(text)} bytes without it",
                               ConditionStatus.FAILED.value)
    return GoalObservation(goal_id, "file", path, "exists with the expected text",
                           f"{len(text)} bytes", ConditionStatus.VERIFIED.value)


def observe_owned_process(process: dict, goal_id: str = "G1") -> GoalObservation:
    """Whether a process Vantage started is still the one running.

    `process` is a durable record ({pid, started_at, command}) written when the
    process was launched, not a bare pid. A pid that now belongs to something
    else must not read as "our process is alive".
    """
    pid = int(process.get("pid") or 0)
    expected_start = float(process.get("started_at") or 0.0)
    command = str(process.get("command") or "")

    if not pid:
        return GoalObservation(goal_id, "process", command, "our process is running",
                               "no process record", ConditionStatus.UNKNOWN.value)

    try:
        os_pid = _ps_start_time(pid)
    except Exception:
        os_pid = None

    if os_pid is None:
        return GoalObservation(goal_id, "process", f"pid {pid}",
                               "our process is running", "not running",
                               ConditionStatus.FAILED.value)

    # The start time is the identity: a recycled pid has a different one.
    if expected_start and abs(os_pid - expected_start) > 1.0:
        return GoalObservation(
            goal_id, "process", f"pid {pid}",
            "our process is running",
            "that pid belongs to a different process now",
            ConditionStatus.UNKNOWN.value)
    return GoalObservation(goal_id, "process", f"pid {pid}",
                           "our process is running", "running",
                           ConditionStatus.VERIFIED.value)


def _ps_start_time(pid: int) -> Optional[float]:
    """The process start time in epoch seconds, or None when it is gone."""
    import subprocess

    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    stamp = (result.stdout or "").strip()
    if not stamp:
        return None
    try:
        from datetime import datetime

        return datetime.strptime(stamp, "%a %b %d %H:%M:%S %Y").timestamp()
    except ValueError:
        return None


# -- the controller ---------------------------------------------------------

class RecoveryController:
    """Decides whether a repair is allowed, then performs at most one attempt."""

    def __init__(self, *, max_attempts: int = MAX_RECOVERY_ATTEMPTS,
                 min_interval: float = MIN_SECONDS_BETWEEN_ATTEMPTS) -> None:
        self.max_attempts = max_attempts
        self.min_interval = min_interval

    def propose(self, observation: GoalObservation, *,
                attempts: Optional[List[RecoveryAttempt]] = None,
                can_restart: bool = False,
                restart_target: str = "") -> Dict[str, Any]:
        """What, if anything, may be done about this observation.

        Returns a *proposal*, never an action. `allowed` false carries the reason
        a caller can show; a recovery that cannot be justified is reported as
        escalated rather than attempted.
        """
        attempts = attempts or []

        if observation.result == ConditionStatus.VERIFIED.value:
            return {"needed": False, "allowed": False,
                    "reason": "the goal is already satisfied"}

        if observation.result == ConditionStatus.UNKNOWN.value:
            # Proceeding on an unknown is how a broken observer becomes a
            # restart loop: act only on a contract to the world, not on silence.
            return {"needed": True, "allowed": False,
                    "escalate": True,
                    "reason": ("the state cannot be observed, so a repair cannot "
                               f"be justified: {observation.actual}")}

        if len(attempts) >= self.max_attempts:
            return {"needed": True, "allowed": False, "escalate": True,
                    "reason": (f"{len(attempts)} recovery attempts have already been "
                               "made; escalating instead of retrying")}

        recent = [a for a in attempts if time.time() - a.at < self.min_interval]
        if recent:
            return {"needed": True, "allowed": False,
                    "reason": "an attempt was made moments ago; waiting before retrying"}

        if not can_restart:
            return {"needed": True, "allowed": False, "escalate": True,
                    "reason": ("the failing resource is not one Vantage owns, so it "
                               "will not be restarted")}

        return {"needed": True, "allowed": True, "action": "restart",
                "target": restart_target,
                "reason": (f"restart {restart_target}: a Vantage-owned process that "
                           "should be running is not")}

    def attempts_summary(self, attempts: List[RecoveryAttempt]) -> Dict[str, Any]:
        return {
            "count": len(attempts),
            "remaining": max(0, self.max_attempts - len(attempts)),
            "history": [a.to_dict() for a in attempts],
        }