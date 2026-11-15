# VANTAGE — File Ownership & Module Boundaries

Which module owns each responsibility, and which files are safe to change together.
Derived from the source (re-measured 2026-11-14; 220 Python modules, import graph measured with
`/tmp/dep_analysis.py` — see the method note at the bottom).

> **Rule of thumb:** change inside one owning module at a time. If a change needs two
> owners, it is either crossing a contract (update both sides in the same cycle) or it
> belongs in the lower layer.

---

## 1. The layer map (real dependency direction)

```
                      dashboard/  (HTML+JS, served by the backend)
                          │  HTTP
                          ▼
   jarvis/api/          ← HTTP layer: routes, request/response models, auth wiring
       │                        (jarvis/api/app.py ~5,100 lines — the one oversized module)
       ▼
   jarvis/core/         ← domain: planner, orchestrator, models, policy, audit, registry
       │                 (jarvis/core/planner.py ~3,860 lines — the other oversized one)
       ▼
   jarvis/tools/  jarvis/gateway/  jarvis/security/  jarvis/integrations/  jarvis/db/
       │                 capability modules: tool registry, providers, auth, adapters
       ▼
   jarvis/db/, jarvis/memory/, jarvis/voice/     persistence + platform integrations

   jarvis/companion/   ← device gateway (separate app, own auth: PSK + session tokens)
   jarvis/runtime/     ← process lifecycle (RuntimeSupervisor) — currently a DEAD PATH
```

**Measured cross-layer imports (the only ones that exist):**

| From                       | To                         | Note                                                                                                                                                                                                                                                                                                                                                                                                                      |
| -------------------------- | -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `jarvis.core.orchestrator` | `jarvis.api.app`           | **The one layering violation.** The core reaches into the API layer for `_surface_of` / `_is_session_revoked`. It is a _deferred import inside a function_ (so it does not break at import time) but it is still core→api. **Fix direction:** move those two helpers into a lower layer (`jarvis/security/` or `jarvis/companion/`) and have the API import them. Not done yet — recorded as the next architectural step. |
| `jarvis.cli`               | `jarvis.core.*`            | allowed (CLI is a client of the core)                                                                                                                                                                                                                                                                                                                                                                                     |
| `jarvis.config`            | `jarvis.gateway.selection` | allowed (config resolves the provider selection overlay)                                                                                                                                                                                                                                                                                                                                                                  |
| `jarvis.core.planner`      | `jarvis.tools.*`           | allowed (the planner routes to the registry)                                                                                                                                                                                                                                                                                                                                                                              |
| `jarvis.gateway.router`    | `jarvis.gateway.providers` | allowed (within a layer)                                                                                                                                                                                                                                                                                                                                                                                                  |

---

## 2. Ownership by responsibility

| Responsibility                                          | Owning module                                         | Key files                                                                                                                               | Public interface                                               |
| ------------------------------------------------------- | ----------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------- |
| **HTTP routes, request/response models, error mapping** | `jarvis/api/`                                         | `app.py` (routes), **`schemas.py` (request bodies — NEW, iter 37)**                                                                     | `app` (FastAPI instance), `jarvis.api.schemas.*Body`           |
| **Intent parsing / rule chain**                         | `jarvis/core/planner.py`                              | `planner.py`                                                                                                                            | `RulePlanner.parse(text) -> Plan`                              |
| **Conversation orchestration, model handoff**           | `jarvis/core/orchestrator.py`                         | `orchestrator.py`                                                                                                                       | `JarvisCore.handle(request)`, `resolve_confirmation(...)`      |
| **Domain models**                                       | `jarvis/core/models.py`                               | `models.py`                                                                                                                             | `CommandRequest`, `CommandResponse`, `Plan`, `Verification`    |
| **Permissions, approvals, verification semantics**      | `jarvis/security/`                                    | `policy.py`, `risk.py`, `audit.py`                                                                                                      | `PolicyEngine.evaluate/resolve`, `ActionStatus`                |
| **Authentication (token store, sessions)**              | `jarvis/security/auth.py`                             | `auth.py`                                                                                                                               | `TokenStore`, `get_token_store()`                              |
| **Provider configuration + credentials**                | `jarvis/gateway/` + `jarvis/security/providers.py`    | `providers.py`, `selection.py`, `router.py`                                                                                             | `select_provider(settings)`, `get_stt`, `ModelRouter.complete` |
| **Tool registry + execution**                           | `jarvis/tools/` (30 modules)                          | `base.py` (`ok`/`unverified`/`fail`), one module per capability                                                                         | `build_default_registry()`, `ToolSpec`                         |
| **Persistence**                                         | `jarvis/db/`, `jarvis/memory/`                        | `database.py`, `store.py`, `embeddings.py`                                                                                              | `get_db()`, `MemoryStore`                                      |
| **Calendar / email adapters**                           | `jarvis/integrations/`                                | `calendar_provider.py`, `email_provider.py`                                                                                             | `get_calendar_provider()`, provider ABCs                       |
| **Attachment processing**                               | `jarvis/attachments/` (NEW iter 52), `jarvis/vision/` | `pdf.py`, `image.py`, `ocr.py`                                                                                                          | `extract_text()`, `inspect()`                                  |
| **Voice in/out**                                        | `jarvis/voice/`                                       | `stt.py`, `tts.py`, `session.py`                                                                                                        | `get_stt()`, `VoiceSession`                                    |
| **Device gateway (phone)**                              | `jarvis/companion/`                                   | `gateway.py`, `server.py`                                                                                                               | `build_companion_app()`                                        |
| **Process lifecycle**                                   | `jarvis/runtime/`                                     | `supervisor.py`                                                                                                                         | `RuntimeSupervisor` — **DEAD PATH today** (see §4)             |
| **Dashboard (web UI)**                                  | `dashboard/`                                          | `index.html`, `ai_chat.html`, `ai_connections.html`, `mission_control.html`, `phone.html`, `planner.html`, `operator.html`, `work.html` | HTTP only; never imports Python                                |
| **Native macOS app**                                    | `macos/Vantage/`                                      | `Sources/APIClient/`, `Sources/Services/BackendManager.swift`, `Sources/Views/`                                                         | `APIClient`, `BackendManager`                                  |
| **Android companion**                                   | `android/`                                            | `data/api/`, `ui/`                                                                                                                      | `VantageApiClient`                                             |
| **iOS companion**                                       | `ios/VantageAI/`                                      | —                                                                                                                                       | **PAUSED** (project CLAUDE.md)                                 |

---

## 3. Safe-to-change-together

| Change                        | Files that must move together                                                           | Must NOT touch                                                |
| ----------------------------- | --------------------------------------------------------------------------------------- | ------------------------------------------------------------- |
| A new API route               | `jarvis/api/app.py` (+ `schemas.py` if it adds a body)                                  | `jarvis/core/*` (business rules belong there)                 |
| A new tool                    | `jarvis/tools/<capability>.py` + `jarvis/tools/__init__.py` (registry)                  | `jarvis/api/*` (the registry is how the API finds tools)      |
| A provider adapter            | `jarvis/gateway/providers.py` (+ `selection.py` for config)                             | `jarvis/core/planner.py`                                      |
| Verification semantics        | `jarvis/tools/base.py` (`ok`/`unverified`) **and** every caller that passes `verified=` | any UI file — the API already maps `verification`             |
| The macOS app's network layer | `macos/Vantage/Sources/APIClient/`                                                      | `Sources/Views/*` (views consume `APIClient`, never raw URLs) |
| The dashboard's key form      | `dashboard/index.html` + the backend's `/ai/providers/*/store` route                    | `jarvis/security/providers.py` (the Keychain path)            |

---

## 4. Known ownership defects (recorded, with the next step)

1. **`jarvis/core/orchestrator.py` → `jarvis/api/app.py`** (the only layering violation).
   _Next step:_ move `_surface_of` / `_is_session_revoked` into `jarvis/security/` and have
   `jarvis.api.app` import them; the core then imports downward only.
2. **`jarvis/api/app.py` is ~5,100 lines / 167 routes.** Extractions done so far:
   request bodies → `jarvis/api/schemas.py` (iter 37). _Next step:_ move the
   `/ai/providers/*` route group into `jarvis/api/routes/providers.py` **and import it**
   (the earlier attempt at that file was never imported — that dead copy was removed in
   iter 37; `audit/PHASE72_PROGRESS_REPORT.md:49` shows the extraction was planned and
   never finished).
3. **`jarvis/core/planner.py` is ~3,860 lines.** The rule chain is one long list; splitting it
   by capability (files/calendar/email/browser/system) would let those parts be owned
   separately. _Next step:_ after the API layer is split, extract the rule groups.
4. **`jarvis/runtime/supervisor.py` is a DEAD PATH.** The Tk launcher that used it was
   retired; `build_supervisor` in `app/launcher.py` always returns `None`, and
   `jarvis/runtime/menubar.py` (its only consumer) is orphaned. _Next step:_ an owner
   decision — either wire the supervisor into the native app's lifecycle (it owns the
   degraded-state semantics) or delete the dead path. **Not done unilaterally** (it is a
   design decision, and the DEGRADED fix lives there).
5. **`jarvis/api/app.py` still holds ~150 route handlers**, so "the API layer" is one file
   rather than a directory of focused modules. _Next step:_ as above, one route group at a
   time, each with its own contract test.

---

## Method note

The import graph was measured with a script that parses every `import`/`from … import` in
`jarvis/**/*.py` and resolves relative imports to absolute module paths; the counts and
edges quoted above come from that run, not from reading files by eye. Re-run it after any
move to confirm the layering claims still hold.
