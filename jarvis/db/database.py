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

-- One row per conversation: the email a follow-up ("read that one", "summarize
-- the second one") points at. Keyed by session_id -- the same key the messages,
-- confirmations and audit rows use -- so a selection made in one conversation
-- cannot be reached from another. Only identifiers and display labels live
-- here; the body is always fetched through the message-read path.
CREATE TABLE IF NOT EXISTS email_selections (
    session_id  TEXT PRIMARY KEY,
    uid         TEXT NOT NULL DEFAULT '',
    subject     TEXT NOT NULL DEFAULT '',
    sender      TEXT NOT NULL DEFAULT '',
    provider    TEXT NOT NULL DEFAULT '',
    candidates  TEXT NOT NULL DEFAULT '[]',  -- JSON array of the last search's rows
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- A user's own to-do item (the daily planner), distinct from `tasks`/`task_steps`
-- which are *workflow* runs with a goal and steps. This is the thing a person
-- writes down: "finish the AMR report by Friday", with a due date, priority,
-- estimate and an explicit status. One row per task; persisted across restarts.
CREATE TABLE IF NOT EXISTS vantage_tasks (
    task_ref     TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    detail       TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'open',   -- open | done | cancelled
    priority     TEXT NOT NULL DEFAULT 'normal', -- low | normal | high | urgent
    due_at       TEXT,                           -- ISO-8601 local, or NULL
    start_at     TEXT,                           -- ISO-8601 local, or NULL
    estimate_minutes INTEGER NOT NULL DEFAULT 0,
    -- JSON array of task_refs this task waits on. A task with unfinished
    -- dependencies is *blocked*, reported as such rather than listed as doable.
    depends_on   TEXT NOT NULL DEFAULT '[]',
    source       TEXT NOT NULL DEFAULT 'user',   -- user | calendar | workflow
    project      TEXT NOT NULL DEFAULT '',
    -- When it was completed, and the evidence the person gave (free text).
    done_at      TEXT,
    evidence     TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- A drafted social/professional post (LinkedIn and similar). Vantage can write,
-- edit, schedule and remind; it does NOT publish -- there is no approved posting
-- integration, and a draft that looks published would be a lie.
CREATE TABLE IF NOT EXISTS post_drafts (
    draft_id     TEXT PRIMARY KEY,
    channel      TEXT NOT NULL DEFAULT 'linkedin',
    body         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'draft',  -- draft | scheduled | published
    scheduled_for TEXT,                          -- ISO local, or NULL
    contact      TEXT NOT NULL DEFAULT '',       -- optional audience/recipient note
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- What was said in a meeting. Vantage did not attend and has no audio, so this
-- is the person's own account, kept so it can be revisited and so the action
-- items can be traced back to the meeting they came from.
CREATE TABLE IF NOT EXISTS meeting_notes (
    note_id      TEXT PRIMARY KEY,
    event_uid    TEXT NOT NULL,
    meeting      TEXT NOT NULL DEFAULT '',
    notes        TEXT NOT NULL DEFAULT '',
    action_items TEXT NOT NULL DEFAULT '[]',   -- JSON array of the agreed items
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_meeting_notes_event ON meeting_notes(event_uid);

-- Which due tasks have already produced a notification. Without this the
-- notifier would fire again on every tick for the same overdue task, which
-- trains a person to ignore the banner -- the opposite of a reminder.
CREATE TABLE IF NOT EXISTS notified_tasks (
    task_ref     TEXT PRIMARY KEY,
    notified_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_vantage_tasks_status ON vantage_tasks(status);
CREATE INDEX IF NOT EXISTS idx_vantage_tasks_due ON vantage_tasks(due_at);

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

-- Mission contracts (P1). One row per mission; its conditions and steps are
-- JSON documents rather than child tables because the whole contract is read
-- and written as a unit. `version` is the optimistic-concurrency guard: an
-- update that names a stale version changes nothing, so two writers cannot
-- silently interleave a state machine.
CREATE TABLE IF NOT EXISTS missions (
    mission_id         TEXT PRIMARY KEY,
    request            TEXT NOT NULL,
    outcome            TEXT NOT NULL DEFAULT '',
    state              TEXT NOT NULL,
    scope              TEXT NOT NULL DEFAULT '{}',
    conditions         TEXT NOT NULL DEFAULT '[]',
    steps              TEXT NOT NULL DEFAULT '[]',
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    evidence           TEXT NOT NULL DEFAULT '[]',
    reason             TEXT NOT NULL DEFAULT '',
    recovery_attempts  INTEGER NOT NULL DEFAULT 0,
    version            INTEGER NOT NULL DEFAULT 1,
    last_actor         TEXT NOT NULL DEFAULT 'system'
);
CREATE INDEX IF NOT EXISTS idx_missions_state ON missions(state, updated_at DESC);

-- Permission leases (D1). A standing grant in `permissions` answers "may this
-- subject use this tool at all"; a lease answers the narrower question a mission
-- needs -- this session, this mission, this capability, this resource prefix,
-- now, this many times. Expiry is an absolute time so a caller's clock skew
-- cannot extend it, and `uses` is spent with a conditional UPDATE.
CREATE TABLE IF NOT EXISTS permission_leases (
    lease_id       TEXT PRIMARY KEY,
    mission_id     TEXT NOT NULL,
    session_id     TEXT NOT NULL DEFAULT '',
    actor          TEXT NOT NULL DEFAULT 'user',
    capability     TEXT NOT NULL DEFAULT '',
    scope          TEXT NOT NULL DEFAULT '[]',
    risk           INTEGER NOT NULL DEFAULT 0,
    issued_at      REAL NOT NULL,
    expires_at     REAL NOT NULL,
    max_uses       INTEGER NOT NULL DEFAULT 1,
    uses           INTEGER NOT NULL DEFAULT 0,
    approval_ref   TEXT NOT NULL DEFAULT '',
    revoked        INTEGER NOT NULL DEFAULT 0,
    revoked_at     REAL,
    revoked_reason TEXT NOT NULL DEFAULT '',
    revoked_by     TEXT NOT NULL DEFAULT '',
    plan_revision  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_leases_mission ON permission_leases(mission_id, revoked);

-- Saved workspaces (V10). A named set of things a person opens together: a
-- project folder, applications, files and URLs. The members are one JSON
-- document because a workspace is saved and edited as a whole. `launch_count`
-- and `last_launched_at` record that a launch was requested, not that it
-- succeeded -- success is observed per member and reported by the caller.
CREATE TABLE IF NOT EXISTS workspaces (
    workspace_id     TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    description      TEXT NOT NULL DEFAULT '',
    project_dir      TEXT NOT NULL DEFAULT '',
    apps             TEXT NOT NULL DEFAULT '[]',
    files            TEXT NOT NULL DEFAULT '[]',
    urls             TEXT NOT NULL DEFAULT '[]',
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    launch_count     INTEGER NOT NULL DEFAULT 0,
    last_launched_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_workspaces_name ON workspaces(name COLLATE NOCASE);
"""


class Database:
    """Thread-safe SQLite wrapper.

    A single connection is shared behind a lock, which is sufficient for the
    single-user, low-concurrency workload of Vantage v0.1.
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
            # V12 phase 3. `version` is an optimistic-concurrency counter that
            # moves on every write, including marking a step done, so it cannot
            # answer "is this still the plan that was approved". `plan_revision`
            # does: it changes only when the plan itself is steered, and a lease
            # or an intent bound to an older revision stops covering the plan.
            # `intent` holds the IntentScope recorded for the request; empty
            # means none was recorded and intent-based checks do not run.
            "missions": {
                "plan_revision": "INTEGER DEFAULT 0",
                "intent": "TEXT DEFAULT ''",
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
    
    # Create AI chat tables (Phase 73)
    _DB.execute("""CREATE TABLE IF NOT EXISTS ai_chats (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")
    
    _DB.execute("""CREATE TABLE IF NOT EXISTS ai_messages (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('user', 'assistant', 'error')),
        content TEXT NOT NULL,
        provider_meta TEXT DEFAULT '{}',
        timestamp TEXT NOT NULL,
        FOREIGN KEY (conversation_id) REFERENCES ai_chats(id)
    )""")
    
    _DB.execute("""CREATE INDEX IF NOT EXISTS idx_ai_messages_conversation 
                   ON ai_messages(conversation_id, timestamp DESC)""")
    
    # AI attachments table (Phase 74 - multimodal support)
    _DB.execute("""CREATE TABLE IF NOT EXISTS ai_attachments (
        id TEXT PRIMARY KEY,
        message_id TEXT NOT NULL,
        file_type TEXT NOT NULL CHECK(file_type IN ('image/png', 'image/jpeg', 'image/gif', 'image/webp', 'application/pdf', 'audio/wav', 'audio/mp3', 'audio/mpeg', 'text/plain')),
        file_size INTEGER NOT NULL,
        filename TEXT NOT NULL,
        storage_path TEXT NOT NULL,
        upload_status TEXT NOT NULL DEFAULT 'pending',
        processing_status TEXT NOT NULL DEFAULT 'pending',
        metadata TEXT DEFAULT '{}',
        created_at TEXT NOT NULL,
        FOREIGN KEY (message_id) REFERENCES ai_messages(id)
    )""")
    
    # File uploads table (shared across features)
    _DB.execute("""CREATE TABLE IF NOT EXISTS uploaded_files (
        id TEXT PRIMARY KEY,
        path TEXT NOT NULL,
        size_bytes INTEGER DEFAULT 0,
        sha256 TEXT,
        mime_type TEXT,
        device TEXT,
        project_id TEXT,
        indexed_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""")
    
    _DB.execute("""CREATE INDEX IF NOT EXISTS idx_ai_attachments_message 
                   ON ai_attachments(message_id, created_at ASC)""")
    _DB.execute("""CREATE INDEX IF NOT EXISTS idx_uploaded_files_sha256 
                   ON uploaded_files(sha256)""")
    
    # ============================================================================
    # AGENT CORE DATABASE SCHEMA (Phase 75 - JARVIS Agent)
    # ============================================================================
    
    _DB.execute("""CREATE TABLE IF NOT EXISTS intents (
        intent_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        user_id TEXT DEFAULT 'user',
        source TEXT DEFAULT 'unknown',
        action_type TEXT NOT NULL,
        parameters TEXT DEFAULT '{}',
        risk_level TEXT DEFAULT 'LOW',
        approval_required INTEGER DEFAULT 0,
        created_at TEXT NOT NULL,
        status TEXT DEFAULT 'pending',
        processed_at TEXT,
        result TEXT,
        verification_status TEXT
    )""")
    
    _DB.execute("""CREATE TABLE IF NOT EXISTS intent_steps (
        step_id TEXT PRIMARY KEY,
        intent_id TEXT NOT NULL REFERENCES intents(intent_id) ON DELETE CASCADE,
        action_type TEXT NOT NULL,
        description TEXT,
        parameters TEXT DEFAULT '{}',
        dependencies TEXT DEFAULT '[]',
        expected_state TEXT,
        risk TEXT DEFAULT 'LOW',
        approval_required INTEGER DEFAULT 0,
        seq INTEGER DEFAULT 0
    )""")
    
    _DB.execute("""CREATE TABLE IF NOT EXISTS plans (
        plan_id TEXT PRIMARY KEY,
        intent_id TEXT NOT NULL REFERENCES intents(intent_id),
        session_id TEXT NOT NULL,
        description TEXT NOT NULL,
        created_at TEXT NOT NULL,
        status TEXT DEFAULT 'draft',
        current_step_index INTEGER DEFAULT -1,
        steps_completed INTEGER DEFAULT 0,
        steps_total INTEGER DEFAULT 0,
        result TEXT,
        failure_reason TEXT
    )""")
    
    _DB.execute("""CREATE TABLE IF NOT EXISTS plan_steps (
        step_id TEXT PRIMARY KEY,
        plan_id TEXT NOT NULL REFERENCES plans(plan_id) ON DELETE CASCADE,
        step_index INTEGER NOT NULL,
        tool_name TEXT NOT NULL,
        arguments TEXT DEFAULT '{}',
        description TEXT,
        dependencies TEXT DEFAULT '[]',
        risk_level TEXT DEFAULT 'LOW',
        approval_required INTEGER DEFAULT 0,
        approval_id TEXT,
        expected_state TEXT,
        verification_method TEXT DEFAULT 'observe',
        status TEXT DEFAULT 'pending',
        started_at TEXT,
        completed_at TEXT,
        actual_result TEXT,
        error_message TEXT
    )""")
    
    _DB.execute("""CREATE INDEX IF NOT EXISTS idx_intents_session ON intents(session_id)""")
    _DB.execute("""CREATE INDEX IF NOT EXISTS idx_intents_created ON intents(created_at DESC)""")
    _DB.execute("""CREATE INDEX IF NOT EXISTS idx_intent_steps_intent ON intent_steps(intent_id)""")
    _DB.execute("""CREATE INDEX IF NOT EXISTS idx_plans_intent ON plans(intent_id)""")
    _DB.execute("""CREATE INDEX IF NOT EXISTS idx_plan_steps_plan ON plan_steps(plan_id)""")
    _DB.execute("""CREATE INDEX IF NOT EXISTS idx_plans_status ON plans(status)""")
    
    return _DB


# ---------------------------------------------------------------------------
# AI Chat Database Schema (Phase 73) - Deprecated (moved above)
# ---------------------------------------------------------------------------
