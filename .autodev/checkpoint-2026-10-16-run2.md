# Session checkpoint — 2026-10-16 (second autonomous run)

## What this run verified and did

- **Android native app now COMPILES and BUILDS** (was ~26 errors).
  - `:app:compileDebugKotlin` clean; `:app:assembleDebug` → 18MB APK.
  - `android/build_apk.sh` run end-to-end: aapt2 confirms package
    `com.vantage.companion` (NOT the legacy `com.vantage.app` WebView wrapper),
    writes `dist/Vantage-1.0-android-debug.apk` + `ANDROID_RELEASE_MANIFEST.json`
    (commit/timestamp/sha256).
- **Backend full suite green after all changes: 2726 passed, 39 skipped, 0
  failed** (380s). New tests: `test_release_pipeline.py` (3), standard
  X.509 fingerprint regression, gateway loopback-issuance guard.
- **Runner re-verified live**: status-with-no-run, resume-of-stopped refuses,
  resume-of-complete refuses, start-while-active refuses. No run invention.

## Security fixes (backend) this run

- `jarvis/companion/cert.fingerprint` now returns the **standard X.509 SHA-256
  of the DER** (was a hash of the PEM _file bytes_ — a value no TLS client can
  reproduce, so pinning was meaningless). Regression test pins it.
- `POST /companion/pairing-code` is now **loopback-only** (closes the
  self-pairing hole: any LAN device could previously issue-and-redeem its own
  code). `redeem` stays LAN-reachable. ASGI-level test proves LAN 403 + redeem
  still works.

## Android contract work (R1.4)

- Models rewritten to the **real** backend wire shapes (verified against live
  payloads): `/command` is a pipeline trace `{reply, decision, actions[],
needs_confirmation}`; `/devices` rows have NO `id`/`state`/`surface` on the
  wire (computed views over `auth_state`/`type`); confirmations carry
  `confirmation_id`/`tool`/`params`/`risk:int`/`preview`/`state`; `/health`
  `security`/`privacy`/`companion` are nested mixed-type objects (`JsonObject`).
- Pairing now goes through the **gateway**: `POST /companion/pairing-redeem`
  for a scoped token, over **TLS with X.509 certificate pinning**
  (`PinnedCertificateTrustManager`). The legacy web flow (`/phone` bootstrap →
  `/auth/session`) is GONE from the native client — that endpoint is
  same-origin-protected (403 for non-browser origins), so a native app can
  never use it.
- QR deep link `vantage://pair?host&port&fingerprint&code` wired
  (manifest intent-filter + `MainActivity.handleIntent`); setup screen collects
  host/fingerprint/code and pins before redeem.
- `SessionStore`: added fingerprint persistence + an injected-store
  (in-memory) test seam; production path still EncryptedSharedPreferences.
- Cross-language pinning proof: `pinning-test-cert.pem` fingerprint
  `3DF3 CB13 C0F3 7603 …` matches across openssl, the backend's Python
  `fingerprint()`, and the Kotlin `fingerprintOf`.
- Test suite rewritten to the real contract: `ApiModelsTest`,
  `PinnedCertificateTrustManagerTest`, `VantageApiClientContractTest`
  (MockWebServer), `SessionStoreTest`, + `InMemorySharedPreferences` fake.
  **Unit-test run still in progress at checkpoint time** — verify before
  citing the count: `gradle :app:testDebugUnitTest --console=plain`
  (results → `app/build/test-results/testDebugUnitTest/*.xml`).

## Release pipeline (R1.5) + CI (R5.3)

- `installers/build_release.sh` now builds the NATIVE app via
  `android/build_apk.sh`; legacy wrapper script marked DEPRECATED;
  `tests/test_release_pipeline.py` fails CI if the release path ever points
  back at the WebView wrapper.
- `.github/workflows/ci.yml`: new `android` job (JDK 17,
  `gradle :app:testDebugUnitTest`, upload results).

## R2.4 (default-UNKNOWN verification)

- `jarvis/tools/base.py ok()`: default flipped from SUCCESS to UNKNOWN.
  A bare `ok()` now means "the handler returned without exception" — honest.
  Self-verifying tools (reads, local computation) opt in with `verified=True`.
- 15 initial failures from the default flip; all fixed by adding `verified=True`
  to the specific self-verifying tool handlers: `get_time`, `read_file`,
  `search_memory`, `get_car_status`, `get_battery_status`, `diagram_created`,
  `run_coding_agent`, and the `_finish` helper in design tools.
- `tests/test_phase10_verification_authority.py`: 2 tests updated to match
  the new contract (bare ok = UNKNOWN, verified=True = SUCCESS).
- Full suite result at checkpoint time: **awaiting final run**.
- Remaining next: R3.3 (honest loading/error states in macOS views).

## Git state

- Commits from BOTH runs: `0c37d4d` (infra), `aa1b66a` (build_app signing).
  **Not pushed** (needs owner authorization).
- This run's changes are in the working tree, layered on the owner's
  uncommitted WIP. `macos/`, `android/`, `jarvis/companion/` remain untracked
  (R0.3 — owner's call to commit; a `git clean` would destroy the native app).

## Owner decisions still open

1. Commit the untracked `macos/`/`android/`/`jarvis/companion/` trees?
2. R2.2 chat architecture: model-primary vs rules-first.
3. Android **device-level** pairing verification — no emulator/phone in this
   environment; needs a real device once committed.

## Exact next actions (in order)

1. `gradle :app:testDebugUnitTest --console=plain` — confirm the Android unit
   suite is green; record the count in `docs/PROJECT_STATUS.md` §1.
2. R2.4 (default-UNKNOWN verification for effect tools) — highest-value
   eligible backend task; see `docs/PROJECT_ROADMAP.md`.
3. If the owner commits the trees, push (`git push`) needs explicit
   authorization.

## Traps (re-confirmed)

- `android/` and `macos/` are untracked — never `git clean`.
- Formatter hook auto-reformats `docs/*.md`; re-Read before a 2nd edit.
- No `timeout` binary on this macOS; use the tool timeout param.
- venv is uv-managed (`uv pip install --python .venv/bin/python`), no pip.
