"""Recovery system (section 47).

    Tool failure -> Retry if safe -> Alternative tool -> Ask user -> Record failure

Three principles drive the design:

1. **Never retry something whose effect may already have landed.** A retry
   assumes the first attempt changed nothing. That is safe for a read, and safe
   for a tool that declares itself idempotent; for everything else the error
   text is not evidence. Until Phase 26 the guard sat at ``risk >= 3``, so
   **45** registered tools below it -- ``move_file``, ``write_file``,
   ``create_event``, ``schedule_task``, ``send_to_device``,
   ``control_home_device``, ``remember_fact`` among them -- were re-executed up
   to twice on a *regex over the error string*, including the case that regex
   matches best: ``command timed out after 30.0s``, where the effect is
   precisely unknown. Re-running is now granted by declaration, not by text.
2. **Never claim success without verification.** A "recovered" action is only
   reported as recovered if the retry or alternative actually returned a
   verified success.
3. **The note must describe the decision that was made.** ``recover()`` and the
   reason it prints both come out of :meth:`retry_policy`, so a note can no
   longer say "not retried" after two retries ran, or "the error is not
   transient" when it plainly was.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..logging_setup import get_logger
from ..db.database import get_db
from .models import ActionResult, PlannedAction, RiskLevel, ToolSpec, Verification, new_id, utcnow

log = get_logger("core.recovery")

#: Failure text that *may* mean nothing happened. Since Phase 26 this is only
#: used to stop retrying early and to word the note -- it never authorises a
#: retry on its own.
TRANSIENT_PATTERNS = [
    r"timed?\s*out",
    r"timeout",
    r"temporarily unavailable",
    r"resource busy",
    r"device not ready",
    r"connection refused",
    r"connection reset",
    r"network is unreachable",
    r"try again",
    r"interrupted",
    r"locked",
    r"EAGAIN",
    r"EBUSY",
    r"ETIMEDOUT",
]

#: The only tools that may be re-run without proving the first attempt inert.
#: Strictly read-only or idempotent operations, declared by name; a tool earns
#: its way in here by argument, not because its error looked transient.
IDEMPOTENT_TOOLS = {
    "get_time",
    "system_status",
    "battery_status",
    "list_apps",
    "list_directory",
    "read_file",
    "search_files",
    "search_memory",
    "project_summary",
    "explain_failures",
    "get_car_status",
    "get_car_telemetry",
    "list_car_metrics",
    "get_car_diagnostics",
    "get_car_service_status",
    "get_location_summary",
}

#: Fallback tool chains, tried in order when the primary keeps failing.
ALTERNATIVES: Dict[str, List[str]] = {
    # Spotlight can be unavailable or its index stale; a bounded walk works.
    "search_files": ["list_directory"],
    "read_file": ["search_files"],
    "list_apps": [],
    "get_car_status": [],
}


@dataclass
class RecoveryOutcome:
    """What recovery managed to achieve."""

    recovered: bool = False
    strategy: str = "none"  # none | retry | alternative | escalated | gave_up
    attempts: int = 0
    result: Optional[ActionResult] = None
    note: str = ""
    tried: List[str] = field(default_factory=list)
    #: The planned tool this outcome answers for. A successful fallback returns
    #: another tool's ActionResult, and without this field the ledger row for
    #: ``search_files`` silently claimed a directory listing was a search
    #: (Phase 9 R-14).
    answered_for: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "recovered": self.recovered,
            "strategy": self.strategy,
            "attempts": self.attempts,
            "note": self.note,
            "tried": self.tried,
            "answered_for": self.answered_for,
        }


def looks_transient(result: ActionResult) -> bool:
    """True when the error text *may* mean nothing happened.

    Advisory only: since Phase 26 no tool is re-run on this verdict alone.
    """
    haystack = f"{result.error} {result.summary}".lower()
    return any(re.search(pattern, haystack, re.I) for pattern in TRANSIENT_PATTERNS)


class RecoveryEngine:
    """Decides how to respond when a tool call fails."""

    def __init__(self, max_retries: int = 2, base_delay: float = 0.2) -> None:
        self.max_retries = max(0, max_retries)
        self.base_delay = base_delay
        self.db = get_db()
        # Phase 9 R-13: ``record_failure`` used to swallow every exception at
        # ``log.info``, so a failure could be recorded nowhere with no signal
        # while ``failure_stats()`` raised. The counters make the loss visible
        # on the same page that reports the recovery rate.
        self._written = 0
        self._unwritten = 0
        self._last_write_error = ""

    # -- policy --------------------------------------------------------------
    def retry_policy(
        self, spec: Optional[ToolSpec], result: ActionResult
    ) -> Tuple[bool, str]:
        """``(may_retry, why_not)`` -- the single source of the retry decision.

        Keyed on whether running the tool a second time is known to be safe,
        not on whether its error sounded transient.
        """
        if spec is None:
            return False, "it is not a registered tool, so nothing is known about it"
        if "safety" in spec.tags:
            return False, "it is a safety action"
        if spec.risk >= RiskLevel.DESTRUCTIVE:
            return False, (
                "it is a destructive action, and a retry could act on a world "
                "you did not approve"
            )
        if spec.risk is RiskLevel.READ or spec.name in IDEMPOTENT_TOOLS:
            return True, ""
        return False, (
            "its first attempt may have changed something already, so running "
            "it twice is not known to be safe"
        )

    def is_retryable(self, spec: Optional[ToolSpec], result: ActionResult) -> bool:
        """Whether an automatic retry is safe for this tool and failure."""
        return self.retry_policy(spec, result)[0]

    def alternatives_for(self, tool: str) -> List[str]:
        return list(ALTERNATIVES.get(tool, []))

    # -- execution -----------------------------------------------------------
    async def recover(
        self,
        action: PlannedAction,
        ctx: Any,
        execute,
        spec: Optional[ToolSpec],
        first_result: ActionResult,
    ) -> RecoveryOutcome:
        """Attempt to recover from a failed action.

        ``execute`` is an async callable ``(action, ctx) -> ActionResult`` --
        normally the agent's executor, injected so recovery works for any agent.
        """
        outcome = RecoveryOutcome(result=first_result, note=first_result.summary,
                                  answered_for=action.tool)
        skip_reason = ""
        stopped_reason = ""

        # --- 1. retry -------------------------------------------------------
        may_retry, skip_reason = self.retry_policy(spec, first_result)
        if may_retry:
            for attempt in range(1, self.max_retries + 1):
                delay = self.base_delay * (2 ** (attempt - 1))
                await asyncio.sleep(delay)
                outcome.attempts += 1
                outcome.tried.append(f"retry#{attempt}")

                log.info(
                    "retrying action",
                    extra_fields={"tool": action.tool, "attempt": attempt, "delay": delay},
                )
                retry_result = await execute(action, ctx)
                # A retry has only recovered the failure if the second attempt
                # established its effect. Gating on ``ok`` alone let Phase 9
                # reproduce "No device was switched" followed by "(Recovered
                # automatically -- succeeded on retry 1)" with recovered=1
                # written to the failures ledger for an action that did nothing.
                if retry_result.verification is Verification.SUCCESS:
                    outcome.recovered = True
                    outcome.strategy = "retry"
                    outcome.result = retry_result
                    outcome.note = f"succeeded on retry {attempt}"
                    self.record_failure(action, first_result, outcome)
                    return outcome

                outcome.result = retry_result
                if not looks_transient(retry_result):
                    # A deterministic error will not fix itself; stop early.
                    stopped_reason = "the error is not transient"
                    break
        else:
            outcome.tried.append("retry:skipped")

        # --- 2. alternative tool -------------------------------------------
        for alternative in self.alternatives_for(action.tool):
            if alternative == action.tool:
                continue
            outcome.attempts += 1
            outcome.tried.append(f"alternative:{alternative}")
            log.info(
                "trying alternative tool",
                extra_fields={"from": action.tool, "to": alternative},
            )
            alt_action = PlannedAction(
                step_id=action.step_id,
                tool=alternative,
                params=dict(action.params),
                device=action.device,
                reason=f"fallback for {action.tool}",
            )
            alt_result = await execute(alt_action, ctx)
            # Same rule as the retry: an alternative that answers without
            # observing the world has not recovered anything, it has only
            # changed which claim gets made.
            if alt_result.verification is Verification.SUCCESS:
                outcome.recovered = True
                outcome.strategy = "alternative"
                outcome.result = alt_result
                outcome.note = f"recovered using {alternative}"
                self.record_failure(action, first_result, outcome)
                return outcome
            outcome.result = alt_result

        # --- 3. escalate ----------------------------------------------------
        outcome.strategy = "escalated"
        retries = sum(1 for t in outcome.tried if t.startswith("retry#"))
        if retries:
            # Phase 9 measured notes saying "not retried" after two retries had
            # run. The count comes from what this call actually attempted.
            detail = (f" (retried {retries} time(s), then stopped: "
                      f"{stopped_reason or skip_reason})")
        elif skip_reason:
            detail = f" (not retried: {skip_reason})"
        else:
            detail = ""
        outcome.note = (
            f"{first_result.summary} — I could not recover automatically{detail}"
        )
        self.record_failure(action, first_result, outcome)
        return outcome

    # -- failure log ---------------------------------------------------------
    def record_failure(
        self,
        action: PlannedAction,
        failure: ActionResult,
        outcome: RecoveryOutcome,
    ) -> bool:
        """Persist the failure and its recovery, for later planning.

        Returns whether the row was written. A record that cannot be written is
        counted and surfaced by :meth:`recording_status` -- it is never again
        dropped at ``log.info``, because a silently empty ledger reads exactly
        like a system that has never failed.
        """
        try:
            self.db.execute(
                """
                INSERT INTO failures
                    (failure_id, tool, params, device, error, recovered,
                     strategy, attempts, note, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id("FAIL"),
                    action.tool,
                    json.dumps(action.params, default=str),
                    action.device,
                    failure.error or failure.summary,
                    1 if outcome.recovered else 0,
                    outcome.strategy,
                    outcome.attempts,
                    outcome.note,
                    utcnow().isoformat(),
                ),
            )
        except Exception as exc:  # pragma: no cover - never break the pipeline
            self._unwritten += 1
            self._last_write_error = str(exc)
            log.warning(
                "failure record could not be written",
                extra_fields={"tool": action.tool, "error": str(exc),
                              "unwritten": self._unwritten},
            )
            return False
        self._written += 1
        return True

    def recording_status(self) -> Dict[str, Any]:
        """How many failure records this engine has written and lost."""
        return {
            "written": self._written,
            "unwritten": self._unwritten,
            "last_error": self._last_write_error or None,
        }

    def recent_failures(self, limit: int = 20) -> List[Dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM failures ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in rows]

    def failure_stats(self) -> Dict[str, Any]:
        """Failure and recovery counts, used by /metrics.

        An unreadable ledger is reported as unreadable rather than raising
        through the endpoint (which left the operator with no health page at the
        exact moment the health data was broken) and rather than reporting zero
        failures, which is what a silently-empty ledger looked like before.
        """
        empty = {
            "total_failures": 0,
            "total_recovered": 0,
            "recovery_rate": 0.0,
            "by_tool": {},
            "ledger_error": None,
            **self.recording_status(),
        }
        try:
            rows = self.db.query(
                """
                SELECT tool,
                       COUNT(*) AS failures,
                       SUM(recovered) AS recovered
                FROM failures
                GROUP BY tool
                ORDER BY failures DESC
                """
            )
            by_tool = {
                r["tool"]: {
                    "failures": r["failures"],
                    "recovered": r["recovered"] or 0,
                    "recovery_rate": round((r["recovered"] or 0) / r["failures"], 3)
                    if r["failures"]
                    else 0.0,
                }
                for r in rows
            }
            totals = self.db.query(
                "SELECT COUNT(*) AS n, SUM(recovered) AS r FROM failures"
            )
            total = totals[0]["n"] if totals else 0
            recovered = (totals[0]["r"] or 0) if totals else 0
        except Exception as exc:  # pragma: no cover - DB unavailable
            empty["ledger_error"] = str(exc)
            log.warning("failure ledger could not be read", extra_fields={"error": str(exc)})
            return empty
        return {
            "total_failures": total,
            "total_recovered": recovered,
            "recovery_rate": round(recovered / total, 3) if total else 0.0,
            "by_tool": by_tool,
            "ledger_error": None,
            **self.recording_status(),
        }
