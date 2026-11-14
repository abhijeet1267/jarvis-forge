# VANTAGE / JARVIS Forge — Blocker-Resolution Run Checkpoint

Updated 2026-10-17 (autonomous blocker-resolution run). Supersedes nothing;
appends to `checkpoint-2026-10-17-release-verification.md`. Every "verified"
below has a command + result.

## Blockers resolved this run

### 1. Android Gradle test-worker hang — RESOLVED (root cause: a test bug, not the toolchain)

Root cause, established by a live `jstack` of the hung Test worker (parked
111s at `okhttp3.mockwebserver.MockWebServer.takeRequest`):

- `MockWebServer.takeRequest()` (no-arg) **blocks until a request arrives**.
- Two tests called it **twice after the client made only one request**, so the
  second call waited forever for a request that never came:
  - `VantageApiClientContractTest.fetch_confirmations_hits_phone_surface`
  - `VantageApiClientContractTest.emergency_stop_posts_with_no_body`
- The other three classes (ApiModels 21, Pinned 7, SessionStore 6) do one
  `takeRequest()` per request and always passed in ~10s — confirming the hang
  was this specific test, not Gradle/JDK/SDK config.

Fix (test-only, no assertion weakened): each hanger now does **one**
`takeRequest()` and asserts both `method` and `path` on that single
`RecordedRequest`.

While unblocking, two **real client contract bugs** surfaced and were fixed in
`VantageApiClient.kt` (production code):

- `approveConfirmation`/`denyConfirmation` decoded `/confirm/{id}` as
  `Confirmation`, but the backend returns `response_model=CommandResponse`
  (jarvis/api/app.py). Changed the return type to `CommandResponse` (the UI
  discards the return value, so the change is safe). `ApprovalRepository`
  (dead code, no usages) aligned to match.
- `buildUrl()` ran the whole path (incl. a `?query`) through
  `addPathSegments`, percent-encoding `?` to `%3F` — so
  `/confirm/CONF_1?approve=true` would become `/confirm/CONF_1%3Fapprove=true`
  on a device and never match the backend route. `buildUrl` now splits the
  query string from the path and adds it via `addQueryParameter`.
  (This is the URL-encoding defect the macOS work flagged; the Android side is
  now fixed and test-pinned.)
- The `approvals_use_confirm_path_with_query` test was extended to enqueue the
  follow-up `GET /confirmations` the client makes after each confirm (the
  refresh call would otherwise block `runTest`).

Reproducibility: there was **no Gradle wrapper**, so a bare CI runner had no
`gradle` on PATH. Generated one pinned to **Gradle 8.11.1** (compatible with
AGP 8.7.3, which needs 8.9+) and verified it end-to-end: `./gradlew
:app:testDebugUnitTest` downloaded 8.11.1 and ran.

**Result: full Android unit suite 46/46 pass** in ~14s (system gradle) and
~4m (wrapper, first download). Per class: ApiModels 21, PinnedCertificate 7,
VantageApiClientContract 12, SessionStore 6 — 0 failures, 0 skipped.
APK rebuilt clean, identity `com.vantage.companion` (aapt2), sha256
`fc2151d3…` (new build; the prior `d78929a3…` manifest in dist/ predates these
client fixes and is stale).

### 2. Swift unit tests — IMPLEMENTED; execution still BLOCKED (full Xcode absent)

- `swift build` (production, incl. the BackendManager testability seams)
  passes.
- `swift build --build-tests` fails **only** with `no such module 'XCTest'` —
  the sole error. This host has Command Line Tools, no full Xcode, and CLT
  does not ship the public XCTest module. So the 11 `BackendManagerStateTests`
  cannot execute here; they are IMPLEMENTED and compile-clean against the
  production symbols, but are NOT runtime-verified on this host.
- CI: added a `macos` job (`runs-on: macos-latest` — full Xcode) that builds
  and runs `swift test` and asserts the app binary is produced. This is where
  the tests will execute. NOT run here (no GitHub runner access from this
  host).

### 3. Python regression (`test_phase10_memory_deletion`) — RESOLVED at root cause

The prior run's "pre-existing" proof was **wrong**: it reverted the
`search_memory` line, but the real cause was elsewhere. Actual chain:

- My R2.4 change made `ok()` default to `UNKNOWN`.
- `remember_fact` (memory_tools.py) called `ok(...)` **without** `verified=True`
  → now reported `UNKNOWN`.
- The orchestrator appends ", but I could not verify it happened" to the reply
  for unverified actions (orchestrator.py:1205).
- So the transcript's assistant message gained that caveat, making it NOT a
  substring of the echo the test deletes. `retained["messages"]` then counted
  only the user message (1), but the test correctly expects `>= 2` (both the
  user's utterance and the assistant's confirmation carry the fact).

Fix (a correct classification, not a test weakening): `remember_fact` writes
to the local store and the write is **confirmed** by the returned row with a
real id — a self-verifying write (exactly like `write_file` re-reads and
`search_memory` returns what it queried). Added `verified=True`. This restores
the transcript the privacy test expects and is the honest classification.

**Result: full Python suite 2726 passed, 0 failed, 39 skipped (412s).** The
39 skips are all named external prerequisites (playwright browser, live OpenAI
key, network, macOS permissions, etc.).

## macOS + conversational (re-confirmed, not re-done)

- The health-polling / error-state / recovery / auth-reachability fixes are
  still in `BackendManager.swift` (healthFetcher, pollInterval, canRecover,
  checkBackend). Bundle rebuilt to match current source (mtime current).
- The live lifecycle (launch → connect → 30s poll → survive backend death →
  recover) was RUNTIME-VERIFIED in the prior run and the code is unchanged;
  not re-run this run because no macOS change was made. Visual badge
  transitions remain unverified (GUI/accessibility blocked on this host).
- Conversational safety (phase30/42/44) re-run: green (included in the 131-test
  fast group). Live model still BLOCKED (no API key; not requested).

## Security + release (re-confirmed green)

- `test_release_pipeline.py` 3 passed; `test_companion_gateway.py` 32 passed;
  `test_companion_server.py` (health-polling/state + X.509 fingerprint) green
  (all within the 131-test fast group + full suite).
- APK identity `com.vantage.companion` re-verified after the client fixes.
  Signing remains debug/ad-hoc — Developer ID + Android release-keystore
  signing are owner prerequisites.

## CI changes (`.github/workflows/ci.yml`, YAML validated)

- Added `macos` job: full-Xcode `swift build` + `swift test` + binary check.
- Hardened `android` job: uses the new pinned wrapper (`./gradlew`), builds the
  APK, and asserts package identity via aapt2.

## Git state (this run)

- Branch `main`, 71 ahead of origin, **0 committed / 0 pushed this run**.
- Tracked files changed by me: `jarvis/core/planner.py`,
  `jarvis/tools/memory_tools.py`, `tests/test_phase42_the_model_has_a_job.py`,
  `.github/workflows/ci.yml`.
- Untracked (in untracked trees, my edits): `macos/Vantage/...` (BackendManager
  seams, Package.swift testTarget, BackendManagerStateTests), `android/...`
  (VantageApiClient, ApprovalRepository, VantageApiClientContractTest,
  gradlew + wrapper). Pre-existing untracked trees preserved; nothing staged,
  committed, or pushed.
- No stray processes or ports left (verified: 0 backends, 0 app procs, :8765
  free).

## Remaining owner prerequisites (not resolvable here)

1. Full Xcode (or CI run) to execute the 11 Swift tests — BLOCKED locally.
2. Live model API key for live conversational + multi-turn verification.
3. Android device/emulator for on-device pairing/QR/pinning runtime test.
4. Developer ID + Android release keystore for release signing.
5. Decision to commit the untracked native trees (my work depends on it).

## Next recommended step

Re-run the `macos` CI job (or run `swift test` on a full-Xcode host) to convert
the 11 Swift tests from IMPLEMENTED → VERIFIED, and rebuild the release APK via
`./gradlew :app:assembleDebug` + the release pipeline so the stale
`dist/ANDROID_RELEASE_MANIFEST.json` reflects the fixed client.
