"""Agent Control Tower: what the agents can do, what they are doing, and a stop
that actually stops.

The Control Tower is not a dashboard drawn over nothing. Three claims it makes,
and what makes each one true:

* **"This worker is busy"** comes from a registry the tool invocation path
  updates, not from a simulated heartbeat. A worker's state is
  ``running`` while a call is inside it and ``idle`` after; the timestamps are
  real clock readings of the call, not a timer that ticks whether or not
  anything happened.
* **"Paused" stops the next call.** Pausing a worker sets a gate that the
  invocation path consults *before* dispatch. A paused worker refuses new work
  with a reason; it does not merely draw a badge.
* **"Budget exhausted" stops the next call too.** A worker has a call budget.
  Each dispatch decrements it; at zero the worker refuses. This bounds an
  agent that would otherwise loop, which is the failure a pause button cannot
  catch because a person is not watching at 3am.

What this deliberately does not claim: it cannot stop a call already inside a
tool handler. Python threads cannot be pre-empted, and killing the process to
interrupt one tool would take the rest of the app with it. So a pause or a
cancel takes effect **at the next dispatch boundary**, and the screen says so
rather than implying instantaneous pre-emption.

State lives in memory because a worker is a live object in this process: its
"running" state describes a call happening now, which a database row cannot.
The *counters* a person cares about across restarts -- calls made, budget spent
-- are likewise per-process, because they count calls this process made.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..logging_setup import get_logger

log = get_logger("core.control_tower")

#: Worker states. There is no "paused-by-system" distinct from a person's pause:
#: a pause is a pause, and `paused_by` names who asked.
IDLE = "idle"
RUNNING = "running"
PAUSED = "paused"
BUDGET_EXHAUSTED = "budget_exhausted"
STOPPED = "stopped"


class WorkerRefusal(Exception):
    """A worker declined a call. Carries the state that caused it."""

    def __init__(self, worker: str, state: str, reason: str) -> None:
        super().__init__(reason)
        self.worker = worker
        self.state = state
        self.reason = reason


@dataclass
class Worker:
    """One agent, as the tower sees it."""

    name: str
    #: Tools this worker owns, from the registry. Static per process.
    tools: List[str] = field(default_factory=list)
    #: Calls allowed before the worker refuses more. ``None`` means unbounded.
    call_budget: Optional[int] = None
    #: Calls dispatched in this process. Not persisted: it counts this process.
    calls_made: int = 0
    calls_refused: int = 0
    failures: int = 0
    #: Wall-clock of the most recent call, and how long it took.
    last_call_at: Optional[float] = None
    last_duration_ms: Optional[int] = None
    last_tool: str = ""
    last_error: str = ""
    #: How many calls are inside the worker right now. A worker can be re-entered
    #: by independent schedulers, so "running" is a count, not a bool.
    in_flight: int = 0
    paused: bool = False
    paused_by: str = ""
    paused_reason: str = ""
    #: Set when a stop was requested. A stopped worker refuses like a paused one
    #: but is not resumable; the person must restart the process.
    stopped: bool = False

    # -- state -------------------------------------------------------------
    @property
    def state(self) -> str:
        if self.stopped:
            return STOPPED
        if self.paused:
            return PAUSED
        if self.call_budget is not None and self.calls_made >= self.call_budget:
            return BUDGET_EXHAUSTED
        if self.in_flight > 0:
            return RUNNING
        return IDLE

    @property
    def remaining_budget(self) -> Optional[int]:
        if self.call_budget is None:
            return None
        return max(0, self.call_budget - self.calls_made)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state,
            "tools": list(self.tools),
            "tool_count": len(self.tools),
            "call_budget": self.call_budget,
            "calls_made": self.calls_made,
            "calls_refused": self.calls_refused,
            "failures": self.failures,
            "remaining_budget": self.remaining_budget,
            "last_call_at": self.last_call_at,
            "last_duration_ms": self.last_duration_ms,
            "last_tool": self.last_tool,
            "last_error": self.last_error,
            "in_flight": self.in_flight,
            "paused_by": self.paused_by,
            "paused_reason": self.paused_reason,
        }


class ControlTower:
    """The registry of workers, and the gate that enforces pause and budget.

    A single instance per process, guarded by a lock, because it is read and
    written from the HTTP thread and from whatever scheduler runs a tool. A
    second instance would have its own pause flags, and pausing through the API
    would not stop the worker the runner actually calls.
    """

    _instance: Optional["ControlTower"] = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._workers: Dict[str, Worker] = {}
        #: Tool -> worker, so a dispatch by tool name finds the right worker.
        self._tool_owner: Dict[str, str] = {}

    # -- construction ------------------------------------------------------
    @classmethod
    def get(cls) -> "ControlTower":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = ControlTower()
            return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Drop the singleton. For tests; production has exactly one process."""
        with cls._instance_lock:
            cls._instance = None

    def register_worker(self, name: str, tools: List[str],
                        call_budget: Optional[int] = None) -> Worker:
        """Declare a worker and the tools it owns.

        Re-registering updates the tool list and leaves the counters and pause
        state alone: the orchestrator builds agents once per process, but a
        caller re-reading the registry should not lose the pause it set.
        """
        with self._lock:
            worker = self._workers.get(name)
            if worker is None:
                worker = Worker(name=name, call_budget=call_budget)
                self._workers[name] = worker
            worker.tools = list(tools)
            if call_budget is not None:
                worker.call_budget = call_budget
            for tool in worker.tools:
                self._tool_owner[tool] = name
            return worker

    def register_from_registry(self, registry, *, default_budget: Optional[int] = None
                               ) -> List[Worker]:
        """Build the worker list from the tool registry's own ``agent`` field.

        The registry is the one place that says which agent owns a tool, so the
        tower reads it rather than keeping a second mapping that could drift.
        """
        by_agent: Dict[str, List[str]] = {}
        for spec in registry.all():
            by_agent.setdefault(spec.agent, []).append(spec.name)
        return [self.register_worker(name, sorted(tools), call_budget=default_budget)
                for name, tools in sorted(by_agent.items())]

    # -- reading -----------------------------------------------------------
    def workers(self) -> List[Worker]:
        with self._lock:
            return list(self._workers.values())

    def worker(self, name: str) -> Optional[Worker]:
        with self._lock:
            return self._workers.get(name)

    def worker_for_tool(self, tool: str) -> Optional[Worker]:
        with self._lock:
            owner = self._tool_owner.get(tool)
            return self._workers.get(owner) if owner else None

    def snapshot(self) -> Dict[str, Any]:
        """Everything the Control Tower screen needs, in one read."""
        with self._lock:
            workers = list(self._workers.values())
            return {
                "workers": [w.to_dict() for w in workers],
                "summary": {
                    "total": len(workers),
                    "running": sum(1 for w in workers if w.state == RUNNING),
                    "paused": sum(1 for w in workers if w.state == PAUSED),
                    "budget_exhausted": sum(1 for w in workers
                                            if w.state == BUDGET_EXHAUSTED),
                    "stopped": sum(1 for w in workers if w.state == STOPPED),
                    "calls_made": sum(w.calls_made for w in workers),
                    "calls_refused": sum(w.calls_refused for w in workers),
                },
                "note": ("A pause or stop takes effect at the next dispatch "
                         "boundary. A call already inside a tool handler runs to "
                         "completion -- Python cannot pre-empt a thread, and "
                         "killing the process to interrupt one tool would stop "
                         "everything else with it."),
            }

    # -- control -----------------------------------------------------------
    def pause(self, name: str, *, by: str = "user", reason: str = "") -> Worker:
        with self._lock:
            worker = self._require(name)
            if worker.stopped:
                raise WorkerRefusal(name, STOPPED,
                                    "a stopped worker cannot be paused; restart "
                                    "the process to bring it back")
            worker.paused = True
            worker.paused_by = by
            worker.paused_reason = reason or "paused by the operator"
            log.info("worker paused", extra_fields={"worker": name, "by": by})
            return worker

    def resume(self, name: str, *, by: str = "user") -> Worker:
        with self._lock:
            worker = self._require(name)
            if worker.stopped:
                raise WorkerRefusal(name, STOPPED, "a stopped worker cannot resume")
            worker.paused = False
            worker.paused_by = ""
            worker.paused_reason = ""
            log.info("worker resumed", extra_fields={"worker": name, "by": by})
            return worker

    def cancel(self, name: str, *, by: str = "user", reason: str = "") -> Worker:
        """Stop a worker for good in this process.

        Not "pause": a cancelled worker does not resume automatically, so a
        person cannot be surprised by an agent coming back on its own.
        """
        with self._lock:
            worker = self._require(name)
            worker.stopped = True
            worker.paused = False
            worker.paused_by = by
            worker.paused_reason = reason or "stopped by the operator"
            log.info("worker stopped", extra_fields={"worker": name, "by": by})
            return worker

    def set_budget(self, name: str, budget: Optional[int]) -> Worker:
        """Set the call budget. ``None`` lifts it; 0 refuses every further call."""
        with self._lock:
            worker = self._require(name)
            if budget is not None and int(budget) < 0:
                raise ValueError("a call budget cannot be negative")
            worker.call_budget = None if budget is None else int(budget)
            return worker

    def _require(self, name: str) -> Worker:
        worker = self._workers.get(name)
        if worker is None:
            raise KeyError(f"no worker {name!r}")
        return worker

    # -- the gate ----------------------------------------------------------
    def admit(self, tool: str) -> Optional[Worker]:
        """Decide whether a dispatch of ``tool`` may proceed.

        Returns the worker it belongs to, or ``None`` when no worker owns the
        tool (tool names the registry does not have a tower worker for, which
        should not happen but must not be a crash). Raises `WorkerRefusal` when
        the worker is paused, stopped or out of budget -- the caller turns that
        into a refused action, never a silent skip.

        This is called by the invocation path *before* the tool runs. A worker
        whose gate is not consulted would have a pause button that only redraws
        a badge.
        """
        with self._lock:
            owner = self._tool_owner.get(tool)
            if owner is None:
                return None
            worker = self._workers[owner]
            if worker.stopped:
                worker.calls_refused += 1
                raise WorkerRefusal(owner, STOPPED,
                                    f"{owner} was stopped by the operator; the "
                                    "next call is refused")
            if worker.paused:
                worker.calls_refused += 1
                raise WorkerRefusal(owner, PAUSED,
                                    f"{owner} is paused"
                                    + (f" ({worker.paused_reason})"
                                       if worker.paused_reason else "")
                                    + "; the next call is refused until it resumes")
            if worker.call_budget is not None and \
                    worker.calls_made >= worker.call_budget:
                worker.calls_refused += 1
                raise WorkerRefusal(
                    owner, BUDGET_EXHAUSTED,
                    f"{owner} has used its budget of {worker.call_budget} call(s); "
                    "raise the budget to let it run again")
            worker.calls_made += 1
            worker.in_flight += 1
            worker.last_call_at = time.time()
            worker.last_tool = tool
            return worker

    def settle(self, tool: str, *, ok: bool, duration_ms: Optional[int] = None,
               error: str = "") -> None:
        """Record that a dispatched call finished. Always call this after admit."""
        with self._lock:
            owner = self._tool_owner.get(tool)
            if owner is None:
                return
            worker = self._workers[owner]
            worker.in_flight = max(0, worker.in_flight - 1)
            if duration_ms is not None:
                worker.last_duration_ms = int(duration_ms)
            if not ok:
                worker.failures += 1
                worker.last_error = str(error or "failed")

    def release(self, tool: str) -> None:
        """Decrement in-flight without recording a success or failure.

        For a dispatch that admitted and then could not run for a reason that is
        neither the tool's fault nor a success -- an exception before the handler
        started. Without this the worker would read "running" forever.
        """
        with self._lock:
            owner = self._tool_owner.get(tool)
            if owner is not None:
                self._workers[owner].in_flight = max(
                    0, self._workers[owner].in_flight - 1)