# VANTAGE / JARVIS Forge — Architecture & Integration Contracts

Derived from source on 2026-10-16. Where a contract is subtle, the invariant
and its test are named so a future change knows what it must not break.

## Topology

```
        ┌─────────────────────────────────────────────┐
        │  macOS app (SwiftPM, macos/Vantage)          │
        │  talks to 127.0.0.1:8765 only (loopback)     │
        └──────────────┬──────────────────────────────┘
                       │  X-JARVIS-Token (master)
                       ▼
        ┌─────────────────────────────────────────────┐
        │  Backend  (jarvis/, FastAPI)  :8765          │
        │  master token lives ONLY here                │
        │  dashboard/ (index.html, phone.html)         │
        └──────┬───────────────────────────┬───────────┘
               │ loopback, phone surface    │
               │ PHONE_ALLOWED (local)      │
               ▼                            ▼
     ┌──────────────────┐        ┌─────────────────────────────┐
     │ phone.html       │        │ Companion gateway :8766      │
     │ (local dashboard)│        │ (jarvis/companion/gateway)   │
     └──────────────────┘        │ TLS self-signed, 0.0.0.0     │
                                 │ proxy = master token         │
                                 └──────────────┬────────────────┘
                                                │  X-JARVIS-Token (scoped session)
                                                ▼
                                 Android companion (android/,
                                 com.vantage.companion)
```

## Auth model

- **Master token**: `jarvis/security/auth.py:TokenStore`. Generated once to
  `<home>/api_token` (0600) or taken from `JARVIS_API_TOKEN`. Never leaves the
  Mac. Sent as `X-JARVIS-Token`.
- **Scoped sessions**: two independent mechanisms —
  - Backend phone surface: `jarvis/api/app.py` mints session tokens checked
    against `PHONE_ALLOWED` (the _local_ phone surface, larger set).
  - Companion gateway: `jarvis/companion/gateway.py` mints `CMP*` session
    tokens from one-shot pairing codes, checked against its own `PHONE_ALLOWED`
    (a **strict subset**). The gateway forwards to the backend _as the master
    token_; the Android session token never reaches the backend.
- **Invariant (test-pinned)**: `gateway.PHONE_ALLOWED − {"/health"} ⊆
backend PHONE_ALLOWED`. See `tests/test_companion_gateway.py::
test_allowlist_is_a_subset_of_the_backend_phone_surface`. The local surface
  may be _larger_ than the remote one; the remote one must never _widen_.

## Pairing flow (target contract)

1. Mac: `POST /companion/pairing-code` → `{code, certificate_fingerprint,
expires_in_seconds=120, companion_port}`. Only one live code per surface —
   issuing a new code supersedes the old (verified).
2. QR / manual entry of host:port + fingerprint on the phone.
3. Android: `POST /companion/pairing-redeem {code}` → `{session_token,
surface, expires_in=28800}`. One-shot; reuse/expiry → 403.
4. Android pins the served `certificate_fingerprint` (OkHttp
   CertificatePinner) — **not yet implemented in the client** (R1.4).
5. Android stores the token in EncryptedSharedPreferences (done).
6. Requests carry `X-JARVIS-Token: <session_token>` + optional
   `X-Vantage-Device-Id`. Allowed phone paths → 200; everything else → 403
   before touching the backend.
7. Sign-out: `DELETE /companion/session` revokes; revoked session → 401.

Legacy contract (the phone _web_ page, `dashboard/phone.html`): scrapes
`jarvis-bootstrap` meta from `GET /phone`, `POST /auth/session` — kept intact
for the local page; the Android client must not depend on it.

## Request pipeline (backend)

`POST /command` → `JarvisCore.handle` → **rule planner** (54 regex rules,
`jarvis/core/planner.py`) → `PermissionEngine.evaluate` (risk READ/LOW/
EXTERNAL/DESTRUCTIVE/PHYSICAL → EXECUTE/CONFIRM/BLOCK) → tool execution →
tri-state `Verification {SUCCESS,UNKNOWN,FAILED}` → audit → reply.

The **model** is a fallback annotator only: invoked when the planner yields
`not_understood`/`smalltalk`/`question` (`orchestrator.py:532-536`), single
turn, no history. A second, model-driven surface `POST /ai/chat/send`
(`app.py:3621`) has no tools/permissions. Unifying these is R2.2.

There is a **second, parallel** pipeline under `jarvis/agent/` (Phase 76) and
`jarvis/workflows/` with weaker verification — drift risk, see roadmap.

## Config

`jarvis/config.py:Settings` (pydantic, env prefix `JARVIS_`). `home` =
`~/.jarvis-forge` (or `JARVIS_HOME`; tests use a temp home). `llm_enabled` is
true only when AI is on **and** a key is available (env **or** the Keychain
store via `keyring`). Base URL not required (providers have defaults).

## Build/packaging

- **Backend**: `run.py` (uvicorn :8765). `pytest` for tests.
- **macOS desktop bundle**: `build_app.py` + `install_app.sh` → a Python
  launcher bundle (`app/launcher.py`), NOT the Swift app.
- **Native macOS app**: `swift build -C macos/Vantage` (separate artifact).
- **Android**: canonical `android/` (Compose) — no build script yet; the
  release pipeline wrongly builds `installers/android/` (WebView wrapper).
