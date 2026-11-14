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
- **R0.2 Green test baseline** — done (2026-10-16; re-measured 2026-10-22
  at 3019 passed / 18 skipped / 0 failed with a backend running).
  Acceptance: full `pytest` run with 0 failures; every skip has a named
  external prerequisite; counts re-measured and recorded in PROJECT_STATUS.
- **R0.3 Preserve the canonical Android tree** — **half closed 2026-10-27: the
  decision is recorded below; the git-add itself remains the owner's.**
  `android/` (native Compose, `com.vantage.companion`) is untracked by git.
  Acceptance: it is intentionally reviewed and added to git (or a written
  decision to keep it untracked is recorded). No `git clean` may ever remove it.

  **Recorded decision (2026-10-27).** It stays untracked for now, deliberately:
  adding it is a commit, and the repository is 71 commits ahead of origin with
  173 modified and 191 untracked paths already in the working tree — a staging
  decision the owner has not made. What must not happen is losing it, so:

  - It is **not** in `.gitignore` and must not be. Ignoring it would make it
    invisible to `git status` — the opposite of protection — and would not stop
    `git clean -fdx` either.
  - `git clean -fd` (or `-fdx`) **would delete 1,380 untracked files** under
    `android/` (80 excluding `build/`). No agent may run it. This is already a
    standing rule in `CLAUDE.md`; it is restated here because this is the tree
    it protects.
  - The canonical sources are the ones under `android/app/src/`; `build/` is
    generated. A future backup, if the owner wants one, should copy that
    directory somewhere under version control rather than rely on the working
    tree surviving.

  Verified on 2026-10-27: `git ls-files android/` → 0; `git check-ignore
android/` → nothing; `git status --porcelain -uall android/` → 1,380 paths;
  build and 92 unit tests pass with JDK 17. The remaining half — the actual
  `git add` — needs the owner.

## Tier 1 — The security surface that lets a second device in

- **R1.1 Gateway test coverage** — done (2026-10-16): 31 tests in
  `tests/test_companion_gateway.py`; pinned the subset invariant, one-shot
  pairing codes, supersession, session expiry/revocation, proxy allowlist
  denial, master-token forwarding, device identity.
- **R1.2 Pairing-code supersession** — done (2026-10-16): `issue_pairing_code`
  now invalidates prior codes for the same surface, matching its docstring.
- **R1.3 Wire the gateway into the backend lifecycle** — done (2026-10-16).
  `jarvis/companion/gateway.build_companion_app` is never started by any
  process. Acceptance: a documented command (and the macOS app) starts it on
  demand; on main-backend shutdown it is stopped; `/companion/health` answers;
  cert + fingerprint stable across restarts; tests cover start/stop.
- **R1.4 Native Android client speaks the gateway contract** — done (2026-10-16);
  device-level pairing still needs hardware.
  Currently: scrapes `jarvis-bootstrap` from `GET /phone`, posts
  `/auth/session`, `DELETE /auth/session`, and `POST /companion/pairing-code`
  (a _Mac_ endpoint) — a hybrid that will not work once the gateway is the
  pairing path. No TLS cert pinning despite `certificate_fingerprint` being
  served. Acceptance: QR/manual entry → `POST /companion/pairing-redeem` →
  store scoped token (already EncryptedSharedPreferences) → OkHttp
  CertificatePinner on the served fingerprint → allowed paths 200, denied
  paths 403, expired/reused codes 401/403 — verified against a real gateway
  (instrumented test or emulator).
- **R1.5 Release pipeline ships the native companion** — done (2026-10-16).
  `installers/build_release.sh` builds `installers/android/` (legacy WebView
  wrapper, `com.vantage.app`). Acceptance: `android/build_apk.sh` exists and
  builds `com.vantage.companion`; `build_release.sh` calls it; artifact
  identity verified (aapt dump badging) before "success"; legacy tree retired
  or clearly marked.

## Tier 2 — Real conversational assistant

- **R2.1 Multi-turn conversation memory** — done (2026-10-16).
  Neither chat path passes history to the model. Acceptance: `/ai/chat/send`
  threads prior turns (bounded window) from its `conversation_id`; `/command`
  fallback advice includes the last N relevant turns; tests assert the model
  receives history; follow-up questions about earlier answers resolve.
- **R2.2 Unify the two chat surfaces** — done (2026-10-21): one `ConversationService`
  behind both surfaces, and the chat surface can act through the same gate.
  `/command` (rules-first, tools+permissions) vs `/ai/chat/send` (model, no
  tools). Acceptance: a single documented decision on which is primary,
  implemented with model-proposed actions gated by `PermissionEngine`; the
  other path either routes into it or is removed; no model-bypass of
  approvals (proven by a test where a model-suggested DESTRUCTIVE action
  requires confirmation).
- **R2.3 Model-planned actions** — done (2026-10-21): the model returns a structured
  proposal, which is validated and then run through the existing permission gate.
  Today only 54 regex rules can trigger tools. Acceptance: model can propose a
  structured plan → validated (params, paths, risk) → permission decision →
  execution with the existing tri-state verification; negative tests for
  invalid/over-privileged proposals.
- **R2.4 Default-UNKNOWN verification for effect tools** — done (2026-10-20): the
  original claim was stale — `ok()` already defaulted to UNKNOWN.
  `ok()` defaults to SUCCESS; world-effect tools must opt in. Acceptance:
  `tools/base.ok()` gains an explicit `verified` argument defaulting to
  unverified for write-side tools; affected tools updated and tested; replies
  read honestly ("done, unverified" vs "done, checked").

## Tier 3 — macOS app reliability (audit-verified defects)

- **R3.1 Service lifecycle** — done (2026-10-16). `ContentView.onDisappear` kills
  backend+gateway; `connect()` not idempotent (double-spawn + orphan);
  SIGTERM goes to `/usr/bin/env` not the Python child. Acceptance: process
  group kill verified (no listener on :8765 after quit); reconnect is safe to
  call twice; window minimize/Space-switch does not disconnect (or is an
  explicit documented choice).
- **R3.2 Destructive-action confirmations** — done (2026-10-16). "Delete All Data", session
  revoke, task cancel have no confirmation alert. Acceptance: every
  consequential action shows a confirmation naming what will happen; tests /
  UI pass.
- **R3.3 Honest loading/error states** — done (2026-07-20, runtime-verified 2026-10-21). HomeView silent catches,
  SelectionInfoView stuck "Loading…", Settings empty catches, Export Data
  fetches-then-discards. Acceptance: each view shows error or retry, no
  spinner-forever, export actually exports (or is removed).
- **R3.4 Version from Info.plist** — done (2026-10-16). Hardcoded `Text("Version 0.2.0")`
  drifts. Acceptance: `CFBundleShortVersionString` read at runtime.
- **R3.5 Percent-encode URL paths** — done (2026-10-16). `APIClient.makeRequest`
  interpolates IDs raw. Acceptance: `addingPercentEncoding` applied; a
  test with a non-ASCII id round-trips.

## Tier 4 — Workflows (build as vertical slices, one at a time)

- **R4.1 Calendar** — done (2026-10-21). "add my task to the calendar" end-to-end with a
  real backend calendar store; verify the event row exists after create.
- **R4.2 Email** — done (2026-10-21) for search/read/summarise/draft;
  a live CalDAV/SMTP account is still an owner credential dependency. Summarize recent, search by criteria, save an
  attachment to an authorized path with verification; send/reply only with
  explicit approval.
- **R4.3 Browser workflow** — done (2026-10-21), plus R4.4 (save to file). playwright dep exists but is not in the
  venv; add `playwright install chromium` to setup docs; slice: open URL →
  read title/content → save summary to authorized file, verified.

## Tier 5 — Build & release quality

- **R5.1 Fail-loud signing** — done (2026-10-16). `build_app.py:182` and `build_dmg.sh:51`
  swallow codesign failure. Acceptance: signing failure = build failure (or an
  explicit, logged, opt-in ad-hoc mode).
- **R5.2 Artifact provenance** — done (2026-10-16). Record build sha + timestamp +
  checksum in the manifest; `scripts/release_verify.sh` verifies it.
- **R5.3 CI** — done (2026-10-16). `.github/workflows/ci.yml` runs no Android/Gradle job;
  add one (or a documented deferral) once R1.5 lands.

## Tier 6 — Client surfaces that were never run

A surface that was written, contract-tested and never exercised looks exactly
like a surface that works. Two dashboard pages in this tier were entirely
broken and nothing said so.

- **R8.1 Drive the dashboard's other pages in a real browser** — done (2026-10-22).
  `ai_chat.html` was driven in a browser (R7.2) and three of its defects were
  found that way. The other five pages had never been run. Acceptance: every
  served page is loaded in a real browser against a real backend; each fix has
  a regression test that fails against the pre-fix code.
  - `mission_control.html` could not authenticate (posted `{code}`, read
    `data.token`), read four fields the server never sends, and called two
    routes that do not exist. Every key save and delete failed.
  - `phone.html` assigned and read four variables it never declared, so its
    four-second poll died on its first statement and the page made **zero**
    `/devices` and `/confirmations` requests.
  - `ai_settings.html` is an untracked pre-Phase-72 prototype with no route;
    left on disk, removed from the ownership doc, asserted unreachable.

## Tier 7 — The app talks back like an assistant

- **R8.2 The macOS Command Center is a conversation** — done (2026-10-22).
  The screen sent every message as a brand new conversation (the request
  hardcoded `session_id = nil`, and the backend mints a fresh `SESS-` id per
  request), and its response model read four fields the server never sends.
  Acceptance: a general question is answered by the configured provider
  through the real path; a follow-up resolves against the earlier answer; a
  new conversation does not inherit the old one's history; tool requests
  still go through the gated path and consequential actions are still held;
  the transcript is visibly better; the app is built, launched and the path
  exercised. Details: `docs/AUTONOMOUS_ENGINEERING_LOG.md` iteration 74.

- **R8.3 The router must not send a model the vendor cannot serve** — done
  (2026-10-22). The role models default to OpenAI names, so an install
  pointed at a gateway sent `o4-mini` to a vendor that had never heard of
  it; the conversational path hid it, and the model planner — every
  model-proposed action — died at the first request. Acceptance: a role
  model the vendor does not publish is never called; a gateway with no
  catalogue is not asked again for a model it already refused; a refusal of
  the model name is retried once on the configured model; a timeout is
  **not** retried on another model. Details: iteration 76.

- **R8.4 The AI connections page must describe the providers the server
  has** — done (2026-10-22). The page offered a provider the backend has
  never had and had no card for the one in use; the dropdown could not be
  changed; three checkboxes did nothing. Acceptance: the cards come from
  `GET /ai/providers`; the dropdown holds the user's choice; Save appears
  only after a test passes; the key round trip works in a real browser.
  Details: iteration 76.

- **R8.5 Every app screen's model must match the response it fetches** —
  done (2026-10-22). `AuditEntry.detail` was a `String?` where the server
  sends an object, so the whole `/audit` array failed to decode and the
  Activity screen's audit list was empty; the device, task, project and
  session models had the same class of drift. Acceptance: each model
  decodes the real response; the fields a view reads are fields the server
  sends; the outcome vocabulary the view knows is the server's own; the
  screens are seen in the running app. Details: iteration 76.

## Explicitly out of scope

- iOS work (paused; sources preserved).
- Pushing to origin / publishing (only on explicit user authorization).
- WIDening any phone/companion allowlist (the gateway is a strict subset by
  design; R1.4 aligns the client, not the surface).
