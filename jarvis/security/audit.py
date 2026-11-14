"""Audit log (section 46).

Append-only. Two sinks: the ``action_logs`` SQLite table (queryable by the
dashboard) and a JSONL file (tamper-evident, easy to ship elsewhere later).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..config import get_settings
from ..core.models import AuditRecord, new_id, utcnow
from ..db.database import get_db
from ..logging_setup import get_logger
from .privacy import redact, redact_value

log = get_logger("security.audit")

GENESIS = "0" * 64


def _canonical(record: AuditRecord) -> str:
    """Stable serialisation used for hashing (excludes the hash fields)."""
    payload = {
        "timestamp": record.timestamp.isoformat(),
        "user": record.user,
        "device": record.device,
        "session_id": record.session_id,
        "request_id": record.request_id,
        "intent": record.intent,
        "tool": record.tool,
        "target": record.target,
        "risk": record.risk,
        "confirmation": record.confirmation,
        "result": record.result,
        "detail": record.detail,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def chain_hash(prev_hash: str, record: AuditRecord) -> str:
    """Hash of (previous hash + this record)."""
    return hashlib.sha256((prev_hash + _canonical(record)).encode("utf-8")).hexdigest()


class AuditLog:
    """Records every meaningful action JARVIS takes."""

    def __init__(self) -> None:
        self.db = get_db()
        self.path = get_settings().audit_path

    def record(
        self,
        *,
        intent: str = "",
        tool: str = "",
        target: str = "",
        risk: str = "read",
        confirmation: bool = False,
        result: str = "",
        user: str = "user",
        device: str = "laptop_main",
        session_id: str = "",
        request_id: str = "",
        detail: Optional[Dict[str, Any]] = None,
    ) -> AuditRecord:
        settings = get_settings()

        # Redact before persisting. An audit log is long-lived and widely read;
        # a credential that lands here has effectively leaked.
        if settings.redact_audit:
            intent = redact(intent, settings)
            target = redact(target, settings)
            result = redact(result, settings)
            detail = redact_value(detail or {}, settings)

        record = AuditRecord(
            intent=intent,
            tool=tool,
            target=target,
            risk=risk,
            confirmation=confirmation,
            result=result,
            user=user,
            device=device,
            session_id=session_id,
            request_id=request_id,
            detail=detail or {},
        )

        # Link to the previous entry before persisting.
        record.prev_hash = self.last_hash()
        record.record_hash = chain_hash(record.prev_hash, record)

        # The id is produced once and kept on the record. Callers that need to
        # point at this evidence later (a mission condition naming the audit row
        # that supports it) had no way to learn it, so the id was effectively
        # write-only.
        log_id = new_id("LOG")
        record.log_id = log_id

        self.db.execute(
            """
            INSERT INTO action_logs
                (log_id, request_id, session_id, user_id, device, intent, tool,
                 target, risk, confirmation, result, detail, created_at,
                 prev_hash, record_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                log_id,
                record.request_id,
                record.session_id,
                record.user,
                record.device,
                record.intent,
                record.tool,
                record.target,
                record.risk,
                1 if record.confirmation else 0,
                record.result,
                json.dumps(record.detail),
                record.timestamp.isoformat(),
                record.prev_hash,
                record.record_hash,
            ),
        )
        self._append_file(record)
        return record

    def last_hash(self) -> str:
        """Hash of the most recent record, or the genesis hash."""
        rows = self.db.query(
            "SELECT record_hash FROM action_logs ORDER BY created_at DESC, rowid DESC LIMIT 1"
        )
        return (rows[0]["record_hash"] or GENESIS) if rows else GENESIS

    @staticmethod
    def _row_to_record(row: Any) -> AuditRecord:
        """Rebuild an AuditRecord from a database row (for hashing)."""
        return AuditRecord(
            timestamp=datetime.fromisoformat(row["created_at"]),
            user=row["user_id"] or "user",
            device=row["device"] or "",
            session_id=row["session_id"] or "",
            request_id=row["request_id"] or "",
            intent=row["intent"] or "",
            tool=row["tool"] or "",
            target=row["target"] or "",
            risk=row["risk"] or "",
            confirmation=bool(row["confirmation"]),
            result=row["result"] or "",
            detail=json.loads(row["detail"] or "{}"),
        )

    def seal_chain(self, force: bool = False) -> int:
        """Give every record a hash, in order.

        Records written before the chain existed have no hash, which would make
        verification report a break on the very first row. This seals them once.

        Honest limitation: sealing happens *now*, so it protects against future
        edits, not edits made before sealing. Data that was never hashed cannot
        be retroactively protected.

        `force=True` re-seals a ledger that is already hashed, which is what repairing
        a row requires -- a deletion or an edit invalidates that row's hash and its
        successor's `prev_hash`, and the chain only verifies again from the change on.
        It is refused as a quiet default on purpose, and when it does run it writes an
        event row, because a re-sealed chain proves only that nothing changed *after*
        the re-seal. A reader who cannot see that date would read the ledger's green
        light as a longer guarantee than it is.
        """
        unhashed = self.db.query(
            "SELECT COUNT(*) AS n FROM action_logs WHERE record_hash IS NULL OR record_hash = ''"
        )
        if not unhashed or not unhashed[0]["n"]:
            if not force:
                return 0

        rows = self.db.query(
            "SELECT * FROM action_logs ORDER BY created_at ASC, rowid ASC"
        )
        previous = GENESIS
        for row in rows:
            record = self._row_to_record(row)
            digest = chain_hash(previous, record)
            self.db.execute(
                "UPDATE action_logs SET prev_hash = ?, record_hash = ? WHERE log_id = ?",
                (previous, digest, row["log_id"]),
            )
            previous = digest
        resealed = len(rows)

        if force:
            self.record(
                intent=f"audit chain re-sealed: {len(rows)} records re-hashed on "
                       f"{utcnow().isoformat()}",
                tool="seal_chain", result="success",
                detail={"forced": True, "records": len(rows),
                        "meaning": "integrity is proved from this moment forward; edits before "
                                   "it cannot be detected by the chain any longer"},
            )

        log.info("audit chain re-sealed" if force else "audit chain sealed",
                 extra_fields={"records": resealed, "forced": bool(force)})
        return resealed

    def result_value_counts(self) -> Dict[str, int]:
        """Every distinct value in the `result` column, counted over the whole ledger.

        `/metrics` buckets the newest 500 rows for its own summary; this asks the table instead,
        because the two malformed rows this was written for (an early build stored a *response
        message* in a status column) sit older than that window and were therefore invisible in
        every published count. A blind spot that is not reported is a blind spot that is trusted.
        """
        rows = self.db.query("SELECT result, COUNT(*) AS n FROM action_logs GROUP BY result")
        return {(r["result"] or ""): r["n"] for r in rows}

    def verify_chain(self, limit: Optional[int] = None) -> Dict[str, Any]:
        """Re-walk the audit chain, reporting every break rather than stopping at one.

        Detects deletion, reordering and edits of past records. An attacker who
        can write to the database could recompute the whole chain, so this
        proves integrity against casual tampering rather than a determined
        local attacker -- pair it with off-box shipping for that.

        The walk used to ``return`` at the first bad record. One poisoned row was
        therefore enough to void verification of everything after it: a leftover
        test fixture dated 2020-01-01 sorted to the head of the chain and made
        ``checked`` 0 out of 461, so the ledger read as "cannot be verified"
        when in fact 460 records verified cleanly. Each break is now recorded and
        the walk re-syncs from the stored hash, so ``checked`` and ``breaks``
        describe what was actually proved. ``reason``/``at`` still report the
        first break, which is what callers assert on.
        """
        total_rows = self.db.query("SELECT COUNT(*) AS n FROM action_logs")[0]["n"]
        if limit is None:
            rows = self.db.query("SELECT * FROM action_logs ORDER BY created_at ASC, rowid ASC")
        else:
            rows = self.db.query(
                "SELECT * FROM action_logs ORDER BY created_at ASC, rowid ASC LIMIT ?", (limit,)
            )
        previous = GENESIS
        checked = 0
        breaks: list[Dict[str, Any]] = []

        for row in rows:
            record = self._row_to_record(row)
            stored_prev = row["prev_hash"] or ""
            stored_hash = row["record_hash"] or ""

            if not stored_hash:
                reason = "record has no hash (written before the chain existed)"
            elif stored_prev != previous:
                reason = "previous-hash mismatch (a record was removed or reordered)"
            elif chain_hash(stored_prev, record) != stored_hash:
                reason = "record hash mismatch (a record was edited)"
            else:
                reason = ""

            if reason:
                breaks.append({"reason": reason, "at": row["created_at"], "log_id": row["log_id"],
                              "position": checked + len(breaks) + 1})
                # Re-sync so a single break does not cascade into every later row.
                previous = stored_hash or GENESIS
                continue

            previous = stored_hash
            checked += 1

        # `total` is what this walk covered; `verified_of` is the ledger. They were the same
        # number while the default limit reached every row, and they are not any more: 525 rows
        # on this machine, a 500-row window, and the newest 25 -- the ones a week-later edit
        # touches -- were never read. Reporting only the first of the two let the panel say
        # "intact" about a part of the chain it had not looked at.
        covered = {
            "checked": checked,
            "total": len(rows),
            "verified_of": total_rows,
            "unverified": total_rows - len(rows),
        }
        if breaks:
            return dict(covered, ok=False, breaks=len(breaks), break_details=breaks,
                        reason=breaks[0]["reason"], at=breaks[0]["at"],
                        first_bad_log_id=breaks[0]["log_id"],
                        first_bad_position=breaks[0]["position"])

        return dict(covered, ok=True, breaks=0, break_details=[],
                    reason="", head=previous)

    def _append_file(self, record: AuditRecord) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(record.model_dump_json() + "\n")
        except OSError as exc:  # pragma: no cover - disk issues only
            log.info("audit file write failed", extra_fields={"error": str(exc)})

    def recent(self, limit: int = 50) -> List[Dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM action_logs ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        out: List[Dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["detail"] = json.loads(item.get("detail") or "{}")
            except json.JSONDecodeError:
                item["detail"] = {}
            out.append(item)
        return out

    def failures(self, limit: int = 20) -> List[Dict[str, Any]]:
        rows = self.db.query(
            """
            SELECT * FROM action_logs
            WHERE result NOT IN ('success', 'ok', 'confirmed')
            ORDER BY created_at DESC LIMIT ?
            """,
            (limit,),
        )
        return [dict(r) for r in rows]
