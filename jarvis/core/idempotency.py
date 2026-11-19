"""Durable idempotency records for inbound commands.

A retrying client -- a phone on a flaky network, a proxy, a user pressing the
button twice -- sends the same request again. Without a record of the first
attempt the backend cannot tell a retry from new work, so the side effect runs
twice. This module is the *store* that closes that gap; the callers (the HTTP
route, the mission runner) decide what a repeat means for them.

Design notes that are load-bearing:

* **A key is scoped.** ``(scope, key)`` is unique, so one actor/profile can never
  read or overwrite another's record by guessing a key. ``scope`` is the
  actor identity the caller binds the request to, not a free-form label.
* **A key is bound to one request.** The record carries the SHA-256 fingerprint
  of the canonical request content. Reusing a key for *different* content is a
  client bug and is reported as ``conflict`` -- never silently treated as a
  repeat, because that would suppress a different action under a stale id.
* **Claiming is atomic.** Two simultaneous claims of an unused key settle on the
  unique index: ``INSERT ... ON CONFLICT DO NOTHING`` and the insert's rowcount
  decide the winner, so exactly one caller sees ``new``. A check-then-insert
  would let both observe "absent" and both proceed.
* **The table is additive.** It is created with ``CREATE TABLE IF NOT EXISTS``
  under its own name; no existing table or column is modified, so the migration
  is safe on a populated database and reversible by dropping the table.

The module holds no in-memory state: a record survives a restart exactly as it
was written.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, Optional

from ..logging_setup import get_logger

log = get_logger("core.idempotency")

#: Every state a claimed request can occupy. ``received`` is where a fresh claim
#: starts; the rest are written by the caller as the request moves through the
#: pipeline. Anything outside this set is rejected by ``update_state`` rather
#: than stored, so a typo cannot create an unobservable state.
STATES: frozenset = frozenset({
    "received",
    "claimed",
    "planning",
    "awaiting_approval",
    "executing",
    "verifying",
    "completed",
    "failed",
    "unknown",
})

#: The three outcomes of ``claim``.
NEW = "new"
EXISTING = "existing"
CONFLICT = "conflict"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS idempotency_records (
    scope       TEXT NOT NULL,
    idem_key    TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'received',
    mission_id  TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (scope, idem_key)
);
"""


@dataclass(frozen=True)
class Record:
    """One stored idempotency binding."""

    scope: str
    key: str
    fingerprint: str
    state: str
    mission_id: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Claim:
    """The result of ``claim``.

    ``status`` is ``new`` (the caller owns the request and may proceed),
    ``existing`` (the same scope+key+fingerprint was already recorded -- the
    caller should reconcile with ``record`` instead of executing again), or
    ``conflict`` (the key was reused for different content -- refuse). ``record``
    is the stored row in all three cases; for ``conflict`` it holds the
    *original* fingerprint, not the one offered.
    """

    status: str
    record: Record

    @property
    def is_new(self) -> bool:
        return self.status == NEW

    @property
    def is_existing(self) -> bool:
        return self.status == EXISTING

    @property
    def is_conflict(self) -> bool:
        return self.status == CONFLICT


def fingerprint_request(payload: Any) -> str:
    """SHA-256 of the canonical JSON form of ``payload``.

    Canonical means sorted keys and no insignificant whitespace, so two
    semantically identical request bodies hash to the same value regardless of
    how their dictionaries were built. Non-JSON values fall back to ``str`` so a
    fingerprint is always computable; the result is a 64-character hex digest.
    """
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _require_text(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _row_to_record(row: Any) -> Record:
    return Record(
        scope=row["scope"],
        key=row["idem_key"],
        fingerprint=row["fingerprint"],
        state=row["state"],
        mission_id=row["mission_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class IdempotencyStore:
    """SQLite-backed deduplication store, bound to one ``Database`` handle."""

    def __init__(self, db: Any) -> None:
        self.db = db
        # Additive: a new table under its own name. ``IF NOT EXISTS`` makes this
        # idempotent across restarts and safe on an existing, populated file.
        self.db.execute(_SCHEMA)

    def claim(self, scope: str, key: str, fingerprint: str) -> Claim:
        """Claim ``(scope, key)`` for ``fingerprint``.

        The insert and the follow-up read happen in one transaction. The unique
        primary key on ``(scope, idem_key)`` means SQLite itself serialises
        concurrent claims: exactly one insert takes effect and reports a
        rowcount of 1. Every other claim reads the winner's row and compares
        fingerprints, so the outcome is ``new`` for exactly one caller and
        ``existing`` (or ``conflict``) for the rest.
        """
        scope = _require_text("scope", scope)
        key = _require_text("key", key)
        fingerprint = _require_text("fingerprint", fingerprint)

        with self.db.cursor() as cur:
            cur.execute(
                """
                INSERT INTO idempotency_records (scope, idem_key, fingerprint, state)
                VALUES (?, ?, ?, 'received')
                ON CONFLICT(scope, idem_key) DO NOTHING
                """,
                (scope, key, fingerprint),
            )
            inserted = cur.rowcount == 1
            row = cur.execute(
                "SELECT * FROM idempotency_records WHERE scope = ? AND idem_key = ?",
                (scope, key),
            ).fetchone()

        if row is None:  # only possible if another actor deleted the row
            raise RuntimeError("idempotency record vanished after claim")

        record = _row_to_record(row)
        if inserted:
            return Claim(NEW, record)
        if record.fingerprint != fingerprint:
            log.warning(
                "idempotency key reused for different content",
                extra_fields={"scope": scope, "key": key},
            )
            return Claim(CONFLICT, record)
        return Claim(EXISTING, record)

    def update_state(
        self,
        scope: str,
        key: str,
        state: str,
        mission_id: Optional[str] = None,
    ) -> Record:
        """Move a record to ``state``; optionally set ``mission_id``.

        ``mission_id=None`` leaves the stored value untouched. An unknown state
        name raises ``ValueError``; an unknown record raises ``LookupError``,
        since silently creating a row here would hide a broken claim flow.
        """
        scope = _require_text("scope", scope)
        key = _require_text("key", key)
        if state not in STATES:
            raise ValueError(
                f"invalid state {state!r}; expected one of {sorted(STATES)}"
            )
        if mission_id is not None and not isinstance(mission_id, str):
            raise ValueError("mission_id must be a string or None")

        with self.db.cursor() as cur:
            cur.execute(
                """
                UPDATE idempotency_records
                   SET state = ?,
                       mission_id = COALESCE(?, mission_id),
                       updated_at = datetime('now')
                 WHERE scope = ? AND idem_key = ?
                """,
                (state, mission_id, scope, key),
            )
            if cur.rowcount == 0:
                raise LookupError(
                    f"no idempotency record for scope={scope!r} key={key!r}"
                )
            row = cur.execute(
                "SELECT * FROM idempotency_records WHERE scope = ? AND idem_key = ?",
                (scope, key),
            ).fetchone()

        return _row_to_record(row)

    def get(self, scope: str, key: str) -> Optional[Record]:
        """Return the record for ``(scope, key)``, or ``None``.

        Scoped by construction: the query carries ``scope`` in its ``WHERE``
        clause, so a key belonging to another scope is indistinguishable from an
        unknown key.
        """
        scope = _require_text("scope", scope)
        key = _require_text("key", key)
        rows = self.db.query(
            "SELECT * FROM idempotency_records WHERE scope = ? AND idem_key = ?",
            (scope, key),
        )
        return _row_to_record(rows[0]) if rows else None
