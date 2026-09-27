"""Recovery system (section 47).

    Tool failure -> Retry if safe -> Alternative tool -> Ask user -> Record failure

Two principles drive the design:

1. **Never retry something destructive.** A retry assumes the first attempt
   changed nothing. For ``delete_file``, ``move_file`` or a physical action
   that assumption is false, and a second attempt can act on a world the user
   did not approve. Destructive and physical tools are therefore excluded from
   automatic retry, regardless of the error.
2. **Never claim success without verification.** A "recovered" action is only
   reported as recovered if the retry or alternative actually returned a
   verified success.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..logging_setup import get_logger
from ..db.database import get_db
from .models import ActionResult, PlannedAction, RiskLevel, ToolSpec, Verification, new_id, utcnow

log = get_logger("core.recovery")

#: Failures that are worth another attempt -- the world probably has not changed.
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

#: Tools that may be retried automatically even on a non-transient error.
#: Strictly read-only or idempotent operations.
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

    def as_dict(self) -> Dict[str, Any]:
        return {
            "recovered": self.recovered,
            "strategy": self.strategy,
            "attempts": self.attempts,
            "note": self.note,
            "tried": self.tried,
        }


def looks_transient(result: ActionResult) -> bool:
    """True when the error text suggests a retry might succeed."""
    haystack = f"{result.error} {result.summary}".lower()
    return any(re.search(pattern, haystack, re.I) for pattern in TRANSIENT_PATTERNS)


class RecoveryEngine:
    """Decides how to respond when a tool call fails."""

    def __init__(self, max_retries: int = 2, base_delay: float = 0.2) -> None:
        self.max_retries = max(0, max_retries)
        self.base_delay = base_delay
        self.db = get_db()

    # -- policy --------------------------------------------------------------
    def is_retryable(self, spec: Optional[ToolSpec], result: ActionResult) -> bool:
        """Whether an automatic retry is safe for this tool and failure."""
        if spec is None:
            return False

        # Destructive and physical actions are never retried automatically.
        # A retry could act on a world the user did not approve.
        if spec.risk >= RiskLevel.DESTRUCTIVE:
            return False
        if "safety" in spec.tags:
            return False

        # Read-only/idempotent tools can always be retried.
        if spec.name in IDEMPOTENT_TOOLS or spec.risk is RiskLevel.READ:
            return True

        # Otherwise only when the error looks transient.
        return looks_transient(result)

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
        outcome = RecoveryOutcome(result=first_result, note=first_result.summary)
        skip_reason = ""

        # --- 1. retry -------------------------------------------------------
        if self.is_retryable(spec, first_result):
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
                    skip_reason = "the error is not transient"
                    break
        else:
            outcome.tried.append("retry:skipped")
            if spec is not None and spec.risk >= RiskLevel.DESTRUCTIVE:
                skip_reason = "it is a destructive action, and a retry could act on a world you did not approve"
            elif spec is not None and "safety" in spec.tags:
                skip_reason = "it is a safety action"
            else:
                skip_reason = "the error is not transient"

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
        detail = f" (not retried: {skip_reason})" if skip_reason else ""
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
    ) -> None:
        """Persist the failure and its recovery, for later planning."""
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
            log.info("could not record failure", extra_fields={"error": str(exc)})

    def recent_failures(self, limit: int = 20) -> List[Dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM failures ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in rows]

    def failure_stats(self) -> Dict[str, Any]:
        """Failure and recovery counts, used by /metrics."""
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
        totals = self.db.query("SELECT COUNT(*) AS n, SUM(recovered) AS r FROM failures")
        total = totals[0]["n"] if totals else 0
        recovered = (totals[0]["r"] or 0) if totals else 0
        return {
            "total_failures": total,
            "total_recovered": recovered,
            "recovery_rate": round(recovered / total, 3) if total else 0.0,
            "by_tool": by_tool,
        }
