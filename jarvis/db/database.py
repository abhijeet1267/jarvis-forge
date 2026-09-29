"""SQLite persistence layer.

Implements the initial schema from section 38 of the plan. SQLite is the
starting point; the access layer is deliberately thin so a later move to
PostgreSQL + pgvector only requires replacing this module.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from ..config import get_settings
from ..logging_setup import get_logger

log = get_logger("db")

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
    user_id      TEXT PRIMARY KEY,
    display_name TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS devices (
    device_id    TEXT PRIMARY KEY,
    type         TEXT NOT NULL,
    owner        TEXT NOT NULL DEFAULT 'user',
    platform     TEXT,
    trust_level  TEXT DEFAULT 'medium',
    capabilities TEXT DEFAULT '[]',   -- JSON array
    online       INTEGER DEFAULT 1,
    metadata     TEXT DEFAULT '{}',   -- JSON object
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL DEFAULT 'user',
    device_id    TEXT,
    source       TEXT,
    started_at   TEXT NOT NULL DEFAULT (datetime('now')),
    ended_at     TEXT
);

CREATE TABLE IF NOT EXISTS conversations (
    conversation_id TEXT PRIMARY KEY,
    session_id      TEXT,
    title           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS messages (
    message_id      TEXT PRIMARY KEY,
    session_id      TEXT,
    role            TEXT NOT NULL,       -- user | assistant | system
    content         TEXT NOT NULL,
    intent          TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tasks (
    job_id       TEXT PRIMARY KEY,
    goal         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'running',
    requires_confirmation TEXT DEFAULT '[]',
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS task_steps (
    step_id      TEXT PRIMARY KEY,
    job_id       TEXT NOT NULL REFERENCES tasks(job_id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    detail       TEXT DEFAULT '',
    seq          INTEGER DEFAULT 0,
    confirmation_id TEXT DEFAULT '',
    params       TEXT DEFAULT '{}',
    device       TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS tools (
    name         TEXT PRIMARY KEY,
    agent        TEXT,
    risk         INTEGER NOT NULL DEFAULT 0,
    requires_confirmation INTEGER DEFAULT 0,
    reversible   INTEGER DEFAULT 1,
    description  TEXT,
    registered_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS permissions (
    permission_id TEXT PRIMARY KEY,
    subject       TEXT NOT NULL,      -- user or device
    tool          TEXT NOT NULL,
    device        TEXT,
    granted       INTEGER DEFAULT 1,
    granted_at    TEXT NOT NULL DEFAULT (datetime('now')),
    revoked_at    TEXT
);

CREATE TABLE IF NOT EXISTS confirmations (
    confirmation_id TEXT PRIMARY KEY,
    request_id      TEXT,
    session_id      TEXT,
    tool            TEXT NOT NULL,
    params          TEXT DEFAULT '{}',
    device          TEXT,
    risk            INTEGER DEFAULT 0,
    preview         TEXT,
    state           TEXT NOT NULL DEFAULT 'pending',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at     TEXT
);

CREATE TABLE IF NOT EXISTS action_logs (
    log_id       TEXT PRIMARY KEY,
    request_id   TEXT,
    session_id   TEXT,
    user_id      TEXT,
    device       TEXT,
    intent       TEXT,
    tool         TEXT,
    target       TEXT,
    risk         TEXT,
    confirmation INTEGER DEFAULT 0,
    result       TEXT,
    detail       TEXT DEFAULT '{}',
    duration_ms  INTEGER DEFAULT 0,
    prev_hash    TEXT DEFAULT '',
    record_hash  TEXT DEFAULT '',
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS device_pairings (
    pairing_id   TEXT PRIMARY KEY,
    device_id    TEXT NOT NULL,
    device_name  TEXT,
    public_key   TEXT,
    fingerprint  TEXT,
    state        TEXT NOT NULL DEFAULT 'pending',  -- pending|approved|denied|revoked
    requested_at TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at  TEXT,
    resolved_by  TEXT
);

CREATE TABLE IF NOT EXISTS transfers (
    transfer_id  TEXT PRIMARY KEY,
    job_id       TEXT,
    direction    TEXT NOT NULL,                    -- mac_to_device | device_to_mac
    device_id    TEXT NOT NULL,
    source_path  TEXT,
    dest_path    TEXT,
    filename     TEXT,
    size_bytes   INTEGER DEFAULT 0,
    bytes_sent   INTEGER DEFAULT 0,
    sha256       TEXT,
    state        TEXT NOT NULL DEFAULT 'pending',
    error        TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS transfer_events (
    event_id     TEXT PRIMARY KEY,
    transfer_id  TEXT NOT NULL,
    state        TEXT NOT NULL,
    detail       TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS contacts (
    contact_id  TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    address     TEXT NOT NULL,
    aliases     TEXT DEFAULT '[]',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS email_drafts (
    draft_id     TEXT PRIMARY KEY,
    to_addresses TEXT DEFAULT '[]',
    cc_addresses TEXT DEFAULT '[]',
    subject      TEXT DEFAULT '',
    body         TEXT DEFAULT '',
    attachments  TEXT DEFAULT '[]',
    state        TEXT DEFAULT 'draft',   -- draft | sent | discarded
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS design_assets (
    asset_id   TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    kind       TEXT NOT NULL,   -- diagram | ui_concept | documentation | presentation
    format     TEXT NOT NULL,   -- svg | html | markdown
    filename   TEXT NOT NULL,
    created_by TEXT DEFAULT 'local',
    project_id TEXT,
    summary    TEXT DEFAULT '',
    added_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS papers (
    paper_id       TEXT PRIMARY KEY,
    title          TEXT NOT NULL,
    authors        TEXT DEFAULT '[]',
    year           INTEGER,
    venue          TEXT DEFAULT '',
    url            TEXT DEFAULT '',
    doi            TEXT DEFAULT '',
    arxiv_id       TEXT DEFAULT '',
    abstract       TEXT DEFAULT '',
    source_backend TEXT NOT NULL,   -- arxiv | local | user
    verified       INTEGER DEFAULT 0,
    project_id     TEXT,
    added_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS research_notes (
    note_id    TEXT PRIMARY KEY,
    project_id TEXT,
    paper_id   TEXT,
    topic      TEXT DEFAULT '',
    claim      TEXT NOT NULL,
    claim_type TEXT NOT NULL,  -- source_fact | interpretation | hypothesis | unresolved
    evidence   TEXT DEFAULT '',
    added_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS memory_categories (
    name       TEXT PRIMARY KEY,
    enabled    INTEGER DEFAULT 1,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS calendar_events (
    uid         TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    start_at    TEXT NOT NULL,
    end_at      TEXT NOT NULL,
    location    TEXT DEFAULT '',
    description TEXT DEFAULT '',
    calendar    TEXT DEFAULT 'default',
    all_day     INTEGER DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS schedule_runs (
    run_id       TEXT PRIMARY KEY,
    schedule_id  TEXT NOT NULL,
    started_at   TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at  TEXT,
    outcome      TEXT,          -- executed | awaiting_approval | blocked | failed
    detail       TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS failures (
    failure_id   TEXT PRIMARY KEY,
    tool         TEXT,
    params       TEXT DEFAULT '{}',
    device       TEXT,
    error        TEXT,
    recovered    INTEGER DEFAULT 0,
    strategy     TEXT,
    attempts     INTEGER DEFAULT 0,
    note         TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS memories (
    id           TEXT PRIMARY KEY,
    content      TEXT NOT NULL,
    source       TEXT,
    scope        TEXT NOT NULL DEFAULT 'session',
    privacy_level TEXT DEFAULT 'internal',
    project_id   TEXT,
    confidence   REAL DEFAULT 1.0,
    tags         TEXT DEFAULT '[]',
    embedding    TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS projects (
    project_id   TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    description  TEXT DEFAULT '',
    status       TEXT DEFAULT 'active',
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS graph_nodes (
    node_id      TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL,
    kind         TEXT NOT NULL,
    label        TEXT NOT NULL,
    data         TEXT DEFAULT '{}',
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS graph_edges (
    edge_id      TEXT PRIMARY KEY,
    source_id    TEXT NOT NULL,
    target_id    TEXT NOT NULL,
    relation     TEXT DEFAULT 'related_to',
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS files (
    file_id      TEXT PRIMARY KEY,
    path         TEXT NOT NULL,
    device       TEXT,
    project_id   TEXT,
    size         INTEGER,
    sha256       TEXT,
    indexed_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS experiments (
    experiment_id TEXT PRIMARY KEY,
    project_id    TEXT,
    name          TEXT NOT NULL,
    status        TEXT DEFAULT 'pending',
    result        TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sensor_readings (
    reading_id    TEXT PRIMARY KEY,
    device        TEXT,
    sensor        TEXT NOT NULL,
    value         REAL,
    unit          TEXT,
    taken_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS integrations (
    integration_id TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    kind           TEXT,
    status         TEXT DEFAULT 'disconnected',
    metadata       TEXT DEFAULT '{}',
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS scheduled_jobs (
    schedule_id  TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    rrule        TEXT,
    prompt       TEXT,
    status       TEXT DEFAULT 'active',
    last_run     TEXT,
    next_run     TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_action_logs_created ON action_logs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope);
CREATE INDEX IF NOT EXISTS idx_memories_project ON memories(project_id);
CREATE INDEX IF NOT EXISTS idx_nodes_project ON graph_nodes(project_id);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
"""


class Database:
    """Thread-safe SQLite wrapper.

    A single connection is shared behind a lock, which is sufficient for the
    single-user, low-concurrency workload of JARVIS Forge v0.1.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        settings = get_settings()
        self.path = Path(path) if path else settings.db_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self.init_schema()

    def init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.commit()
        log.info("database ready", extra_fields={"path": str(self.path)})

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created.

        ``CREATE TABLE IF NOT EXISTS`` never alters an existing table, so new
        columns need an explicit, idempotent ALTER.
        """
        additions = {
            "action_logs": {
                "prev_hash": "TEXT DEFAULT ''",
                "record_hash": "TEXT DEFAULT ''",
            },
            # v0.6 device identity. Existing rows default to "unpaired" with no
            # key material, so pre-v0.6 devices keep behaving exactly as before.
            "devices": {
                "fingerprint": "TEXT DEFAULT ''",
                "auth_state": "TEXT DEFAULT 'unpaired'",
                "paired_at": "TEXT",
                "last_seen": "TEXT",
                "key_ref": "TEXT DEFAULT ''",
            },
            # v0.8 scheduler. The table existed but was never used; these
            # columns carry the action, its permission ceiling and its history.
            # Phase 14: a workflow step that stopped waiting for an approval has
            # to be findable from the approval, which arrives in a later,
            # unrelated request. Without this link the job stayed `running`.
            # Phase 27: a step also has to survive being *re-run*. The row kept
            # the tool's name and none of what it was asked to do, so nothing
            # could re-issue it; `name` already holds the tool, so only the
            # missing two are added. Pre-Phase-27 rows keep the defaults and are
            # refused by the gate rather than guessed at.
            "task_steps": {
                "confirmation_id": "TEXT DEFAULT ''",
                "params": "TEXT DEFAULT '{}'",
                "device": "TEXT DEFAULT ''",
            },
            # v0.9 memory lifecycle. Existing rows default to a general,
            # medium-importance, non-expiring memory -- unchanged behaviour.
            "memories": {
                "memory_type": "TEXT DEFAULT 'general'",
                "importance": "REAL DEFAULT 0.5",
                "expires_at": "TEXT",
                "relationships": "TEXT DEFAULT '{}'",
            },
            "scheduled_jobs": {
                "owner": "TEXT DEFAULT 'user'",
                "action": "TEXT DEFAULT ''",
                "params": "TEXT DEFAULT '{}'",
                "max_risk": "INTEGER DEFAULT 1",
                "run_count": "INTEGER DEFAULT 0",
                "failure_count": "INTEGER DEFAULT 0",
                "last_error": "TEXT DEFAULT ''",
                "last_result": "TEXT DEFAULT ''",
                "requires_confirmation": "INTEGER DEFAULT 0",
            },
        }
        for table, columns in additions.items():
            try:
                existing = {
                    row["name"]
                    for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
                }
            except sqlite3.Error:
                continue
            for column, definition in columns.items():
                if column not in existing:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                    log.info(
                        "schema migrated",
                        extra_fields={"table": table, "column": column},
                    )

    @contextmanager
    def cursor(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            finally:
                cur.close()

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            rows = cur.fetchall()
            cur.close()
            return rows

    def execute(self, sql: str, params: tuple = ()) -> None:
        with self.cursor() as cur:
            cur.execute(sql, params)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


_DB: Optional[Database] = None


def get_db() -> Database:
    """Return the process-wide database singleton."""
    global _DB
    if _DB is None:
        _DB = Database()
    return _DB
