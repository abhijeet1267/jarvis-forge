# VANTAGE / JARVIS Forge — Project Roadmap

Prioritized objectives with acceptance criteria. Priority reflects product
impact × verified risk. Statuses: **open** / **in progress** / **done** /
**blocked** / **deferred**. The durable machine-readable mirror is
`.autodev/tasks.json` — keep the two in agreement.

## Tier 0 — Integrity & foundations (do not regress)

- **R0.1 Durable dev infrastructure** — done (2026-10-16).
  CLAUDE.md, this roadmap, PROJECT_STATUS, ARCHITECTURE, AUTODEV_RUNBOOK,
  `.autodev/` queue + checkpoints, supervisor with verified start/status/stop/
  resume, deadline enforcement, login-agent scripts.
- **R0.2 Green test baseline** — in progress (9/9 verified failures fixed
  2026-10-16; awaiting full-suite confirmation).
  Acceptance: full `pytest` run with 0 failures; every skip has a named
  external prerequisite; counts re-measured and recorded in PROJECT_STATUS.
- **R0.3 Preserve the canonical Android tree** — open.
  `android/` (native Compose, `com.vantage.companion`) is untracked by git.
  Acceptance: it is intentionally reviewed and added to git (or a written
  decision to keep it untracked is recorded). No `git clean` may ever remove it.

## Tier 1 — The security surface that lets a second device in

- **R1.1 Gateway test coverage** — done (2026-10-16): 31 tests in
  `tests/test_companion_gateway.py`; pinned the subset invariant, one-shot
  pairing codes, supersession, session expiry/revocation, proxy allowlist
  denial, master-token forwarding, device identity.
- **R1.2 Pairing-code supersession** — done (2026-10-16): `issue_pairing_code`
  now invalidates prior codes for the same surface, matching its docstring.
- **R1.3 Wire the gateway into the backend lifecycle** — open, high impact.
  `jarvis/companion/gateway.build_companion_app` is never started by any
  process. Acceptance: a documented command (and the macOS app) starts it on
  demand; on main-backend shutdown it is stopped; `/companion/health` answers;
  cert + fingerprint stable across restarts; tests cover start/stop.
- **R1.4 Native Android client speaks the gateway contract** — open.
  Currently: scrapes `jarvis-bootstrap` from `GET /phone`, posts
  `/auth/session`, `DELETE /auth/session`, and `POST /companion/pairing-code`
  (a _Mac_ endpoint) — a hybrid that will not work once the gateway is the
  pairing path. No TLS cert pinning despite `certificate_fingerprint` being
  served. Acceptance: QR/manual entry → `POST /companion/pairing-redeem` →
  store scoped token (already EncryptedSharedPreferences) → OkHttp
  CertificatePinner on the served fingerprint → allowed paths 200, denied
  paths 403, expired/reused codes 401/403 — verified against a real gateway
  (instrumented test or emulator).
- **R1.5 Release pipeline ships the native companion** — open, high impact.
  `installers/build_release.sh` builds `installers/android/` (legacy WebView
  wrapper, `com.vantage.app`). Acceptance: `android/build_apk.sh` exists and
  builds `com.vantage.companion`; `build_release.sh` calls it; artifact
  identity verified (aapt dump badging) before "success"; legacy tree retired
  or clearly marked.

## Tier 2 — Real conversational assistant

- **R2.1 Multi-turn conversation memory** — open, top product gap.
  Neither chat path passes history to the model. Acceptance: `/ai/chat/send`
  threads prior turns (bounded window) from its `conversation_id`; `/command`
  fallback advice includes the last N relevant turns; tests assert the model
  receives history; follow-up questions about earlier answers resolve.
- **R2.2 Unify the two chat surfaces** — open.
  `/command` (rules-first, tools+permissions) vs `/ai/chat/send` (model, no
  tools). Acceptance: a single documented decision on which is primary,
  implemented with model-proposed actions gated by `PermissionEngine`; the
  other path either routes into it or is removed; no model-bypass of
  approvals (proven by a test where a model-suggested DESTRUCTIVE action
  requires confirmation).
- **R2.3 Model-planned actions** — open (after R2.2).
  Today only 54 regex rules can trigger tools. Acceptance: model can propose a
  structured plan → validated (params, paths, risk) → permission decision →
  execution with the existing tri-state verification; negative tests for
  invalid/over-privileged proposals.
- **R2.4 Default-UNKNOWN verification for effect tools** — open.
  `ok()` defaults to SUCCESS; world-effect tools must opt in. Acceptance:
  `tools/base.ok()` gains an explicit `verified` argument defaulting to
  unverified for write-side tools; affected tools updated and tested; replies
  read honestly ("done, unverified" vs "done, checked").

## Tier 3 — macOS app reliability (audit-verified defects)

- **R3.1 Service lifecycle** — open. `ContentView.onDisappear` kills
  backend+gateway; `connect()` not idempotent (double-spawn + orphan);
  SIGTERM goes to `/usr/bin/env` not the Python child. Acceptance: process
  group kill verified (no listener on :8765 after quit); reconnect is safe to
  call twice; window minimize/Space-switch does not disconnect (or is an
  explicit documented choice).
- **R3.2 Destructive-action confirmations** — open. "Delete All Data", session
  revoke, task cancel have no confirmation alert. Acceptance: every
  consequential action shows a confirmation naming what will happen; tests /
  UI pass.
- **R3.3 Honest loading/error states** — open. HomeView silent catches,
  SelectionInfoView stuck "Loading…", Settings empty catches, Export Data
  fetches-then-discards. Acceptance: each view shows error or retry, no
  spinner-forever, export actually exports (or is removed).
- **R3.4 Version from Info.plist** — open. Hardcoded `Text("Version 0.2.0")`
  drifts. Acceptance: `CFBundleShortVersionString` read at runtime.
- **R3.5 Percent-encode URL paths** — open (latent). `APIClient.makeRequest`
  interpolates IDs raw. Acceptance: `addingPercentEncoding` applied; a
  test with a non-ASCII id round-trips.

## Tier 4 — Workflows (build as vertical slices, one at a time)

- **R4.1 Calendar** — open. "add my task to the calendar" end-to-end with a
  real backend calendar store; verify the event row exists after create.
- **R4.2 Email** — open. Summarize recent, search by criteria, save an
  attachment to an authorized path with verification; send/reply only with
  explicit approval.
- **R4.3 Browser workflow** — open. playwright dep exists but is not in the
  venv; add `playwright install chromium` to setup docs; slice: open URL →
  read title/content → save summary to authorized file, verified.

## Tier 5 — Build & release quality

- **R5.1 Fail-loud signing** — open. `build_app.py:182` and `build_dmg.sh:51`
  swallow codesign failure. Acceptance: signing failure = build failure (or an
  explicit, logged, opt-in ad-hoc mode).
- **R5.2 Artifact provenance** — open. Record build sha + timestamp +
  checksum in the manifest; `scripts/release_verify.sh` verifies it.
- **R5.3 CI** — open. `.github/workflows/ci.yml` runs no Android/Gradle job;
  add one (or a documented deferral) once R1.5 lands.

## Explicitly out of scope

- iOS work (paused; sources preserved).
- Pushing to origin / publishing (only on explicit user authorization).
- WIDening any phone/companion allowlist (the gateway is a strict subset by
  design; R1.4 aligns the client, not the surface).
