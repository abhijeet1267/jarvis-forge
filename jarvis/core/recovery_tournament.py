"""Recovery Tournament: rank the safe repairs for a failure, and say why.

The recovery engine already knows *which* repairs are safe -- `retry_policy`
decides whether a retry is allowed at all, `ALTERNATIVES` names the fallback
tools, `looks_transient` says whether the error might clear on its own. What it
does not do is **compare** them. `recover()` takes the first option that
verifies and stops; a person watching cannot see that a cheaper or more
targeted repair existed, or why the one chosen was chosen.

The tournament makes the comparison visible. For one failure it produces every
candidate the engine would consider, each with:

* a **safety verdict** taken from `retry_policy` and the tool spec -- never a
  model's opinion. A destructive tool is not "probably fine to retry"; it is
  unsafe, and the candidate says so and is ranked last regardless of anything
  else.
* a **likelihood** of success, derived from declared properties, not invented
  confidence: whether the error looks transient, whether the fallback addresses
  the same intent, the tool's own risk.
* a **cost**: what the repair touches, how many calls it spends.

The ordering is deterministic and the reasons are printed, so the ranking can be
disagreed with. There is no score that hides its inputs.

This module makes no claim that the top-ranked candidate *will* succeed. It
ranks which repair is the best first thing to try, and says what it is betting
on. Whether it worked is decided afterwards, by the same independent verifier
the runner already uses -- a tournament is a plan, not a proof.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..logging_setup import get_logger
from .models import ActionResult, RiskLevel, ToolSpec
from .recovery import ALTERNATIVES, IDEMPOTENT_TOOLS, looks_transient

log = get_logger("core.recovery_tournament")

#: Ranked repair kinds, in the order a person would normally prefer them: a
#: retry that is safe is cheapest and best; a same-intent fallback tool next;
#: asking the person, last.
RETRY = "retry"
ALTERNATIVE = "alternative"
ASK = "ask"

#: Safety verdicts. There is no middle "probably safe": a repair is safe, or it
#: is not, or it is unsafe. The middle value exists only to describe a candidate
#: that is safe-but-unverified (a retry whose effect is idempotent but whose
#: result is not independently observed).
SAFE = "safe"
UNSAFE = "unsafe"


@dataclass
class Candidate:
    """One repair the tournament would consider, with its reasoning."""

    kind: str                      # retry | alternative | ask
    tool: str
    safety: str                    # safe | unsafe
    #: Why it is safe or unsafe, quoted from the rule that decided it.
    safety_reason: str
    #: Why this is ranked where it is -- the declared properties, named.
    likelihood: str
    #: What a person pays for trying it: calls spent and what it touches.
    cost: str
    #: A sortable rank; lower is better. Only safe candidates get a real rank.
    rank: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "tool": self.tool,
            "safety": self.safety,
            "safety_reason": self.safety_reason,
            "likelihood": self.likelihood,
            "cost": self.cost,
            "rank": self.rank,
        }


@dataclass
class Tournament:
    """Every safe repair for one failure, ranked, with the one worth trying first."""

    tool: str
    failure: str
    candidates: List[Candidate] = field(default_factory=list)

    @property
    def recommended(self) -> Optional[Candidate]:
        """The best *automatic* repair, or None when nothing can run on its own.

        ``ask`` is never recommended here even though it is always safe: it is
        not an automatic repair, it is what remains when none is. ``None`` is
        the honest answer for a destructive failure with no safe fallback -- the
        only move left is to ask, which the caller does explicitly rather than
        reading a recommendation that was never safe to make.
        """
        for candidate in self.candidates:
            if candidate.kind != ASK and candidate.safety == SAFE:
                return candidate
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tool": self.tool,
            "failure": self.failure,
            "candidates": [c.to_dict() for c in self.candidates],
            "recommended": self.recommended.to_dict() if self.recommended else None,
            "note": self._note(),
        }

    def _note(self) -> str:
        top = self.recommended
        if top is None:
            return ("No repair is safe to attempt automatically for this "
                    "failure. A destructive or unknown action is not retried; "
                    "the person has to decide.")
        return (f"Best first attempt: {top.kind} via {top.tool} — {top.likelihood}. "
                "The ranking is a plan, not a proof; whether it worked is decided "
                "afterwards by the same independent check as any run.")


class RecoveryTournament:
    """Builds the candidate set for a failure and ranks it.

    It reuses `recovery`'s own policy and tables rather than restating them, so
    the tournament can never recommend a repair `recover()` would refuse. That
    is the whole point: a ranked list that disagreed with the executor would be
    advice the system does not follow.
    """

    def __init__(self, engine=None) -> None:
        #: The recovery engine whose `retry_policy` and alternatives this uses.
        #: Injected so the tournament ranks against exactly the rules in force.
        self._engine = engine

    def _policy(self, spec: Optional[ToolSpec], result: ActionResult):
        if self._engine is not None:
            return self._engine.retry_policy(spec, result)
        return _default_retry_policy(spec, result)

    def _alternatives(self, tool: str) -> List[str]:
        if self._engine is not None:
            return self._engine.alternatives_for(tool)
        return list(ALTERNATIVES.get(tool, []))

    def evaluate(self, *, tool: str, spec: Optional[ToolSpec],
                 result: ActionResult, registry=None,
                 alternatives: Optional[List[str]] = None) -> Tournament:
        """Rank the safe repairs for one failed call.

        ``registry`` (a ToolRegistry, optional) lets the tournament describe an
        alternative's cost from its real spec; without it, alternatives are
        still ranked but their cost is stated as unknown rather than guessed.
        """
        tour = Tournament(tool=tool, failure=result.summary or str(result.error or ""))

        may_retry, why_not = self._policy(spec, result)
        transient = looks_transient(result)

        # -- retry -----------------------------------------------------------
        if may_retry:
            likelihood = (
                "the error looks transient and the tool is safe to re-run"
                if transient else
                "the tool is safe to re-run, though the error did not look transient")
            tour.candidates.append(Candidate(
                kind=RETRY, tool=tool, safety=SAFE,
                safety_reason=("re-running it is safe: it is read-only or "
                               "declares itself idempotent"),
                likelihood=likelihood,
                cost="1 call; the first attempt's effect is known not to matter",
                rank=0,
            ))
        else:
            tour.candidates.append(Candidate(
                kind=RETRY, tool=tool, safety=UNSAFE,
                safety_reason=why_not or "re-running it is not known to be safe",
                likelihood="n/a — not attempted",
                cost="",
                rank=999,
            ))

        # -- alternatives ----------------------------------------------------
        pool = alternatives if alternatives is not None else self._alternatives(tool)
        for index, alt in enumerate(pool):
            if alt == tool:
                continue
            alt_spec = None
            if registry is not None:
                try:
                    alt_spec = registry.get(alt)
                except Exception:
                    alt_spec = None
            safety, safety_reason = _alternative_safety(alt_spec)
            cost = _alternative_cost(alt_spec, registry is not None)
            tour.candidates.append(Candidate(
                kind=ALTERNATIVE, tool=alt, safety=safety,
                safety_reason=safety_reason,
                likelihood=("a declared fallback that answers the same intent"
                            if alt_spec is not None or registry is None else
                            "an alternative, but not a registered tool"),
                cost=cost,
                rank=10 + index if safety == SAFE else 999,
            ))

        # -- ask -------------------------------------------------------------
        tour.candidates.append(Candidate(
            kind=ASK, tool=tool, safety=SAFE,
            safety_reason="asking changes nothing on its own",
            likelihood=("the person decides; correct when no automatic repair "
                        "is safe or likely"),
            cost="no calls; the person's time",
            rank=500,
        ))

        tour.candidates.sort(key=lambda c: c.rank)
        log.info("recovery tournament", extra_fields={
            "tool": tool, "candidates": len(tour.candidates),
            "recommended": (tour.recommended.tool if tour.recommended else None)})
        return tour


def _alternative_safety(spec: Optional[ToolSpec]) -> tuple:
    """Whether an alternative tool is safe to attempt as a fallback.

    Unknown spec is not "safe": a tool the registry does not describe cannot be
    vouched for, so it is unsafe and ranked last. Destructive is unsafe for the
    same reason a destructive primary is not retried.
    """
    if spec is None:
        return UNSAFE, ("not a registered tool, so nothing is known about what "
                        "running it would do")
    if "safety" in spec.tags:
        return UNSAFE, "it is a safety action"
    if spec.risk >= RiskLevel.DESTRUCTIVE:
        return UNSAFE, "it is destructive, so a fallback could act on a world you did not approve"
    return SAFE, f"declared risk {int(spec.risk)} with no safety tag"


def _alternative_cost(spec: Optional[ToolSpec], described: bool) -> str:
    if not described or spec is None:
        return "1 call; the fallback's effect is unknown to this ranking"
    reversible = "reversible" if spec.reversible else "not reversible"
    return f"1 call; {reversible}, risk {int(spec.risk)}"


def _default_retry_policy(spec: Optional[ToolSpec], result: ActionResult):
    """The same decision `RecoveryEngine.retry_policy` makes, for a bare tournament.

    Duplicated rather than imported behind a constructed engine, because
    building a `RecoveryEngine` opens the database. A test pins the two to the
    same verdicts so they cannot drift.
    """
    if spec is None:
        return False, "it is not a registered tool, so nothing is known about it"
    if "safety" in spec.tags:
        return False, "it is a safety action"
    if spec.risk >= RiskLevel.DESTRUCTIVE:
        return False, ("it is a destructive action, and a retry could act on a "
                       "world you did not approve")
    if spec.risk is RiskLevel.READ or spec.name in IDEMPOTENT_TOOLS:
        return True, ""
    return False, ("its first attempt may have changed something already, so "
                   "running it twice is not known to be safe")