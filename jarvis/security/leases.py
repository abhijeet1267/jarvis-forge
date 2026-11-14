"""PermissionLease: a short, scoped, revocable authorization to do one thing.

The existing `permissions` table answers "may this subject use this tool at all?",
which is a standing grant. A lease answers the narrower question an autonomous
mission actually needs: *may this session, for this mission, perform this
operation on this resource, now, this many times?*

Design decisions that carry weight:

* **A lease never widens the standing grant.** It is checked *in addition to* the
  PermissionEngine, never instead of it. A mission with a lease for `write_file`
  still cannot do something the engine would refuse.
* **Scope is data, compared literally.** A lease carries a set of resource
  prefixes; a use must fall under one of them. There is no fuzzy matching, so
  `/a/bc` never satisfies a lease for `/a/b`.
* **Single use is atomic.** `consume` is a conditional UPDATE on the use count,
  so two concurrent callers cannot both spend the same last use.
* **Expiry is checked against a stored absolute time**, not a duration, so a
  clock skew in a caller cannot extend it.
* **Revocation is immediate and durable.** `revoke` writes the row; every use
  re-reads the lease rather than trusting a value cached at issue time.

A lease that has been revoked, expired, exhausted, or scoped wrongly is refused
with the reason, and the refusal is what the caller reports -- there is no path
here that silently downgrades to "allowed".
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..logging_setup import get_logger

log = get_logger("security.leases")


@dataclass
class Lease:
    lease_id: str
    mission_id: str
    session_id: str
    actor: str
    #: The capability this lease permits, by tool name. Empty means "any tool the
    #: engine already allows", which is still bounded by the resource scope.
    capability: str = ""
    #: Resource prefixes this lease may touch. Empty means no resource scope was
    #: declared, which is refused at use time rather than treated as unrestricted.
    scope: List[str] = field(default_factory=list)
    risk: int = 0
    issued_at: float = 0.0
    expires_at: float = 0.0
    max_uses: int = 1
    uses: int = 0
    approval_ref: str = ""
    revoked: bool = False
    #: Plan revision this lease was granted against. A plan that changed after
    #: approval must not spend a lease granted for the old one.
    plan_revision: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "lease_id": self.lease_id, "mission_id": self.mission_id,
            "session_id": self.session_id, "actor": self.actor,
            "capability": self.capability, "scope": list(self.scope),
            "risk": self.risk, "issued_at": self.issued_at,
            "expires_at": self.expires_at, "max_uses": self.max_uses,
            "uses": self.uses, "approval_ref": self.approval_ref,
            "revoked": self.revoked, "plan_revision": self.plan_revision,
        }

    @property
    def remaining_uses(self) -> int:
        return max(0, self.max_uses - self.uses)


class LeaseRefusal(Exception):
    """A lease could not authorize a use. The message says why."""


class LeaseService:
    """Issues, checks, consumes and revokes leases."""

    def __init__(self, db: Any) -> None:
        self.db = db

    # -- issuing ------------------------------------------------------------
    def issue(self, *, mission_id: str, session_id: str = "", actor: str = "user",
              capability: str = "", scope: Optional[List[str]] = None,
              risk: int = 0, ttl_seconds: float = 600.0, max_uses: int = 1,
              approval_ref: str = "", plan_revision: int = 0) -> Lease:
        """Grant a bounded lease. Every bound is required to be positive."""
        from ..core.models import new_id

        if not str(mission_id or "").strip():
            raise ValueError("a lease must name the mission it authorizes")
        ttl = float(ttl_seconds)
        if not (0 < ttl <= 24 * 3600):
            raise ValueError("a lease must expire within a day")
        uses = int(max_uses)
        if uses < 1:
            raise ValueError("a lease must permit at least one use")

        now = time.time()
        lease = Lease(
            lease_id=new_id("LEASE"),
            mission_id=str(mission_id),
            session_id=str(session_id),
            actor=str(actor or "user"),
            capability=str(capability or ""),
            scope=[str(item).rstrip("/") or "/" for item in (scope or [])],
            risk=int(risk),
            issued_at=now,
            expires_at=now + ttl,
            max_uses=uses,
            approval_ref=str(approval_ref or ""),
            plan_revision=int(plan_revision),
        )
        self.db.execute(
            """INSERT INTO permission_leases
                 (lease_id, mission_id, session_id, actor, capability, scope, risk,
                  issued_at, expires_at, max_uses, uses, approval_ref, revoked,
                  plan_revision)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""",
            (lease.lease_id, lease.mission_id, lease.session_id, lease.actor,
             lease.capability, json.dumps(lease.scope), lease.risk,
             lease.issued_at, lease.expires_at, lease.max_uses, lease.uses,
             lease.approval_ref, lease.plan_revision),
        )
        log.info("lease issued", extra_fields={
            "lease_id": lease.lease_id, "mission_id": lease.mission_id,
            "capability": lease.capability, "max_uses": lease.max_uses})
        return lease

    # -- reading ------------------------------------------------------------
    def get(self, lease_id: str) -> Optional[Lease]:
        rows = self.db.query(
            "SELECT * FROM permission_leases WHERE lease_id = ?", (lease_id,))
        return _from_row(rows[0]) if rows else None

    def active(self, limit: int = 50) -> List[Lease]:
        rows = self.db.query(
            """SELECT * FROM permission_leases
               WHERE revoked = 0 AND expires_at > ?
               ORDER BY issued_at DESC LIMIT ?""",
            (time.time(), max(1, min(int(limit), 200))))
        return [_from_row(r) for r in rows]

    # -- enforcement --------------------------------------------------------
    def check(self, lease_id: str, *, capability: str = "", resource: str = "",
              plan_revision: Optional[int] = None,
              session_id: str = "") -> Lease:
        """Is this lease usable for this use, right now? Raises `LeaseRefusal`.

        Re-read from the database on every call: a revocation written by another
        process must take effect immediately, not at the next lease refresh.
        """
        lease = self.get(lease_id)
        if lease is None:
            raise LeaseRefusal(f"no lease {lease_id!r}")
        if lease.revoked:
            raise LeaseRefusal("the lease has been revoked")
        if lease.expires_at <= time.time():
            raise LeaseRefusal("the lease has expired")
        if lease.remaining_uses <= 0:
            raise LeaseRefusal("the lease has no uses left")
        if lease.capability and capability and lease.capability != capability:
            raise LeaseRefusal(
                f"the lease authorizes {lease.capability!r}, not {capability!r}")
        if session_id and lease.session_id and lease.session_id != session_id:
            raise LeaseRefusal("the lease belongs to a different session")
        if plan_revision is not None and lease.plan_revision and \
                int(plan_revision) != lease.plan_revision:
            raise LeaseRefusal(
                "the plan changed after this lease was approved; it no longer "
                "covers the current plan")
        if not lease.scope:
            raise LeaseRefusal(
                "the lease declares no resource scope, so it authorizes nothing")
        if resource and not _in_scope(resource, lease.scope):
            raise LeaseRefusal(
                f"{resource!r} is outside the lease's scope "
                f"({', '.join(lease.scope)})")
        return lease

    def consume(self, lease_id: str, *, capability: str = "", resource: str = "",
                plan_revision: Optional[int] = None,
                session_id: str = "") -> Lease:
        """Check and spend one use atomically.

        The check and the increment run inside **one** transaction holding the
        database lock. Reading the lease and then spending it as two separate
        statements is what let two threads both see one remaining use and both
        take it: each read a consistent snapshot, and the conditional UPDATE
        matched twice because neither had committed yet.

        The UPDATE also re-states every bound in SQL, so even if a caller's
        in-memory lease were stale, the write itself cannot exceed the grant.
        """
        with self.db.cursor() as cur:
            rows = cur.execute(
                "SELECT * FROM permission_leases WHERE lease_id = ?",
                (lease_id,)).fetchall()
            if not rows:
                raise LeaseRefusal(f"no lease {lease_id!r}")
            lease = _from_row(rows[0])

            # The same rules as `check`, applied to the row read under the lock.
            if lease.revoked:
                raise LeaseRefusal("the lease has been revoked")
            if lease.expires_at <= time.time():
                raise LeaseRefusal("the lease has expired")
            if lease.remaining_uses <= 0:
                raise LeaseRefusal("the lease has no uses left")
            if lease.capability and capability and lease.capability != capability:
                raise LeaseRefusal(
                    f"the lease authorizes {lease.capability!r}, not {capability!r}")
            if session_id and lease.session_id and lease.session_id != session_id:
                raise LeaseRefusal("the lease belongs to a different session")
            if plan_revision is not None and lease.plan_revision and \
                    int(plan_revision) != lease.plan_revision:
                raise LeaseRefusal(
                    "the plan changed after this lease was approved; it no longer "
                    "covers the current plan")
            if not lease.scope:
                raise LeaseRefusal(
                    "the lease declares no resource scope, so it authorizes nothing")
            if resource and not _in_scope(resource, lease.scope):
                raise LeaseRefusal(
                    f"{resource!r} is outside the lease's scope "
                    f"({', '.join(lease.scope)})")

            cur.execute(
                """UPDATE permission_leases SET uses = uses + 1
                   WHERE lease_id = ? AND revoked = 0 AND expires_at > ?
                     AND uses < max_uses""",
                (lease_id, time.time()))
            if cur.rowcount != 1:
                raise LeaseRefusal("the lease could not be spent")

        spent = self.get(lease_id)
        if spent is None:
            raise LeaseRefusal(f"no lease {lease_id!r} after spending it")
        return spent

    def revoke(self, lease_id: str, *, reason: str = "", actor: str = "user") -> bool:
        self.db.execute(
            "UPDATE permission_leases SET revoked = 1, revoked_at = ?, "
            "revoked_reason = ?, revoked_by = ? WHERE lease_id = ?",
            (time.time(), str(reason or "")[:300], str(actor), lease_id))
        log.info("lease revoked", extra_fields={"lease_id": lease_id,
                                                "reason": reason[:80]})
        return True

    def revoke_mission(self, mission_id: str, *, reason: str = "") -> int:
        rows = self.db.query(
            "SELECT lease_id FROM permission_leases WHERE mission_id = ? AND revoked = 0",
            (mission_id,))
        for row in rows:
            self.revoke(row["lease_id"], reason=reason)
        return len(rows)


def _in_scope(resource: str, scope: List[str]) -> bool:
    """Literal prefix containment, with a path boundary.

    `/a/bc` must not satisfy a lease for `/a/b`, so the prefix has to end at a
    separator or be the whole resource.
    """
    candidate = str(resource).rstrip("/") or "/"
    for prefix in scope:
        if candidate == prefix:
            return True
        if candidate.startswith(prefix + "/"):
            return True
    return False


def _from_row(row: Any) -> Lease:
    try:
        scope = json.loads(row["scope"] or "[]")
    except (TypeError, ValueError):
        scope = []
    return Lease(
        lease_id=str(row["lease_id"]),
        mission_id=str(row["mission_id"]),
        session_id=str(row["session_id"] or ""),
        actor=str(row["actor"] or ""),
        capability=str(row["capability"] or ""),
        scope=[str(s) for s in scope],
        risk=int(row["risk"] or 0),
        issued_at=float(row["issued_at"] or 0.0),
        expires_at=float(row["expires_at"] or 0.0),
        max_uses=int(row["max_uses"] or 0),
        uses=int(row["uses"] or 0),
        approval_ref=str(row["approval_ref"] or ""),
        revoked=bool(row["revoked"]),
        plan_revision=int(row["plan_revision"] or 0),
    )