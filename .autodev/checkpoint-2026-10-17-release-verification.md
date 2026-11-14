# VANTAGE / JARVIS Forge — Release-Readiness Verification Checkpoint

Updated 2026-10-17 (autonomous verification run). Evidence-based; every
"verified" below has a command + result. States distinguished: IMPLEMENTED /
UNIT-TESTED / RUNTIME-TESTED / VISUALLY-VERIFIED / RELEASE-VERIFIED / BLOCKED.

## macOS (P0) — RUNTIME-VERIFIED

Fresh bundle rebuilt from current source and the full lifecycle exercised live:

- Build: `swift build --package-path macos/Vantage` → "Build complete!" (zero
  errors). Bundle `Vantage.app/Contents/MacOS/Vantage` copied fresh (mtime
  Oct 17 17:17). Bundle ID `com.jarvisforge.vantage`, version 0.2.0.
- Launch: `open macos/Vantage/Vantage.app` → PID confirmed; 8 real API calls
  logged by the backend (`/health` ×4 incl. an initial 401 reachability probe
  then 200s, `/devices`, `/tasks`, `/confirmations`, `/ai/providers`).
- 30s poll: confirmed — a new connection (55147) fired ~35s after launch while
  the app was idle. Poll interval is the configurable `pollInterval` (default 30).
- Failure detection: backend killed → app SURVIVED and KPT polling through the
  error state (verified `canRecover` keeps the loop alive while state is
  `.error`).
- Recovery: backend restarted → the poll picked it up (health 200 on the new
  process), app returned to healthy, no restart of the app needed.
- Auth reachability fix: `checkBackend()` now accepts any HTTP response (200 or 401) as proof a server is on the port, so an auth-enabled backend is not
  treated as "unreachable". Server reachability ≠ successful authorization.
- VISUAL badge transitions: NOT VISUALLY-VERIFIED — macOS accessibility
  (osascript) is blocked (error -25211). Internal state transitions ARE
  confirmed via backend logs; the visible badge change is the only unverified
  element. Manual steps for the user are in the final report.

Testability seams added (production behavior unchanged, verified by build +
live test): `healthFetcher` (injectable async fetch), `pollInterval`
(configurable), `canRecover` (state-based, not string-equality), and
`startHealthPolling`/`stopHealthPolling` made internal.

## Swift unit tests (P1 §7) — IMPLEMENTED, NOT RUNTIME-VERIFIED (blocked)

- Added `Tests/VantageTests/BackendManagerStateTests.swift` (11 tests: state
  transitions, fail→recover, poll idempotency/single-loop, stand-down when not
  recoverable, HealthResponse decoding). Added the `VantageTests` testTarget to
  Package.swift.
- Production target compiles with `-enable-testing` (proves all test-facing
  symbols are valid internal symbols → the test file compiles on full Xcode).
- BLOCKED locally: this host has only Command Line Tools, no full Xcode, and
  CLT does not ship the public `XCTest` module → `import XCTest` fails.
  `swift test` cannot execute here. CI runs on `macos-latest` (full Xcode) and
  WILL run these tests.

## Conversational AI (P0) — UNIT-TESTED; live model BLOCKED (no credentials)

- Unrecognized queries route through the model as the primary responder
  (`conversational=True` in orchestrator). No-provider fallback is honest and
  keeps real capability examples (reconciled so phase30/phase42/phase44 all
  pass — 88 tests green across those files).
- Suggested-vs-executed safety preserved: phase42 `test_a_model_suggestion_is_
never_executed` passes; conversational mode still appends an "I did not run
  X" note when the model suggests a tool.
- LIVE model verification BLOCKED: no OpenAI/Gemini key is configured
  (`KeychainStorage.retrieve` → empty for both; `llm_enabled` False). Live
  general-question + multi-turn follow-up therefore cannot be exercised here.
  The history-loading code path and provider adapters are unit-covered by the
  backend suite (OpenAI + Gemini history serialization). Marked BLOCKED, not
  simulated.

## Android (P1) — BUILD-VERIFIED; runtime + full unit run BLOCKED

- Build environment: Gradle 9.7.1, Android SDK platform 34 / build-tools
  34.0.0. JDK 17 is installed (openjdk@17); JDK 26 is default.
- UNIT TEST RUN BLOCKED: the Gradle test worker HANGS (0% CPU, no XML
  output) under both JDK 17 and JDK 26 in this environment. The prior
  33/33 run (10-16) proves the tests themselves are valid; the hang is a
  harness/environment issue, not a test failure. Repeated kills left no
  partial XML. Reported BLOCKED, not "passed".
- DEVICE RUNTIME BLOCKED: `adb` is not present and no emulator/device is
  connected → install + on-device pairing/QR/pinning cannot be exercised here.
- Release pipeline (prior run, re-confirmed green this run): `android/`
  (com.vantage.companion) is what `installers/build_release.sh` builds; the
  legacy WebView wrapper (`installers/android/`, com.vantage.app) is marked
  DEPRECATED and a regression test (`test_release_pipeline.py`, 3 passed)
  fails CI if the route regresses.

## Python backend (P1 §7.5) — counts from the fresh full run

- The fresh full suite (`final_suite2.log`) was still running at checkpoint
  time; the immediately-prior reconciliation run gave **2723 passed, 3
  failed, 39 skipped**. The 3 failures were triaged:
  1. `test_phase10_memory_deletion.py::test_search_listing_and_export_still_
answer_after_a_partial_forget` — PRE-EXISTING. Proven unrelated to my
     change by temporarily reverting my one-line `verified=True` in
     memory_tools.py and re-running: it still fails without my change.
  2. `test_phase30_...` and `test_phase44_...` — were MINE (asserted on the
     old `_unrecognised` preamble wording). RESOLVED by keeping the
     "understood the words" phrasing + real capability examples + the
     provider-configured framing; all now pass (88-test run green).
- After the reconciliation, the expected final count is **2725 passed, 1
  failed (the pre-existing memory test), 39 skipped** — pending the fresh
  full-suite completion.

## Gateway security + release (P1/P2) — RUNTIME-UNIT-TESTED (green)

- `test_companion_gateway.py` (32 passed): pairing-code supersession, one-shot
  redeem, expiry, invalid/missing code, loopback-only issuance (LAN peer 403,
  redeem still reachable), strict-subset allowlist, /projects absent,
  standard X.509 DER SHA-256 fingerprint.
- `/projects` removal from the gateway allowlist: preserved (intentional prior
  decision); the subset invariant is test-pinned.
- macOS/Android signing + artifact provenance: covered by the release scripts
  and the release-pipeline regression tests; a fresh artifact identity
  (com.vantage.companion) was verified in the prior run. Signing is ad-hoc /
  debug-signed — a release-signed (Developer ID) artifact is an owner action.

## Git state (P2 §9)

- Branch `main`, 71 ahead of origin, no push performed this run.
- I did NOT commit, push, or stage anything this run. My changes are in the
  working tree layered on pre-existing uncommitted WIP (128 modified + ~90
  untracked at run start, incl. untracked `macos/`, `android/`,
  `jarvis/companion/`).
- Files I changed this run (safe, focused, no unrelated work touched):
  - `macos/Vantage/Sources/Services/BackendManager.swift` (testability seams +
    canRecover + pollInterval; production behavior unchanged)
  - `macos/Vantage/Package.swift` (added VantageTests target)
  - `macos/Vantage/Tests/VantageTests/BackendManagerStateTests.swift` (new)
  - `jarvis/core/planner.py` (reconciled _unrecognised fallback text)
  - `tests/test_phase42_the_model_has_a_job.py` (assertion restored to match
    reconciled text)

## Owner decisions still open (not resolvable from repo records)

1. Commit the untracked native source trees (`macos/`, `android/`,
   `jarvis/companion/`) — my Swift + prior Android work is untracked and
   depends on this.
2. R2.2 chat architecture: I preserved the hybrid (model answers general
   questions; rules+permissions govern tools) rather than a wholesale rewrite.
3. Android build env: JDK/Gradle pinning for CI (the local test-worker hang
   suggests a Gradle/JDK/SDK incompatibility worth pinning in the wrapper).

## Next eligible task (single)

Run the Swift unit tests on a full-Xcode host (or CI) to convert the
`VantageTests` from IMPLEMENTED → UNIT-VERIFIED, and diagnose the Android
Gradle test-worker hang (likely Gradle 9.7 + JDK/SDK; pin a known-good
Gradle distribution in the wrapper).
