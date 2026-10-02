"""Core domain models shared across Vantage.

These are deliberately plain Pydantic models so they can be serialised to the
API, persisted to SQLite, and passed between agents without translation.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timezone
from enum import Enum, IntEnum
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    """A short, unique, human-readable identifier for logs and records."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def new_secret_id(prefix: str) -> str:
    """A higher-entropy identifier for things that act as credentials.

    Confirmation IDs authorise a real action, so they get 128 bits rather than
    the 48 used for ordinary log identifiers -- they should not be guessable
    even by someone who can reach the API.
    """
    return f"{prefix}-{secrets.token_hex(16)}"


# ---------------------------------------------------------------------------
# Risk & policy enums
# ---------------------------------------------------------------------------
class RiskLevel(IntEnum):
    """Risk tiers from section 8 of the plan."""

    READ = 0          # time, battery, calendar read, memory read
    LOW = 1           # open app, turn on light, start music
    EXTERNAL = 2      # calendar write, file transfer, install approved app
    DESTRUCTIVE = 3   # delete, send email, uninstall, share private data
    PHYSICAL = 4      # unlock door, vehicle control, payments, machinery


class PolicyDecision(str, Enum):
    """The three human-in-the-loop states from section 48."""

    EXECUTE = "EXECUTE"
    CONFIRM = "CONFIRM"
    BLOCK = "BLOCK"


class ConfirmationState(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"


class ActionStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    #: The handler ran and answered, but whether the world changed was not
    #: established. Distinct from FAILED (it did not fail) and from SUCCEEDED
    #: (it was not observed). Phase 9 found that `.ok` alone was being recorded
    #: as success for offline hardware reads and no-op actuations.
    UNVERIFIED = "unverified"
    BLOCKED = "blocked"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    SKIPPED = "skipped"


class MemoryScope(str, Enum):
    """Memory lifetime policies from section 30."""

    TEMPORARY = "temporary"
    SESSION = "session"
    PROJECT = "project"
    LONG_TERM = "long_term"
    PRIVATE = "private"


class MemoryType(str, Enum):
    """What kind of thing a memory is (section 30 / v0.9).

    The type drives filtering, expiry defaults and how a memory is presented.
    """

    PREFERENCE = "preference"      # how the user likes things done
    FACT = "fact"                  # a statement about the world
    TASK = "task"                  # something to do
    PROJECT = "project"            # project-level knowledge
    DECISION = "decision"          # a choice that was made, and why
    EXPERIMENT = "experiment"      # a run and its setup
    FAILURE = "failure"            # something that went wrong
    FIX = "fix"                    # what resolved a failure
    DOCUMENT = "document"          # a file or its contents
    CONVERSATION = "conversation"  # something said in dialogue
    DEVICE = "device"              # device state or identity
    RELATIONSHIP = "relationship"  # how two things relate
    GENERAL = "general"            # unclassified

    @classmethod
    def values(cls) -> List[str]:
        return [member.value for member in cls]


class PrivacyLevel(str, Enum):
    PUBLIC = "public"
    INTERNAL = "internal"
    PRIVATE = "private"
    SECRET = "secret"


# ---------------------------------------------------------------------------
# Registries: tools, devices, applications
# ---------------------------------------------------------------------------
class ToolSpec(BaseModel):
    """Declarative description of a callable action (section 7 & 8).

    Frozen, and its gate-read list fields are tuples. ``PermissionEngine`` and
    the recovery policy read *this object* out of the registry on every call, so
    a mutable spec was a permission bypass: measured on the previous tree,
    ``core.tools.get("delete_file").risk = RiskLevel.READ`` moved the gate from
    ``CONFIRM (level 3 (destructive)...)`` to ``EXECUTE (low-risk action)`` and
    made the same action automatically retryable. Tuples matter as much as
    ``frozen`` -- ``tags`` decides whether an emergency stop may be retried, and
    a frozen field holding a list can still be appended to in place.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    description: str = ""
    agent: str = "laptop"
    risk: RiskLevel = RiskLevel.READ
    requires_confirmation: bool = False
    reversible: bool = True
    allowed_devices: Tuple[str, ...] = Field(default_factory=lambda: ("laptop_main",))
    # JSON-schema-ish parameter description used for validation & prompting.
    # Still a dict (so ``spec.parameters[k] = ...`` remains possible in place):
    # nothing downstream authorises from it -- see Phase 26's residual note.
    parameters: Dict[str, str] = Field(default_factory=dict)
    required_parameters: Tuple[str, ...] = ()
    #: Identifiers this tool *prints*, and where to find them in its result:
    #: ``{"workspace_id": {"items": "workspaces", "id": "workspace_id", "label": "name"}}``.
    #:
    #: Declared on the enumerator rather than on the dozens of tools that consume the
    #: identifier, because the thing that knows the shape of an id is the thing that hands
    #: it out. A clarification that needs one (Phase 33) looks a candidate list up here
    #: instead of keeping a second copy of the mapping in the router -- the pattern Phase
    #: 30 and 32 established, and the failure mode 32 documented: two lists that drift.
    #: Only a READ-tier tool that requires nothing may be declared, and a test sweeps it.
    provides_identifiers: Dict[str, Dict[str, str]] = Field(default_factory=dict)
    tags: Tuple[str, ...] = ()

    def risk_label(self) -> str:
        return {
            RiskLevel.READ: "read",
            RiskLevel.LOW: "low",
            RiskLevel.EXTERNAL: "external",
            RiskLevel.DESTRUCTIVE: "destructive",
            RiskLevel.PHYSICAL: "physical",
        }[self.risk]


class DeviceSpec(BaseModel):
    """A connected device identity (section 6).

    The identity fields below are additive: devices that predate pairing simply
    stay ``unpaired`` with no key material, so existing behaviour is unchanged.
    """

    device_id: str
    type: str
    owner: str = "user"
    platform: str = "unknown"
    capabilities: List[str] = Field(default_factory=list)
    trust_level: str = "medium"  # low | medium | high
    online: bool = True
    connection: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    # --- identity & pairing (v0.6) -----------------------------------------
    #: Human-verifiable fingerprint derived from the device secret. Safe to
    #: display and log; the secret itself never leaves its own store.
    fingerprint: str = ""
    #: unpaired | pending | paired | revoked
    auth_state: str = "unpaired"
    paired_at: Optional[datetime] = None
    last_seen: Optional[datetime] = None
    #: Path to the device's secret, if this machine holds it. Never the secret.
    key_ref: str = ""

    @property
    def is_paired(self) -> bool:
        return self.auth_state == "paired"

    @property
    def is_revoked(self) -> bool:
        return self.auth_state == "revoked"


class AppSpec(BaseModel):
    """An installable/launchable application (section 11)."""

    key: str
    display_name: str
    platform: List[str] = Field(default_factory=lambda: ["macos"])
    risk: RiskLevel = RiskLevel.LOW
    # Per-platform launch identifiers, e.g. {"macos": "Visual Studio Code"}
    launch: Dict[str, str] = Field(default_factory=dict)
    aliases: List[str] = Field(default_factory=list)
    category: str = "general"


# ---------------------------------------------------------------------------
# Planning & execution
# ---------------------------------------------------------------------------
class PlannedAction(BaseModel):
    """A single validated tool invocation produced by the planner."""

    step_id: int = 0
    tool: str
    params: Dict[str, Any] = Field(default_factory=dict)
    device: str = "laptop_main"
    reason: str = ""
    status: ActionStatus = ActionStatus.PENDING
    result: Optional["ActionResult"] = None


class Verification(str, Enum):
    """Whether an *external* effect was observed, not whether code returned.

    UNKNOWN exists as its own state on purpose. ``verified=False`` means "this
    did not happen" only for a failure; for a command that was never sent
    because no gateway is configured, the honest answer is that the outcome
    cannot be established. Collapsing the two either hides failures or invents
    them.
    """

    SUCCESS = "success"
    FAILED = "failed"
    UNKNOWN = "unknown"


class ActionResult(BaseModel):
    """Outcome of executing one planned action."""

    ok: bool
    tool: str
    device: str = "laptop_main"
    output: Dict[str, Any] = Field(default_factory=dict)
    message: str = ""
    error: Optional[str] = None
    verified: bool = False
    verification: Verification = Verification.UNKNOWN
    verification_note: str = ""
    duration_ms: int = 0
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _reconcile_verification(self) -> "ActionResult":
        """Keep ``verified`` and ``verification`` from disagreeing.

        ``verified`` stays a plain field because schedulers, the CLI and the
        dashboard all read it and existing callers construct it; ``verification``
        is the richer statement. Deriving one from the other here means a
        handler cannot claim success in one field and uncertainty in the other.
        """
        if self.verification is Verification.SUCCESS:
            self.verified = True
        elif self.verification is Verification.FAILED:
            self.verified = False
        elif self.verified:
            # Explicitly verified without a tri-state: treat as established.
            self.verification = Verification.SUCCESS
        else:
            self.verification = Verification.UNKNOWN
        if not self.ok:
            self.verified = False
            if self.verification is Verification.UNKNOWN and self.error:
                self.verification = Verification.FAILED
        return self

    @property
    def audit_result(self) -> str:
        """The status token the audit log stores, derived from verification.

        This is what keeps a hardware no-op out of the log as a success: the
        audit line is produced from the same field the user-facing reply is,
        so prose and record cannot diverge.
        """
        return self.verification.value

    @property
    def summary(self) -> str:
        if self.ok:
            return self.message or f"{self.tool} completed"
        return self.error or f"{self.tool} failed"


class Plan(BaseModel):
    """A decomposed goal: an ordered list of actions plus metadata."""

    plan_id: str = Field(default_factory=lambda: new_id("PLAN"))
    goal: str = ""
    intent: str = "unknown"
    actions: List[PlannedAction] = Field(default_factory=list)
    needs_clarification: bool = False
    clarification_question: Optional[str] = None
    direct_reply: Optional[str] = None
    #: Why the plan produced no action, in the planner's own words. It knows which
    #: of these it hit and the response used to flatten them together, which is how
    #: "I did not understand", "I have no such capability", "I will not do that" and
    #: "may I have more detail" all reached the ledger as the same `no_action`.
    #: ``""`` means the planner did not say, and the renderer must guess nothing.
    reply_kind: str = ""  # not_understood | capability_missing | declined | clarification | refusal
    #: Parts of the sentence this plan does not cover, each with the planner's own
    #: reason. A request whose second half was refused used to be answered with only
    #: the first half, silently.
    unhandled: List[str] = Field(default_factory=list)
    #: Which read-only enumerators answered a question this plan raised (Phase 33). A
    #: clarification built from another tool's result carries the same debt as a fallback
    #: that answers with one: the chain has to say which lookup produced the candidates.
    identifier_sources_used: List[str] = Field(default_factory=list)
    #: Identifiers the sentence did not supply, so the orchestrator can look candidates up.
    missing_identifiers: List[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    planner: str = "rules"  # rules | llm


class CommandRequest(BaseModel):
    """An inbound user command from any surface (voice, CLI, API, watch)."""

    text: str
    user_id: str = "user"
    device_id: str = "laptop_main"
    session_id: str = Field(default_factory=lambda: new_id("SESS"))
    source: str = "cli"  # cli | voice | api | watch
    context: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("text")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("command text must not be empty")
        return value.strip()


class CommandResponse(BaseModel):
    """What JARVIS says back, plus the machine-readable trace."""

    request_id: str = Field(default_factory=lambda: new_id("REQ"))
    session_id: str = ""
    reply: str = ""
    intent: str = "unknown"
    plan: Optional[Plan] = None
    actions: List[ActionResult] = Field(default_factory=list)
    #: Populated when an action failed and was recovered automatically, so the
    #: user is told what went wrong even though the outcome was a success.
    recovery_notes: List[str] = Field(default_factory=list)
    decision: Optional[PolicyDecision] = None
    needs_confirmation: bool = False
    confirmation_id: Optional[str] = None
    blocked_reason: Optional[str] = None
    #: Which job this command's steps belong to, when the goal was a sequence.
    #: A correlation id only -- it carries no verdict, and the states behind it
    #: are read from the server, never supplied by whoever renders them.
    job_id: str = ""
    model_used: str = "rules"
    duration_ms: int = 0
    created_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------
class MemoryItem(BaseModel):
    """One stored fact (section 4.10 / 30)."""

    id: str = Field(default_factory=lambda: new_id("MEM"))
    content: str
    source: str = "user"
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    confidence: float = 1.0
    scope: MemoryScope = MemoryScope.SESSION
    privacy_level: PrivacyLevel = PrivacyLevel.INTERNAL
    project_id: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    embedding: Optional[List[float]] = None

    # --- v0.9 lifecycle -----------------------------------------------------
    memory_type: MemoryType = MemoryType.GENERAL
    #: 0.0–1.0. Higher-importance memories survive pruning and rank higher.
    importance: float = 0.5
    #: When this memory stops being true or useful, if ever.
    expires_at: Optional[datetime] = None
    #: Ids of related memories, with the relationship name.
    relationships: Dict[str, List[str]] = Field(default_factory=dict)

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        if self.expires_at is None:
            return False
        return self.expires_at <= (now or utcnow())


class GraphNode(BaseModel):
    """A node in the project-state graph (section 5)."""

    node_id: str = Field(default_factory=lambda: new_id("NODE"))
    project_id: str
    kind: str  # DESIGN | CODE | COMPONENT | EXPERIMENT | FAILURE | FIX | FILE | TASK | DEVICE
    label: str
    data: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)


class GraphEdge(BaseModel):
    """A typed relationship between two graph nodes."""

    edge_id: str = Field(default_factory=lambda: new_id("EDGE"))
    source_id: str
    target_id: str
    relation: str = "related_to"
    created_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------------------
# Tasks / jobs
# ---------------------------------------------------------------------------
class JobStep(BaseModel):
    name: str
    status: ActionStatus = ActionStatus.PENDING
    detail: str = ""


class Job(BaseModel):
    """A long-running unit of work (section 31)."""

    job_id: str = Field(default_factory=lambda: new_id("JOB"))
    goal: str
    status: str = (
        "running"
        # running | paused | done | failed | cancelled | awaiting_confirmation |
        # incomplete | unverified
    )
    steps: List[JobStep] = Field(default_factory=list)
    requires_confirmation: List[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------
class AuditRecord(BaseModel):
    """Append-only audit entry (section 46)."""

    timestamp: datetime = Field(default_factory=utcnow)
    user: str = "user"
    device: str = "laptop_main"
    session_id: str = ""
    request_id: str = ""
    intent: str = ""
    tool: str = ""
    target: str = ""
    risk: str = "read"
    confirmation: bool = False
    result: str = ""
    detail: Dict[str, Any] = Field(default_factory=dict)
    # Tamper-evidence: each record commits to the one before it, so removing or
    # editing a past entry breaks the chain and becomes detectable.
    prev_hash: str = ""
    record_hash: str = ""


PlannedAction.model_rebuild()
