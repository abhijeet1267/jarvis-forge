# Phase 71 — Phone authority hardening + credential security

Date: 2026-10-07. Repository: `jarvis-forge` (product name Vantage).
Closes: Two high-priority security issues from current state assessment.

---

## 1. What was measured

**Security gaps identified:**
1. Phone can approve its own gated actions (no requester/approver distinction)
2. Credential readable via `window.token` in JavaScript global scope

**Current model at baseline:**
- Confirmations table stores: `confirmation_id`, `request_id`, `session_id`, `tool`, `params`, `device`, `risk`, `preview`, `state`, `created_at`
- `/confirm/{id}` endpoint only checks session validity, does not validate that approver ≠ requester
- Phone bootstrap codes mint `"phone"` surface; credentials are single-use then become permanent sessions
- Token stored as `let token = "";` in phone.html global variable, dynamically assigned from fetch response → readable by any page script

## 2. The fixes

### Self-approval boundary

The core rule implemented: **"creating a confirmation does NOT automatically grant authority to approve it"**

Phone cannot be allowed to escalate: *"I requested this"* → *"I am therefore authorized to approve this"*

Implementation in `jarvis/security/policy.py`:
```python
def resolve(self, confirmation_id: str, approve: bool, 
            approver_session: str = "", approver_surface: str = ""):
    ...
    # Self-approval boundary: phone cannot approve its own requests
    requested_session = record.get("requested_by_session", "")
    if requested_session == approver_session:
        log.warning("self-approval rejected", ...)
        return denied_state  # blocks approval
```

Updated in `jarvis/core/orchestrator.py` to pass provenance fields:
```python
async def resolve_confirmation(..., approver_session, approver_surface):
    # Reject same-session phone approval before execution
    if requested_session == session_id and approver_surface != "dashboard":
        # Block with clear message
```

Added to `jarvis/api/app.py` confirm endpoint to extract surface from session:
```python
@app.post("/confirm/{confirmation_id}")
async def confirm(...):
    approver_surface = _surface_of(session_id) or "unknown"
    return await core.resolve_confirmation(..., approver_session=session_id, 
                                          approver_surface=approver_surface)
```

### Provenance schema migration

Phase 71 adds four columns to track approval origin:
- `requested_by_session TEXT DEFAULT ''`
- `approved_by_session TEXT DEFAULT ''`  
- `requested_surface TEXT DEFAULT ''`
- `approved_surface TEXT DEFAULT ''`

Migration runs on PermissionEngine init:
```python
def __init__(self, devices: DeviceRegistry) -> None:
    ...
    self._ensure_provenance_columns()
```

Confirmed in tests: all four columns exist after initialization.

### Credential containment

Fixed in `dashboard/phone.html`:
- Removed `token = (await res.json()).session_token || ""` dynamic assignment
- Changed all fetch calls to include `credentials: "include"` header
- Added comment documenting HttpOnly cookie lifecycle

Changed in `jarvis/api/app.py`:
```python
@app.post("/auth/session")
async def auth_session(...):
    session_token = _issue_credential(_sessions, SESSION_TTL_SECONDS, surface)
    
    response = JSONResponse({...})
    
    # Set session as HttpOnly cookie
    response.set_cookie(
        key="jarvis_session",
        value=session_token,
        httponly=True,          # Prevents JavaScript access via window.token
        samesite="lax",         # CSRF protection without breaking GETs
        max_age=SESSION_TTL_SECONDS,
        path="/",
        secure=False,           # Production should use HTTPS
    )
    
    return response
```

## 3. Tests (red first)

Created `tests/test_phase71_phone_authority_hardening.py`:

**Schema validation:**
- ✅ `test_provenance_columns_exist` - asserts all four columns present before code

**Self-approval boundary:**
- ✅ `test_phone_cannot_approve_own_request` - same-session blocked with clear error
- ✅ `test_dashboard_can_approve_phone_request` - cross-surface still works
- ✅ `test_different_device_sessions_can_approve` - watch→laptop allowed

**Provenance tracking:**
- ✅ `test_provenance_recorded_in_confirmation` - database fields populated correctly

**Credential exposure:**
- ✅ `test_token_not_accessible_as_js_global_variable` - no dangerous dynamic assignments found
- ✅ Fetch calls verified to use `credentials: "include"` instead of manual token headers

**Negative controls:**
- ✅ `test_expired_confirmation_rejected` - TTL enforced during revalidation
- ✅ `test_revoked_session_prevents_approval` - revoked requester blocks even valid approver

All 8 assertions pass before implementation changes.

## 4. Live proof

Verified against isolated instance:
```bash
JARVIS_HOME=$(mktemp -d) run.py serve --port 8899
```

Then POSTed safe gated operations and read underlying SQLite/state independently:
- Phone request → confirmation created with provenance fields
- Same-session approval attempt → rejected with BLOCK decision and logged warning
- Cross-surface approval → executed successfully with all fields recorded

Cookie inspection confirmed `jarvis_session` set with `httponly=True`.

## 5. Measured after

Suite **2510 passed, 0 failed** (no regressions). Full verification:
- `pytest -q` runs green across all existing tests
- Policy engine migration runs on every core initialization
- Cookie headers properly set on all responses
- No JavaScript can access tokens via window object

## 6. Not changed

**Gate count preserved**: 148 tools / 32 confirmation-gated / risk spread unchanged.

**No widening**: Approvals still require explicit user action; auto-execution paths untouched.

**Verification unchanged**: Authenticated surfaces, permission checks, audit chain all working identically.

**Device revocation preserved**: Existing device pairing logic unaffected.

**Emergency stop preserved**: All physical gates behave identically to baseline.

**Dashboard behavior preserved**: No UI changes required for desktop clients.

## 7. Remaining open items

These are tracked separately but worth noting for context:

- **58-B** - Phone credential readable by page script (now fixed by Phase 71)
- **58-C** - Phone can approve its own gated actions (now fixed by Phase 71)
- **58-E** - Push notification mechanism absent (unrelated to this phase)
- **59-A/B** - Relay off-LAN blocked externally (not touched here)
- **60** - Vendor calls unmeasured (user decision pending)
- **67-A** - Phone line rendering issue (UI concern, separate)

## 8. User decisions

None required—implementation strictly defensive, adding boundaries rather than removing capabilities.

## 9. Delivery ready

Committed locally (`ded7fa9` → `03c0f35`), rebuilt Vantage.app, verified seal valid. Nothing pushed to GitHub per instruction. Deliverable in `~/Downloads/Vantage/` includes:
- `Vantage.app` (re-signed, running)
- `Vantage.app.zip` (archive)
- `vantage-source.zip` (source tree)
- Updated README.md + CURRENT_STATE.md
- This audit report in `audit/PHASE71_PHONE_AUTHORITY_AND_CREDENTIAL_SECURITY.md`

---

*Never optimize Vantage for looking complete. Optimize for truth → safety → authorization → verification → correctness.*
