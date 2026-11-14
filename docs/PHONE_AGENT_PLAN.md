# v0.6 Implementation Plan — Phone Agent + Secure Device-to-Device Transfer

**Status:** architecture review complete · awaiting approval before any code changes
**Baseline:** v0.5 — 312 tests passing, 24 tools, 2 agents
**Rule:** preserve all existing functionality and tests

---

## 1. Architecture review — findings

### 1.1 What already exists and is directly reusable

| Component                       | File                                               | Reusable for v0.6                                                                                                                                                    |
| ------------------------------- | -------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `Agent` base + `build_agents()` | `jarvis/agents/base.py`                            | **Yes.** A new agent is one class + one entry in `build_agents`. `tool_names()` derives ownership from `spec.agent`, so tools auto-attach.                           |
| `ToolSpec` / `ToolRegistry`     | `jarvis/registry/tools.py`                         | **Yes.** `register(spec, handler)`, `validate_params()` (rejects unknown/missing params), `allowed_devices`.                                                         |
| `PermissionEngine.evaluate()`   | `jarvis/security/policy.py`                        | **Yes — unchanged.** It is the single authorisation path: param validation → device exists → `allowed_devices` → online → path guard → revoked grants → risk policy. |
| Confirmation mechanism          | `policy._create_confirmation`, `ConfirmationState` | **Yes — this is the key reuse.** Pairing approval is exactly a CONFIRM with a preview. No new approval system needed.                                                |
| `RecoveryEngine`                | `jarvis/core/recovery.py`                          | **Yes.** Transfers get retry/alternative/escalation for free once tools return failures. Already refuses to retry destructive actions.                               |
| `TaskManager`                   | `jarvis/core/tasks.py`                             | **Yes.** Transfer progress rides on a job (create/update_step/pause/resume/retry/cancel already exist).                                                              |
| `AuditLog`                      | `jarvis/security/audit.py`                         | **Yes.** Hash-chained, redaction-aware, `verify_chain()`.                                                                                                            |
| `DeviceRegistry`                | `jarvis/registry/devices.py`                       | **Partially.** Exists, but the model is too thin (§1.2).                                                                                                             |
| FastAPI + auth dependency       | `jarvis/api/app.py`                                | **Yes.** New endpoints inherit token auth via `Protected`.                                                                                                           |

### 1.2 Gaps that must be filled

| #   | Gap                                                  | Evidence                                                                                                           | Impact                                                                                                                                                               |
| --- | ---------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| G1  | **`DeviceSpec` has no identity or key material**     | Model has only `device_id, type, owner, platform, capabilities, trust_level, online, connection, metadata`         | Cannot express pairing, auth state, revocation, last-seen. Needs model + DB migration.                                                                               |
| G2  | **`devices` table has no key/auth columns**          | `CREATE TABLE devices (device_id, type, owner, platform, trust_level, capabilities, online, metadata, updated_at)` | Needs an idempotent `ALTER TABLE` migration (pattern already exists in `Database._migrate`).                                                                         |
| G3  | **`evaluate()` BLOCKS any offline device**           | `if not device.online: return BLOCK`                                                                               | `phone_main` is seeded `online=False`, so any action routed to the phone is blocked. **Drives the central design decision in §2.1.**                                 |
| G4  | **No transport layer**                               | No TLS server, no transfer protocol anywhere                                                                       | Must be built.                                                                                                                                                       |
| G5  | **No crypto libraries**                              | `cryptography` and `nacl` are MISSING                                                                              | But `openssl 3.6.4` is installed and verified working, and `ssl`/`hmac`/`hashlib`/`secrets`/`base64` are stdlib. TLS + HMAC are achievable without new dependencies. |
| G6  | **`allowed_devices` defaults to `["laptop_main"]`**  | `ToolSpec.allowed_devices` default                                                                                 | Phone tools need explicit `allowed_devices`.                                                                                                                         |
| G7  | **Standing grants auto-downgrade CONFIRM → EXECUTE** | `evaluate()` step 5                                                                                                | A standing grant must never silently approve a transfer to a **newly paired / unknown** device. Needs a guard.                                                       |

### 1.3 Confirmed extension points

- Agent dispatch: `orchestrator.py:239` `find_agent(self.agents, action.tool)` → `agent.execute(action, ctx)`
- Multi-step plans already supported: `orchestrator.py:200` iterates `plan.actions`
- Tool registration is declarative: `jarvis/tools/__init__.py::build_default_registry()`

---

## 2. Design decisions

### 2.1 `action.device` is the **executor**, not the target — the central decision

`PermissionEngine.evaluate()` blocks offline devices (G3). `phone_main` is seeded offline.

**Decision:** transfer tools execute on **`laptop_main`** and take a `target_device` parameter.

```python
ToolSpec(
    name="send_to_device",
    allowed_devices=["laptop_main"],        # the laptop does the work
    parameters={"path": "string", "target_device": "string"},
    risk=RiskLevel.EXTERNAL,                # → CONFIRM
)
```

Why this is correct, not a workaround:

- The laptop performs the work; the phone is the destination.
- The offline check then correctly applies to the _executor_ (the laptop).
- The phone's reachability is a **tool-level concern** reported honestly
  ("phone is not reachable") rather than a policy-level block with a misleading reason.
- It keeps `PermissionEngine` unchanged — a hard requirement.

### 2.2 The phone is a separate endpoint process, not a hard-coded stub

There is no Android app, and the roadmap says _"Do not hard-code the phone
implementation into the orchestrator."_

**Decision:** build a `PhoneEndpoint` — a small service implementing the **phone
side of the protocol**, runnable as a separate local process. The future Android
app implements the same protocol. This keeps the phone out of the orchestrator
and makes the whole flow genuinely testable end to end.

### 2.3 Pairing reuses the existing confirmation mechanism

No new approval system. Pairing is a `CONFIRM`:

```
Phone endpoint → POST /devices/pair/request (public key + name)
JARVIS         → PolicyDecision.CONFIRM  (preview: "Pixel 8 requesting pairing")
User           → approves
JARVIS         → device registered as trusted, key stored, audited
```

### 2.4 Encryption without new dependencies

`cryptography`/`nacl` are absent (G5), so:

- **Transport:** TLS via stdlib `ssl`, certificate generated with `openssl` (verified working)
- **Device authentication:** HMAC-SHA256 challenge–response over the pairing key
- **Integrity:** SHA-256 checksum per file, verified after transfer
- **Secrets at rest:** key material stored `0600` in `~/.jarvis-forge/devices/`

This is real cryptography using the platform's own TLS stack — not a
home-rolled cipher. I will not invent an encryption scheme.

---

## 3. Data model changes

### 3.1 `DeviceSpec` — new optional fields (backward compatible)

```python
public_key: str = ""          # base64 identity key
auth_state: str = "unpaired"  # unpaired | pending | paired | revoked
paired_at: Optional[datetime] = None
last_seen: Optional[datetime] = None
fingerprint: str = ""         # short human-verifiable hash of public_key
```

Defaults preserve current behaviour: existing devices remain `unpaired` and
everything that works today keeps working.

### 3.2 New tables (added to `SCHEMA` + `_migrate`)

| Table             | Purpose                                                                                                                     |
| ----------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `device_pairings` | pairing_id, device_id, public_key, state, requested_at, approved_at, approved_by                                            |
| `transfers`       | transfer_id, job_id, direction, source, destination, path, size, sha256, state, bytes_sent, error, created_at, completed_at |
| `transfer_events` | transfer_id, state, detail, created_at (progress + audit trail)                                                             |

Migration follows the existing idempotent pattern in `Database._migrate()`
(`PRAGMA table_info` → `ALTER TABLE ADD COLUMN`).

---

## 4. Tool surface (9 new tools, agent="phone")

| Tool                      | Risk          | Confirm | Purpose                           |
| ------------------------- | ------------- | ------- | --------------------------------- |
| `list_devices`            | 0 read        | no      | All known devices + state         |
| `get_device_status`       | 0 read        | no      | Online, last seen, battery        |
| `get_device_capabilities` | 0 read        | no      | What the device can do            |
| `ping_device`             | 0 read        | no      | Reachability check                |
| `register_phone`          | 2 external    | **yes** | Begin pairing (creates a CONFIRM) |
| `approve_pairing`         | 3 destructive | **yes** | Trust a pending device            |
| `remove_phone`            | 3 destructive | **yes** | Revoke trust                      |
| `send_to_device`          | 2 external    | **yes** | Mac → Phone transfer              |
| `get_transfer_status`     | 0 read        | no      | Progress                          |
| `cancel_transfer`         | 1 low         | no      | Cancel in flight                  |

**Explicitly NOT provided:** any tool that executes a command on the phone, or
that lets a remote device execute anything on the Mac (non-negotiable rules 9).

---

## 5. Transfer lifecycle

```
PENDING → CONFIRMED → CONNECTING → TRANSFERRING → VERIFYING → COMPLETED
                                                       ↓
                                          FAILED / CANCELLED
```

- Progress rides on `TaskManager` steps
- Transient network failures → `RecoveryEngine` retries
- Every state transition → audit record
- Checksum verified before COMPLETED; never report success without verification
- **Never silently overwrite** — on collision, rename with a suffix or refuse
- **Never execute a transferred file**
- Size limit + timeout enforced

---

## 6. Files to add / change

**Add**

```
jarvis/agents/phone.py            PhoneAgent
jarvis/tools/phone.py             9 tool handlers + SPECS
jarvis/transport/__init__.py
jarvis/transport/protocol.py      message types + HMAC challenge-response
jarvis/transport/endpoint.py      PhoneEndpoint (the "phone" side)
jarvis/transport/server.py        TLS server on the Mac side
jarvis/transport/transfer.py      chunked transfer + checksum verification
jarvis/devices/pairing.py         pairing lifecycle + key storage
tests/test_phone.py               agent + tools + pairing
tests/test_transfer.py            lifecycle, checksum, cancellation, failures
docs/PHONE_AGENT_PLAN.md          this file
```

**Change (minimally)**

```
jarvis/core/models.py             DeviceSpec: +5 optional fields
jarvis/db/database.py             +3 tables, +1 migration entry
jarvis/registry/devices.py        pair/revoke helpers; phone seeded unpaired
jarvis/agents/base.py             register PhoneAgent in build_agents()
jarvis/tools/__init__.py          register phone SPECS
jarvis/core/planner.py            intents: "send X to my phone", "pair my phone"
jarvis/api/app.py                 /devices/pair/*, /transfers/*
jarvis/security/policy.py         ONE guard: standing grants must not auto-approve
                                  transfers to an unpaired device (G7)
README.md, STATUS.md              document v0.6
```

`PermissionEngine` is otherwise untouched — the pipeline and risk tiers stay as they are.

---

## 7. Test plan (additive — existing 312 must still pass)

| Area               | Tests                                                                                                                                                               |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Pairing            | request → CONFIRM → approve → trusted; deny; revoke; duplicate ID; unpaired device refused                                                                          |
| Device identity    | unique ID enforced, name alone is never sufficient, key material stored 0600                                                                                        |
| Transfer lifecycle | happy path; checksum mismatch → FAILED; cancellation; timeout; size limit; no silent overwrite                                                                      |
| Security           | unpaired device refused; revoked device refused; remote command execution impossible (no such tool); HMAC rejects a wrong key; transferred files are never executed |
| Recovery           | transient failure → retry → success; deterministic failure → no retry                                                                                               |
| Permission         | transfer requires confirmation; standing grant cannot bypass pairing (G7)                                                                                           |
| API                | all new endpoints require auth; pairing approval auditable                                                                                                          |
| Regression         | full existing suite must remain green                                                                                                                               |

**Definition of done:** full suite green, security suite green, acceptance sweep
green, `STATUS.md` updated, and an honest report of what remains unavailable.

---

## 8. Risks

| Risk                                                 | Mitigation                                                                        |
| ---------------------------------------------------- | --------------------------------------------------------------------------------- |
| Phone endpoint is a stand-in, not a real Android app | State this plainly. The protocol is the deliverable; the app implements it later. |
| TLS cert trust on first use                          | Fingerprint shown to the user at pairing; stored and pinned afterwards.           |
| Long transfers blocking the event loop               | Chunked async transfer with progress; `TaskManager` for state.                    |
| Partial file left on failure                         | Write to a temp name, verify checksum, then atomic rename.                        |
| Test suite becoming slow                             | Use small files and a local endpoint; no network dependency.                      |

---

## 9. What I will NOT do

- Not rewrite or restructure anything that works today
- Not remove or weaken any existing security control
- Not add a tool that executes commands on the phone, or lets the phone execute
  anything on the Mac
- Not add a dependency that is not already available
- Not claim the Android app exists when it does not

---

## 10. Open decisions for approval

1. **§2.1 — `action.device = laptop_main` with `target_device` as a parameter.**
   Recommended. Alternative is making the phone an executing device, which
   requires relaxing the offline-device block in the permission engine.
2. **§2.2 — build a `PhoneEndpoint` stand-in process** to make this testable,
   rather than leaving the phone unimplemented until an Android app exists.
3. **§2.4 — TLS via `openssl`-generated certs + HMAC device auth**, given no
   `cryptography` library is installed.
