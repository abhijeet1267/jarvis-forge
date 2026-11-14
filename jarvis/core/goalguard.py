"""Goal guard: which standing goals are due to be looked at, and one look each.

Vantage can watch a goal -- a local service answers, a file still holds the text
it should -- on a cadence, without a person asking each time. Three properties
make that safe:

* **Monitoring is opted into and bounded.** Calling ``add()`` is the opt-in, and
  ``add()`` refuses any interval shorter than 30 seconds. There is no
  "check as often as possible" setting to fall into, and a goal only exists
  because someone deliberately created it.
* **The cadence lives in the goal's persisted state, not in a timer.** ``due()``
  answers "what has earned another look right now" from ``last_observed_at``,
  and it hands each goal out at most once per interval even if it is called
  repeatedly at the same moment. A scheduler therefore never needs its own
  per-goal timers, and a restart cannot duplicate a monitor loop: the new
  process asks ``due()`` and gets the same answer the old one would have.
* **A disabled goal is never observed.** ``due()`` filters on ``enabled`` and
  ``run_due_observations`` checks it again immediately before dialing out.

**This module owns no background thread and contains no ``while True``.** It is
a pure "what is due, go look" primitive: ``due()`` and
``run_due_observations()`` each run once per call and return. Whatever cadence
drives them -- the scheduler, an HTTP route, a cron job -- belongs to the
caller. Keeping the loop out of here is what leaves exactly one place that
decides to poll, instead of one per caller.

Observation itself is *not* reimplemented here. The actual look goes through the
observers in :mod:`jarvis.core.selfhealing` (``observe_http``, ``observe_file``),
so a goal checked on a schedule and a goal checked during recovery see the world
the same way. Repair is also not here: ``RecoveryController`` decides what, if
anything, may be done about a failed observation.

Persistence is one JSON document at ``<JARVIS_HOME>/goals.json`` (or an injected
path), written atomically with mode 0600 like the other small stores.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .selfhealing import GoalObservation, observe_file, observe_http

#: The floor on how often one goal may be looked at. Thirty seconds is long
#: enough that a scheduler cannot turn monitoring into a busy loop, and short
#: enough to notice a local service that died. Enforced rather than clamped:
#: silently slowing a 1-second request would hide that it was wrong.
MIN_INTERVAL_SECONDS = 30.0

#: What a goal gets when its creator did not pick a cadence. One look a minute
#: is the modest default; nothing here polls faster than the floor above.
DEFAULT_INTERVAL_SECONDS = 60.0

#: The two shapes of goal this module knows how to observe.
GOAL_KINDS: tuple = ("http", "file")

#: The only outcomes a recorded observation may carry. There is deliberately no
#: "ok"-ish synonym: an observer says verified, failed, or unknown.
OBSERVATION_RESULTS: tuple = ("verified", "failed", "unknown")

#: The file under ``JARVIS_HOME`` that holds every goal and its last result.
GOALS_FILE_NAME = "goals.json"

#: Detail text is for a person reading a screen or a log line, not an archive.
MAX_DETAIL_LENGTH = 500


class GoalStoreError(ValueError):
    """The goals file cannot be read or does not hold the expected shape.

    A ``ValueError`` subclass so callers already handling validation errors
    catch it too. Raised instead of degrading to an empty store: treating a
    corrupt file as "no goals" would silently drop every standing monitor.
    """


def _default_path() -> Path:
    """``<JARVIS_HOME>/goals.json``, the same home every other store uses."""
    root = os.environ.get("JARVIS_HOME") or str(Path.home() / ".jarvis-forge")
    return Path(root).expanduser() / GOALS_FILE_NAME


@dataclass
class Goal:
    """One standing thing to keep an eye on, plus what the last look found."""

    goal_id: str
    kind: str                                  # "http" | "file"
    target: str                                # URL or filesystem path
    expect_body: str = ""                      # http only: substring of the body
    expect_text: Optional[str] = None          # file only: substring of the text
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS
    enabled: bool = True
    created_at: float = field(default_factory=time.time)
    last_observed_at: float = 0.0              # 0 means "never looked at yet"
    last_result: str = ""                      # verified | failed | unknown | ""
    last_detail: str = ""                      # what the observer actually saw
    run_count: int = 0
    #: Failed observations in a row. Incremented only on "failed" and reset on
    #: "verified"; an "unknown" (could not look) leaves it untouched because
    #: silence is not evidence either way.
    consecutive_failures: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "goal_id": self.goal_id,
            "kind": self.kind,
            "target": self.target,
            "expect_body": self.expect_body,
            "expect_text": self.expect_text,
            "interval_seconds": self.interval_seconds,
            "enabled": self.enabled,
            "created_at": self.created_at,
            "last_observed_at": self.last_observed_at,
            "last_result": self.last_result,
            "last_detail": self.last_detail,
            "run_count": self.run_count,
            "consecutive_failures": self.consecutive_failures,
        }

    def public(self) -> Dict[str, Any]:
        """The same view, with "never observed" made explicit for a UI."""
        out = self.to_dict()
        out["due_at"] = (self.last_observed_at + self.interval_seconds
                         if self.last_observed_at else None)
        return out


def _goal_from_dict(raw: Dict[str, Any]) -> Goal:
    """Build a Goal from stored JSON, tolerating fields older files lack."""
    expect_text = raw.get("expect_text")
    return Goal(
        goal_id=str(raw.get("goal_id") or ""),
        kind=str(raw.get("kind") or ""),
        target=str(raw.get("target") or ""),
        expect_body=str(raw.get("expect_body") or ""),
        expect_text=None if expect_text is None else str(expect_text),
        interval_seconds=_as_float(raw.get("interval_seconds"),
                                   DEFAULT_INTERVAL_SECONDS),
        enabled=bool(raw.get("enabled", True)),
        created_at=_as_float(raw.get("created_at"), 0.0),
        last_observed_at=_as_float(raw.get("last_observed_at"), 0.0),
        last_result=str(raw.get("last_result") or ""),
        last_detail=str(raw.get("last_detail") or ""),
        run_count=int(raw.get("run_count") or 0),
        consecutive_failures=int(raw.get("consecutive_failures") or 0),
    )


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class GoalStore:
    """The goals document: add, list, enable, disable, remove, and say what is due.

    The store is the only place that knows when a goal was last looked at, which
    is what lets callers be stateless. It does not observe anything itself --
    that is :func:`run_due_observations` -- and it does not repair anything.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path is not None else _default_path()
        # The API and a scheduler can share one process; the lock keeps two
        # read-modify-write cycles from losing each other's observation. Cross
        # process writers rely on the atomic replace below, not on this lock.
        self._lock = threading.RLock()
        #: When this process last handed each goal out from ``due()``. In
        #: memory on purpose: a claim is "I am about to look right now", which
        #: says nothing durable. After a restart an unobserved goal is due
        #: again, which is correct -- it was never actually looked at.
        self._claimed: Dict[str, float] = {}

    # -- persistence --------------------------------------------------------

    def _read(self) -> Dict[str, Any]:
        if not self.path.is_file():
            return {}
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise GoalStoreError(
                f"the goals file {self.path} cannot be read: {exc}") from exc
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise GoalStoreError(
                f"the goals file {self.path} is not valid JSON; it is left "
                "untouched so its contents are not silently replaced") from exc
        if not isinstance(data, dict):
            raise GoalStoreError(
                f"the goals file {self.path} must hold a JSON object")
        return data

    def _write(self, data: Dict[str, Any]) -> None:
        """Write the whole document to a temp file, chmod 0600, then replace.

        ``os.replace`` makes the swap atomic on one filesystem, so a crash
        mid-write leaves the previous complete file rather than a truncated one.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".goals-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
                handle.write("\n")
            try:
                os.chmod(tmp_name, 0o600)
            except OSError:
                pass
            os.replace(tmp_name, self.path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    @staticmethod
    def _block(data: Dict[str, Any]) -> Dict[str, Any]:
        block = data.get("goals")
        return block if isinstance(block, dict) else {}

    def _entries(self) -> List[Goal]:
        goals = [_goal_from_dict(raw) for raw in self._block(self._read()).values()
                 if isinstance(raw, dict)]
        goals.sort(key=lambda g: (g.created_at, g.goal_id))
        return goals

    # -- writing ------------------------------------------------------------

    def add(self, goal: Dict[str, Any]) -> Goal:
        """Validate and store one goal, returning it as persisted.

        This is the opt-in point, so the validation is strict rather than
        forgiving:

        * ``kind`` must be ``"http"`` or ``"file"``, and ``target`` must be a
          URL of that kind or a non-empty path.
        * ``expect_body`` belongs to http goals and ``expect_text`` to file
          goals; setting the wrong one is refused instead of stored as a
          silent no-op.
        * ``interval_seconds`` below 30 is refused with: "monitoring must be
          opt-in and bounded; the shortest allowed interval is 30s". A cadence
          that fast turns watching a goal into a polling loop.
        """
        raw = goal if isinstance(goal, dict) else {}

        kind = str(raw.get("kind") or "").strip().lower()
        if kind not in GOAL_KINDS:
            raise ValueError(
                f"{kind!r} is not a goal kind. Known: {', '.join(GOAL_KINDS)}.")

        target = str(raw.get("target") or "").strip()
        if not target:
            raise ValueError(
                "a goal needs a target: a URL for an http goal or a path for a "
                "file goal")
        if kind == "http" and not target.lower().startswith(("http://", "https://")):
            raise ValueError(
                f"an http goal target must be an http(s) URL, got {target!r}")

        expect_body = str(raw.get("expect_body") or "")
        expect_text_raw = raw.get("expect_text")
        expect_text = None if expect_text_raw is None else str(expect_text_raw)
        if kind == "file" and expect_body:
            raise ValueError("expect_body applies to http goals; use expect_text "
                             "for a file goal")
        if kind == "http" and expect_text is not None:
            raise ValueError("expect_text applies to file goals; use expect_body "
                             "for an http goal")

        raw_interval = raw.get("interval_seconds", DEFAULT_INTERVAL_SECONDS)
        try:
            interval = float(raw_interval)
        except (TypeError, ValueError):
            raise ValueError(
                f"interval_seconds must be a number, got {raw_interval!r}") from None
        if interval != interval:  # NaN: comparisons below would all be false
            raise ValueError("interval_seconds must be a number, got NaN")
        if interval < MIN_INTERVAL_SECONDS:
            raise ValueError(
                "monitoring must be opt-in and bounded; the shortest allowed "
                f"interval is 30s (got {interval:g}s)")

        goal_id = str(raw.get("goal_id") or "").strip()
        if not goal_id:
            goal_id = f"goal-{uuid.uuid4().hex[:12]}"

        with self._lock:
            data = self._read()
            block = self._block(data)
            if goal_id in block:
                raise ValueError(
                    f"a goal with id {goal_id!r} already exists; remove it first "
                    "if it should be replaced")
            entry = Goal(
                goal_id=goal_id,
                kind=kind,
                target=target,
                expect_body=expect_body,
                expect_text=expect_text,
                interval_seconds=interval,
                enabled=bool(raw.get("enabled", True)),
                created_at=time.time(),
            )
            block[goal_id] = entry.to_dict()
            data["version"] = 1
            data["goals"] = block
            self._write(data)
            return entry

    def enable(self, goal_id: str) -> Goal:
        """Turn a goal's monitoring back on and return the updated goal."""
        return self._set_enabled(goal_id, True)

    def disable(self, goal_id: str) -> Goal:
        """Stop a goal being observed without forgetting its history."""
        return self._set_enabled(goal_id, False)

    def _set_enabled(self, goal_id: str, enabled: bool) -> Goal:
        with self._lock:
            data = self._read()
            block = self._block(data)
            entry = block.get(goal_id)
            if not isinstance(entry, dict):
                raise KeyError(f"no goal with id {goal_id!r}")
            entry["enabled"] = bool(enabled)
            block[goal_id] = entry
            data["goals"] = block
            self._write(data)
            return _goal_from_dict(entry)

    def remove(self, goal_id: str) -> bool:
        """Delete a goal entirely. Returns whether it existed."""
        with self._lock:
            data = self._read()
            block = self._block(data)
            if goal_id not in block:
                return False
            block.pop(goal_id, None)
            data["goals"] = block
            self._write(data)
            self._claimed.pop(goal_id, None)
            return True

    def record_observation(self, goal_id: str, *, result: str, detail: str = "",
                           now: Optional[float] = None) -> Goal:
        """Store what one look found and return the updated goal.

        ``consecutive_failures`` is incremented only for ``"failed"`` and reset
        to 0 on ``"verified"``. An ``"unknown"`` -- the observer could not look
        -- changes neither: treating silence as a failure would escalate on a
        network blip, and treating it as health would hide one.
        """
        if result not in OBSERVATION_RESULTS:
            raise ValueError(
                f"{result!r} is not an observation result. "
                f"Known: {', '.join(OBSERVATION_RESULTS)}.")
        moment = float(now) if now is not None else time.time()

        with self._lock:
            data = self._read()
            block = self._block(data)
            entry = block.get(goal_id)
            if not isinstance(entry, dict):
                raise KeyError(f"no goal with id {goal_id!r}")

            entry["last_observed_at"] = moment
            entry["last_result"] = result
            entry["last_detail"] = str(detail or "")[:MAX_DETAIL_LENGTH]
            entry["run_count"] = int(entry.get("run_count") or 0) + 1
            failures = int(entry.get("consecutive_failures") or 0)
            if result == "failed":
                failures += 1
            elif result == "verified":
                failures = 0
            entry["consecutive_failures"] = failures

            block[goal_id] = entry
            data["goals"] = block
            self._write(data)
            # The observation is now the durable record; the in-memory claim is
            # folded into it. Max, not plain assignment: a caller whose clock is
            # behind the last claim must not reopen the duplicate-look window.
            self._claimed[goal_id] = max(moment, self._claimed.get(goal_id, 0.0))
            return _goal_from_dict(entry)

    # -- reading ------------------------------------------------------------

    def list(self) -> List[Goal]:
        """Every goal, oldest first. Disabled goals included: they still exist."""
        with self._lock:
            return self._entries()

    def get(self, goal_id: str) -> Optional[Goal]:
        """One goal, or None when that id is unknown."""
        with self._lock:
            entry = self._block(self._read()).get(goal_id)
            return _goal_from_dict(entry) if isinstance(entry, dict) else None

    def due(self, now: Optional[float] = None) -> List[Goal]:
        """The enabled goals that have earned a look at ``now``.

        A goal is due when it has never been observed, or when at least its own
        interval has passed since the last look (or since this process last
        handed it out).

        **This is the duplicate-loop guard.** Each call records an in-memory
        claim at ``now``, so calling ``due()`` repeatedly in a tight loop hands
        each goal out exactly once per interval -- the second call at the same
        moment returns nothing. A scheduler that asks this question instead of
        keeping timers cannot run two loops over the same goal, and a restarted
        process cannot either: its answer is derived from the persisted
        ``last_observed_at``, so a look that already happened is not repeated.
        """
        moment = float(now) if now is not None else time.time()
        out: List[Goal] = []
        with self._lock:
            for goal in self._entries():
                if not goal.enabled:
                    continue
                last_look = max(goal.last_observed_at,
                                self._claimed.get(goal.goal_id, 0.0))
                if last_look <= 0.0 or last_look + goal.interval_seconds <= moment:
                    self._claimed[goal.goal_id] = moment
                    out.append(goal)
        return out


def _observe(goal: Goal) -> GoalObservation:
    """One look at one goal, through the existing self-healing observers."""
    try:
        if goal.kind == "http":
            return observe_http(goal.target, expect_body=goal.expect_body,
                                goal_id=goal.goal_id)
        return observe_file(goal.target, expect_text=goal.expect_text,
                            goal_id=goal.goal_id)
    except Exception as exc:
        # One goal whose observer raises must not abort the rest of the pass,
        # and must not be recorded as healthy: "unknown" is the honest answer
        # to "I could not look", and the error is kept in the detail.
        return GoalObservation(
            goal_id=goal.goal_id, observer=goal.kind, target=goal.target,
            expected="", actual=f"observer error: {str(exc)[:200]}",
            result="unknown")


def run_due_observations(store: GoalStore, *,
                         now: Optional[float] = None) -> List[Dict[str, str]]:
    """Look at every due goal once and record what was found.

    The caller decides the cadence; this function runs once and returns. Each
    observation is written back through ``store.record_observation`` using the
    same ``now``, so several goals checked in one pass share a timestamp and
    none becomes due again until its own interval has passed.

    A disabled goal is skipped even if it appeared in the due list, so a
    ``disable()`` that landed between the two steps still wins. Returns one
    ``{goal_id, result, detail}`` dict per goal actually observed.
    """
    moment = float(now) if now is not None else time.time()
    outcomes: List[Dict[str, str]] = []
    for goal in store.due(moment):
        if not goal.enabled:
            continue
        observation = _observe(goal)
        store.record_observation(goal.goal_id, result=observation.result,
                                 detail=observation.actual, now=moment)
        outcomes.append({
            "goal_id": goal.goal_id,
            "result": observation.result,
            "detail": observation.actual,
        })
    return outcomes