"""Capability Passports: a worker's standing, expressed as something checkable.

A worker claiming "I can do this" is not evidence of anything. A passport answers
three narrower questions from data the system already keeps:

* **What was it *given*?** The tools whose spec names this worker, what each one
  costs, and whether any is gated behind confirmation. This is a *claim about the
  registry*, not a grant of permission -- the engine still decides every call.
* **What has it *done*?** Counts read from the hash-chained audit log: how many
  actions, at what risk, and how many needed confirmation.
* **What has it *never touched*?** The tools it holds that have no audit record
  at all. That is the most useful line on a passport and the one a "capability
  list" never shows: a worker with a destructive tool it has never used is one
  whose blast radius is entirely theoretical.

Two properties matter, and both are negative:

* **A passport is a record, never a credential.** Nothing here is read by the
  permission engine, by a lease, or by any execution path. Importing it can
  grant nothing; :func:`describe_passport` only reads. The one function that
  writes, :func:`issue_passport`, records that a passport *was issued* -- an
  audit entry, not an authorization.
* **It states evidence integrity.** The audit log is a hash chain, so a passport
  carries the result of re-walking it. A passport whose evidence chain is broken
  says so rather than presenting counts as trustworthy.

Counts are what was recorded. They are not a rating, a score, or a prediction:
:func:`describe_passport` returns no aggregate number, because a single "trust
score" would be the one figure on the screen nobody could verify.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

from ..logging_setup import get_logger

log = get_logger("core.passports")

#: Risk-tier names, matching the audit log's own vocabulary.
RISK_NAMES = {0: "read", 1: "low", 2: "external", 3: "destructive", 4: "physical"}

#: How many recent audit rows a passport reads. A bound rather than "all of
#: history" so building a passport stays a fast read; the passport states that
#: its counts cover this window and whether the window was exhausted, so a
#: truncated count is never presented as a total.
AUDIT_ROWS_READ = 2000

#: Above this many passing observations a worker's tool history is described as
#: "measured" rather than "thin". Not a confidence score -- a count of its own
#: recorded actions, so a reader can see what the word rests on.
MEASURED_AFTER = 5


class PassportError(ValueError):
    """A passport cannot be produced; the message says why."""


@dataclass
class Passport:
    """One worker's standing, from the registry and from recorded actions."""

    worker: str
    tools: List[Dict[str, Any]] = field(default_factory=list)
    #: Tools this worker owns that hold a risk above READ.
    gated_tools: List[str] = field(default_factory=list)
    #: Tools this worker owns that have never appeared in the audit log.
    never_used: List[str] = field(default_factory=list)
    #: Tools this worker owns that the audit log saw at least once.
    exercised: List[str] = field(default_factory=list)
    #: Counts by risk tier, read from audit rows attributed to this worker.
    actions_by_risk: Dict[str, int] = field(default_factory=dict)
    actions_total: int = 0
    confirmations: int = 0
    refusals: int = 0
    #: Whether the audit chain re-walked clean, and what the walk found.
    evidence: Dict[str, Any] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "worker": self.worker,
            "tools": [dict(t) for t in self.tools],
            "gated_tools": list(self.gated_tools),
            "never_used": list(self.never_used),
            "exercised": list(self.exercised),
            "actions_by_risk": dict(self.actions_by_risk),
            "actions_total": self.actions_total,
            "confirmations": self.confirmations,
            "refusals": self.refusals,
            "evidence": dict(self.evidence),
            "window": dict(self.window),
            "notes": list(self.notes),
            # Stated on every passport so no reader mistakes it for authority.
            "authority": ("a record of what this worker holds and has done; it "
                          "grants nothing and the permission engine still decides "
                          "every call"),
        }


def _tool_row(spec) -> Dict[str, Any]:
    """One tool, as the passport states it: cost, gate, reach."""
    risk = int(getattr(spec, "risk", 0))
    return {
        "tool": spec.name,
        "risk": RISK_NAMES.get(risk, str(risk)),
        "risk_value": risk,
        "requires_confirmation": bool(getattr(spec, "requires_confirmation", False)),
        "reversible": bool(getattr(spec, "reversible", True)),
        "devices": list(getattr(spec, "allowed_devices", ()) or ()),
    }


def _default_registry():
    """The shipped registry, with every tool registered.

    A bare ``ToolRegistry()`` holds no tools -- the registry is populated by
    ``build_default_registry`` -- so defaulting to it would make every passport
    empty and read as "this worker can do nothing", which is the opposite of
    true. The API routes pass the core's own registry; this is the fallback for
    a caller that did not.
    """
    from ..tools import build_default_registry

    return build_default_registry()


def _registry_tools(registry, worker: str) -> List[Dict[str, Any]]:
    """The tools whose spec names this worker, costliest first."""
    rows = [_tool_row(spec) for spec in registry.all() if spec.agent == worker]
    rows.sort(key=lambda t: (-t["risk_value"], t["tool"]))
    return rows


def _rows_for_worker(audit, worker: str) -> List[Dict[str, Any]]:
    """Audit rows attributable to a worker.

    Attribution is by tool: an audited action names the tool, and the registry
    says which worker owns that tool. Rows whose tool is unknown to the registry
    are left out rather than guessed at.
    """
    try:
        rows = audit.recent(limit=AUDIT_ROWS_READ)
    except Exception as exc:
        raise PassportError(f"the audit log could not be read: {exc}") from exc
    return [row for row in rows if isinstance(row, dict)]


def build_passport(worker: str, *, registry=None, audit=None) -> Passport:
    """Assemble one worker's passport from the registry and recorded actions.

    Raises :class:`PassportError` when the worker is unknown to the registry --
    a passport for a worker nobody has heard of would be an invented document.
    """
    registry = registry if registry is not None else _default_registry()
    if audit is None:
        from ..security.audit import AuditLog

        audit = AuditLog()

    tools = _registry_tools(registry, worker)
    if not tools:
        raise PassportError(
            f"no registered tool names the worker {worker!r}, so there is no "
            "capability to describe")

    passport = Passport(worker=str(worker), tools=tools)
    owned = {t["tool"] for t in tools}
    passport.gated_tools = sorted(
        t["tool"] for t in tools if t["requires_confirmation"] or t["risk"] != "read")

    # Which owned tools the log has actually seen, and what the worker has done.
    rows = _rows_for_worker(audit, worker)
    passport.window = {
        "rows_read": len(rows),
        "rows_limit": AUDIT_ROWS_READ,
        # At the limit the log may hold more rows than were read, so the counts
        # below cover the most recent window rather than all of history.
        "complete": len(rows) < AUDIT_ROWS_READ,
    }
    seen: Dict[str, int] = {}
    refusals = 0
    confirmations = 0
    for row in rows:
        tool = str(row.get("tool") or "")
        if tool not in owned:
            continue
        seen[tool] = seen.get(tool, 0) + 1
        risk = str(row.get("risk") or "read")
        passport.actions_by_risk[risk] = passport.actions_by_risk.get(risk, 0) + 1
        passport.actions_total += 1
        if row.get("confirmation"):
            confirmations += 1
        result = str(row.get("result") or "")
        if result in {"denied", "blocked", "refused"}:
            refusals += 1
    passport.confirmations = confirmations
    passport.refusals = refusals
    passport.exercised = sorted(seen)
    passport.never_used = sorted(owned - set(seen))

    # The evidence chain. A passport that presents counts without saying whether
    # the log they came from verifies would be asking to be taken on faith.
    try:
        # `verify_chain()` reports `ok`, `checked` and `breaks` -- read that
        # contract rather than a field this module wished existed.
        chain = audit.verify_chain()
        breaks = chain.get("breaks") or []
        passport.evidence = {
            "intact": bool(chain.get("ok")),
            "checked": int(chain.get("checked") or 0),
            "breaks": int(breaks) if isinstance(breaks, int) else len(breaks),
        }
    except Exception as exc:
        passport.evidence = {"intact": False, "checked": 0, "breaks": 0,
                             "error": f"the audit chain could not be walked: {exc}"}

    if not passport.evidence.get("intact"):
        passport.notes.append(
            "the audit chain did not re-walk clean, so the action counts above "
            "come from a log whose integrity is not established")

    if not passport.window.get("complete"):
        passport.notes.append(
            f"the audit log held at least {AUDIT_ROWS_READ} rows, so the counts "
            "below cover the most recent of them rather than all of history")

    if passport.actions_total == 0:
        passport.notes.append(
            "this worker has no recorded actions: every capability below is a "
            "declaration about the registry, and none has been exercised")
    elif passport.actions_total < MEASURED_AFTER:
        passport.notes.append(
            f"{passport.actions_total} recorded action(s). That is a thin history, "
            "so the counts describe a little rather than establish a pattern")
    else:
        passport.notes.append(
            f"{passport.actions_total} recorded action(s); the counts are what the "
            "log holds, not a rating")

    if passport.never_used:
        risky = [row["tool"] for row in tools
                 if row["tool"] in passport.never_used
                 and (row["requires_confirmation"] or row["risk"] != "read")]
        if risky:
            passport.notes.append(
                "held but never used, and not read-only: " + ", ".join(sorted(risky))
                + ". Its reach is theoretical until something calls it")
    return passport


def list_passports(*, registry=None, audit=None) -> List[Dict[str, Any]]:
    """A one-line summary per worker, so a screen can list before drilling in."""
    registry = registry if registry is not None else _default_registry()
    workers = sorted({spec.agent for spec in registry.all()})
    out: List[Dict[str, Any]] = []
    for worker in workers:
        try:
            passport = build_passport(worker, registry=registry, audit=audit)
        except PassportError as exc:
            out.append({"worker": worker, "error": str(exc)})
            continue
        out.append({
            "worker": worker,
            "tools": len(passport.tools),
            "gated": len(passport.gated_tools),
            "never_used": len(passport.never_used),
            "actions_total": passport.actions_total,
            "evidence_intact": bool(passport.evidence.get("intact")),
        })
    return out


def issue_passport(worker: str, *, registry=None, audit=None) -> Dict[str, Any]:
    """Record, in the audit log, that this worker's passport was produced.

    This is the only write in the module, and it writes an *audit entry*: who was
    described, when, and what the passport said. It does not create a credential,
    a lease, or a token, and nothing reads it back to authorize anything.
    """
    passport = build_passport(worker, registry=registry, audit=audit)
    from ..security.audit import AuditLog

    log_ = audit or AuditLog()
    record = log_.record(
        intent="issue capability passport",
        tool="",
        target=str(worker),
        risk="read",
        result="ok",
        detail={
            "tools": len(passport.tools),
            "gated": len(passport.gated_tools),
            "never_used": passport.never_used,
            "actions_total": passport.actions_total,
            "evidence_intact": bool(passport.evidence.get("intact")),
            "grants": "nothing",
        },
    )
    body = passport.to_dict()
    body["audit_id"] = getattr(record, "log_id", "")
    return body