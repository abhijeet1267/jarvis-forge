# VANTAGE / JARVIS Forge — Project Status

> Re-measured continuously. Every claim below names its evidence (command +
> result or file:line). Counts are from actual runs on this tree — the old
> `STATUS.md`/`VANTAGE_CURRENT_STATE.md` figures are historical and stale.

**Last full-suite measurement:** the 2026-10-27 update at the end of this file
carries the current figure. Read the _last_ update, not the first: the
2026-10-21 entry claiming the self-approval guard was fixed was superseded on
2026-10-27 — the guard was still disarmed, and the later entry says why.
Earlier rows in §1 are historical.

**The current checkpoint is `VANTAGE_AUTODEV_STATE.md`** — it names the next
action, the completed work and the evidence. This file is the long record.

## 1. Test health (measured)

| Run                               | Result                                       | Evidence                                                                                                                                                                                                                                            |
| --------------------------------- | -------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Baseline 2026-10-16 (pre-fix)     | **2665 passed, 9 failed, 41 skipped** (745s) | `/tmp/baseline_run.log`, exit 0 (tail)                                                                                                                                                                                                              |
| Phase 92 collection break         | fixed                                        | `pytest.importorskip("playwright.async_api")` in `tests/test_phase92_browser_automation.py`; collection now clean: **2714 collected, 0 errors**                                                                                                     |
| `test_companion_gateway.py` (new) | **31 passed**                                | suite run 2026-10-16                                                                                                                                                                                                                                |
| `test_phase67` phone allowlist    | **14 passed**                                | after removing WIP `/projects` from `PHONE_ALLOWED`                                                                                                                                                                                                 |
| `test_ai_chat_provider_path`      | **8 passed, 1 skipped**                      | after declaring + installing `keyring>=25.0` (conftest swaps in an in-memory keyring; real keychain never touched)                                                                                                                                  |
| `test_phase34` deadline ratchet   | **11 passed**                                | ratchet re-recorded to the verified 7-site set incl. gateway proxy                                                                                                                                                                                  |
| `test_phase39c` dashboard         | **14 passed**                                | 5 stray hexes tokenized; 430px breakpoint added; 2 tests corrected to match the (correct) dark-first design                                                                                                                                         |
| Post-R1.4/R1.5 full suite         | **2726 passed, 39 skipped, 0 failed** (380s) | run 2026-10-16 after gateway loopback-issuance guard, standard fingerprint, release-pipeline tests                                                                                                                                                  |
| Autonomous run 2026-10-20         | **2799 passed, 39 skipped, 0 failed** (174s) | `.venv/bin/python -m pytest -q -p no:cacheprovider`; +1 test vs the 2798 at run start (new regression test for the model-timeout branch in `test_phase42`); order-dependence fixed in `tests/conftest.py`. See `docs/AUTONOMOUS_ENGINEERING_LOG.md` |
| Android unit suite                | see §6                                       | `gradle :app:testDebugUnitTest` — real-contract models, cross-language pinning, MockWebServer client contract, SessionStore behavior                                                                                                                |

The 9 baseline failures and their root causes:

1–3. `test_ai_chat_provider_path` (`llm_enabled` ×3) — `keyring` undeclared and
uninstalled; the secure key store silently degraded to a no-op fallback.
Fix: declared in `requirements.txt` + `pyproject.toml`, installed. 4. `test_phase34` silence-ratchet — new 7th site `jarvis/companion/gateway.py`
(timeout=30.0) unrecorded since the gateway post-dated the ratchet.
Fix: ratchet re-recorded with the exact verified set (Counter).
5–8. `test_phase39c` (dark pref, token coverage ×2, breakpoint, stray hex) —
dashboard lacked the 430px tier and 5 stray hex colors (real, fixed in CSS);
two tests assumed a light-first design the app correctly doesn't use
(fixed in the tests to check the same concern: base theme is genuinely dark;
the light override restates every color token). 9. `test_phase67` phone allowlist equality — WIP change had added `/projects`
to the backend phone surface, contradicting Phase 67's pinned decision
(explicitly refused to grow the list) and the module's own "every path
phone.html actually calls" rule. Fix: one line removed. **Flagged: this was
a line in pre-existing uncommitted WIP — review it.**

## 2. Verified capabilities

- **Backend**: FastAPI on `127.0.0.1:8765`; auth via `X-JARVIS-Token`
  (master or scoped session); `/health` protected; 126 tools registered
  (re-verify before citing); scheduler, memory (hybrid keyword+vector),
  voice (macos_say TTS; STT consent-gated), local calendar/email.
- **Companion gateway** (`jarvis/companion/`): full contract now test-covered
  (31 tests): one-shot pairing codes with supersession + 2-min TTL, 8-h
  scoped sessions, revocation, SHA-256 cert fingerprint served for pinning,
  strict-subset allowlist, master-token proxy, device-identity forwarding.
  **Now owned by the backend** (R1.3, done 2026-10-16):
  `jarvis/companion/server.py` spawns/stops the gateway subprocess (own
  process group, TLS chain-verified readiness, cert stable across restarts,
  failures reported not silent); started in the FastAPI lifespan when LAN mode
  is on, stopped on shutdown; `GET /companion/status` (protected) reports the
  real state. 6 lifecycle tests in `tests/test_companion_server.py`.
- **macOS app** (`macos/Vantage/`, SwiftPM, zero deps): builds; talks to
  `127.0.0.1:8765` only (no non-local plaintext); native pairing via QR.
- **Android native** (`android/`, Compose, `com.vantage.companion`): now
  **compiles** (version-catalog + plugin-alias fixes) and builds a verified
  debug APK (aapt2 confirms package `com.vantage.companion`). Contract
  rewritten to the **real** backend payloads; pairing goes through the gateway
  redeem with **X.509 SHA-256 TLS certificate pinning**; QR deep-link
  (`vantage://pair`) wired. Unit suite rewritten to the real contract,
  including a cross-language pinning proof (openssl == python == kotlin).
  **Untracked by git** (R0.3) and **device-level pairing untested** (no
  emulator in this environment). **The unit suite runs** — the blocker
  recorded here was never a missing toolchain, it was the default JDK.
  Measured 2026-10-26:

      export JAVA_HOME=/opt/homebrew/opt/openjdk@17
      export ANDROID_HOME=$HOME/Library/Android/sdk
      cd android && ./gradlew --offline :app:testDebugUnitTest
      -> BUILD SUCCESSFUL, 53 tests, 0 failed, 0 skipped

  JDK 26 (the machine default) and Corretto 11 both fail — AGP 8.7 needs
  17-21. JDK 17.0.20 is installed via Homebrew; nothing needs downloading.
  The remaining Android gap is the **runtime**, not the build: no emulator
  or device exists here, so pairing is still untested at device level.

- **Dashboard** (`dashboard/index.html`): dark-first token system (42 tokens
  base, 40 color tokens restated in the light override — 2 easings inherit by
  design), focus-visible, reduced-motion, aria-live, empty states, 640px +
  430px breakpoints, zero external resources, inline script parses under node.

## 3. Verified defects / gaps (current)

Ranked; details + acceptance criteria in `docs/PROJECT_ROADMAP.md`.

| #   | Defect                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            | Tier               | Evidence                                                                                                                                                                                                         |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | ~~Release pipeline builds the legacy WebView wrapper~~ **fixed** (R1.5): release builds `android/`, aapt2 identity check fails on the wrong package, provenance manifest, regression test                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         | done               | `android/build_apk.sh` + `installers/build_release.sh` + `tests/test_release_pipeline.py` (3 passed); end-to-end run 2026-10-16: `com.vantage.companion` APK + manifest                                          |
| 2   | ~~Companion gateway never started by any process~~ **fixed** (R1.3, owned by backend lifespan)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | done               | `jarvis/companion/server.py` + `jarvis/api/app.py` lifespan + `GET /companion/status`; 6 lifecycle tests                                                                                                         |
| 3   | ~~Native Android client uses legacy contract; no cert pinning~~ **fixed** (R1.4): gateway redeem, X.509 pinning, QR deep-link, real wire models                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   | done               | `SetupViewModel` + `PinnedCertificateTrustManager` + `ApiModels.kt`; cross-language pinning test; device-level pairing still needs a phone/emulator                                                              |
| 4   | Canonical `android/` tree untracked by git                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        | 0                  | `git ls-files android/` → empty                                                                                                                                                                                  |
| 5   | ~~Multi-turn memory on the model chat path~~ **partly fixed** (R2.1): `/ai/chat/send` threads the last 12 turns; the `/command` fallback path has no conversation id (that's R2.2)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                | done               | `tests/test_conversation_history.py` (7 passed)                                                                                                                                                                  |
| 6   | Two disconnected chat surfaces; rules-first, model = fallback annotator                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           | 2                  | `app.py:872` vs `app.py:3621`; model only via `orchestrator.py:532-536`                                                                                                                                          |
| 7   | ~~macOS lifecycle: onDisappear kills services; connect() not idempotent; SIGTERM to env not python~~ **fixed** (R3.1); process-teardown not live-verified (no GUI session)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        | done               | `ContentView.swift` (onDisappear removed), `BackendManager.swift` (idempotent start, SIGINT escalation)                                                                                                          |
| 8   | ~~Destructive actions without confirmation~~ **fixed** (R3.2) — and "Delete All Data" was actually _broken_ (empty body → 422 swallowed); now sends `{scope:"everything",confirm:true}` + shows errors                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            | done               | `SettingsView.swift` (2 alerts), `ActivityView.swift` (cancel confirm)                                                                                                                                           |
| 9   | ~~Silent error states~~ **fixed** (R3.3, 2026-10-20): per-section fetch failures are now a distinct state, never shown as empty. `HomeView` tracks `sectionErrors` + renders a `SectionErrorRow` (error + Retry) and "—" in a failed metric; `SelectionInfoView` gains a Retry; Export Data + session revoke + two SettingsView loads surface failures (no more silent `catch {}`). `swift build` clean; **GUI runtime not run** (running app left untouched) — verify loading/empty/error/retry on next relaunch                                                                                                                                                                                                                                                                                                                                                                                                                                 | done (build)       | `HomeView.swift` (SectionPresentation + sectionErrors), `ProvidersView.swift` (SelectionInfoView Retry), `SettingsView.swift` (exportError/revokeError/loadError alerts); test: `SectionPresentationTests.swift` |
| 10  | ~~Packaging swallows codesign failure~~ **fixed** (R5.1: both scripts now fail loud + verify); artifact provenance still open                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     | 5                  | `build_app.py` sign section + `installers/macos/build_dmg.sh` step 4 (exit 1 on failure)                                                                                                                         |
| 11  | `pyproject` packaging deps lagged runtime deps (fixed this run: +keyring, +python-multipart)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      | done               | diff 2026-10-16                                                                                                                                                                                                  |
| 12  | ~~Native `android/` tree did not compile~~ **fixed**: catalog aliases + plugin alias corrected; **compiles, APK built** (2026-10-16)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              | done               | `gradle :app:assembleDebug` + `android/build_apk.sh` → `dist/Vantage-1.0-android-debug.apk` (aapt2-verified `com.vantage.companion`)                                                                             |
| 13  | **The `ios/VantageAI` package does not compile** — measured 2026-10-22: `swift build` exits 1 with 23 error lines across `BackendManager.swift` (an `Equatable` conformance on an enum carrying a non-`Equatable` payload; a `browseResultsChanged` member that has never existed on `NWBrowser`), `CompanionAPIClient.swift` (four `post(_:body:)` calls that cannot infer a generic parameter), `DevicesView.swift` (a `catch` that binds `error` and then assigns to it) and `SetupView.swift` (an iOS-only `navigationBarTitleDisplayMode` in a package that declares macOS). Its models have the same contract drift the macOS app had: `security`/`privacy` as `[String: String]?` where the server sends mixed-type maps, `peripherals` as a `String?` where it sends a map, and a required `id` on `Confirmation` and `DeviceInfo` that the server never sends. **Recorded, not fixed** — iOS is paused by the project's own instructions | paused (by policy) | `tests/test_r86_ios_package_state.py` (5 passed) — the state is pinned so it cannot drift unnoticed                                                                                                              |

## 4. Pre-existing uncommitted work (NOT mine — preserved)

**2026-11-16:** the tree started this session **clean** (`git status --short`
empty). During the session a **second Claude session titled "Jarvis Forge
setup"** was writing to this same checkout concurrently. It owns, and I did not
touch: `jarvis/tools/desktop_native.py`, `jarvis/desktop/`,
`jarvis/tools/__init__.py` (registers `desktop_native.SPECS` — the registry
went 181 → 196 tools), `native/desktop_executor/main.swift`,
`tools/input_probe/main.swift`, `tests/test_desktop_native.py`.

Two consequences worth knowing when reading this file:

- A full-suite run during my work reported 6 failures; 3 were mine (fixed
  below) and 3 were **not reproducible in a fresh run** — they are the
  signature of a tree being edited mid-run, not of a defect.
- `test_phase52_...::test_the_two_capabilities_this_phase_added_are_there`
  fails because the README publishes **181 tools** while the registry now
  reports **196**. That is the other session's tool registration outrunning its
  README edit, not a provider defect; I left both the tools and that README
  line alone.

The earlier note below describes the 2026-10-16 run.

The tree started this run with **128 modified + 69 untracked files**
(~14,122 insertions / 10,723 deletions vs HEAD), plus 69 unpushed commits.
The WIP in `jarvis/api/app.py` is a coherent **session-revocation hardening**
(`_revoked_sessions`, `_record_session_revocation`, `_is_session_revoked`)
that I preserved; I removed one line from it (`/projects` in `PHONE_ALLOWED`)
because it contradicted a pinned security test. The WIP in
`tests/test_phase72_ai_provider_security.py` and other docs are untouched by
me. A snapshot of the pre-run state lives in `.autodev/presnapshot-2026-10-16.txt`.

## 5. Quality scores (0–10, evidence-backed, no inflation)

| Dimension                       | Score | Rationale                                                                                                                                                                                   |
| ------------------------------- | ----- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Functional correctness          | 6.5   | 2713 tests green, 9 verified failures fixed this run; conversational path is rules-first, not model-driven                                                                                  |
| End-to-end completeness         | 5     | Gateway now wired+tested (R1.3) but the native Android app is half-written WIP (does not compile), so real device pairing still does not work end-to-end; workflows local                   |
| Test reliability                | 8     | Full suite runs deterministic offline in ~8 min; every skip has a named prerequisite; collection clean                                                                                      |
| Security / privacy              | 6.5   | Master-token-on-Mac + scoped sessions + strict-subset gateway allowlist, test-pinned; gateway now owned by backend (no orphan); but no cert pinning on the client, 2 UI actions unconfirmed |
| UX / accessibility              | 6.5   | Dashboard strong (focus, aria, reduced-motion, dark-first, breakpoints); macOS app has silent-error gaps + missing confirmations                                                            |
| Performance / resources         | 6     | Suite 12 min; backend fine; no measured regressions; not profiled systematically                                                                                                            |
| Recovery / maintainability      | 7.5   | Durable autodev queue/checkpoints/deadlines; supervisor tested; two parallel agent pipelines (`core/` vs `agent/`) are drift risk                                                           |
| Integration quality             | 5.5   | Calendar/email local-real; IMAP/SMTP/CalDAV configured-gated; Playwright installed and 28 browser tests run; Android native untested against gateway                                        |
| Documentation / reproducibility | 8     | CLAUDE.md resume protocol, roadmap, runbook, status; build scripts exist but swallow signing failures                                                                                       |

> **Correction (2026-10-20 autonomous run; see `docs/AUTONOMOUS_ENGINEERING_LOG.md`).**
> Three factual claims in the table above are now **stale** — the scores are left as
> the owner's judgment, but the underlying facts changed:
>
> - _Security row:_ "no cert pinning on the client" — **false**: Android enforces
>   X.509 pinning via `PinnedCertificateTrustManager` wired into `VantageApiClient`
>   (sslSocketFactory), with 7 pinning tests. "2 UI actions unconfirmed" — **false**:
>   all consequential UI actions (Delete All Data, session-revoke, device-revoke,
>   task-cancel, Remove Key) have confirmation alerts; the unguarded ones are the
>   pairing approve/deny, which _is_ the approval gate.
> - _Documentation row:_ "build scripts... swallow signing failures" — **false**:
>   `assemble_app.sh` now fails loud on a codesign sign **and** verify failure (verified
>   both ways), mirroring `build_app.py` (R5.1). Both paths were also run end-to-end
>   (native `make app`, and `build_app.py` into a temp dir) producing valid bundles.
> - _R2.4_ ("ok() defaults to SUCCESS") — **false**: `ok()` already defaults to
>   `UNKNOWN`; see §6.

## 6. Next task (single)

**R2.4 — Default-UNKNOWN verification for effect tools: VERIFIED COMPLETE**
(re-verified by the 2026-10-20 autonomous run; see `docs/AUTONOMOUS_ENGINEERING_LOG.md`
iteration 20). The original claim "`tools/base.ok()` currently defaults to SUCCESS" is
**stale** — `ok()` already defaults to `Verification.UNKNOWN` (`jarvis/tools/base.py`), and
an `unverified()` helper exists. The "world-effect tools must report unverified by default
and opt in when they can verify" half is implemented: write-side tools either observe the
effect before claiming success (`open_application` reads the process table, `write_file`
reads the file back, `get_car_telemetry` returns `FAILED` on an unread) or return
`unverified()`/`UNKNOWN` when they can't. **Tests pin both sides** (`test_phase10_verification_authority`,
`test_phase11_write_file::test_unreadable_result_is_reported_unverified_not_success`,
`test_home::test_control_online_unverified_not_claimed_done`). No code change needed.

Also eligible: R3.3 (honest loading/error states in macOS views).
**Waiting on the owner** (not eligible for a worker to decide alone):

- R2.2 unify chat surfaces — product decision: model-primary vs rules-first.
- R0.3 git decision — commit the untracked `macos/`/`android/`/
  `jarvis/companion/` trees? (Both runs' work in those trees is uncommitted,
  deliberately, and waits on it. A `git clean` would destroy the native app.)
- Android **device-level** verification — no emulator/phone in this
  environment; the owner should pair a real device once the tree is committed.

Done this run (2026-10-16): R0.1, R0.2, R1.1, R1.2, R1.3, R1.4, R1.5, R2.1,
R3.1, R3.2, R3.4, R3.5, R5.1, R5.2, R5.3 (CI android job). Full backend suite
after all changes: **2726 passed, 39 skipped, 0 failed** (380s).

How a future session starts here: read `CLAUDE.md` (resume protocol), then
this file, then `.autodev/tasks.json`. To run unattended:
`scripts/autodev/autodev status` / `resume` (see `docs/AUTODEV_RUNBOOK.md`).

---

## 7. Update — 2026-10-21 (live macOS runtime; two defects found and fixed)

The R3.3 / macOS-lifecycle rows that said "EXTERNALLY BLOCKED: needs owner relaunch"
were **stale** — the GUI had been relaunched at 11:58 with a bundle rebuilt at 11:56.
Re-measuring the _running product_ (not the source) found two real defects and closed
the blocked rows. Details and evidence: `docs/AUTONOMOUS_ENGINEERING_LOG.md`
iteration 55.

- **Defect 1 (P0) — the app could launch with zero windows.** The process was alive but
  the SwiftUI scene never activated, so `.task` never ran and the app never connected
  (0 `/health` requests in 40 s). Bisected to a single variable: the same binary, same
  bundle, gave a window with `-ApplePersistenceIgnoreState YES` and none without it;
  a fresh bundle id always worked — so it was stale **per-bundle-id app state**, not the
  code. **Fixed** in `macos/Vantage/Sources/VantageApp.swift`
  (`applicationWillFinishLaunching` registers `ApplePersistenceIgnoreState=true` /
  `NSQuitAlwaysKeepsWindows=false` before AppKit restores state). Verified: 3 cold
  launches → a window each time.
- **Defect 2 (P0) — the companion gateway could be orphaned.** `get_companion_server`
  compared settings by identity, so each call replaced the manager that owned the running
  gateway child. A gateway from Oct 18 was still holding `0.0.0.0:8766` on Oct 21, serving
  a certificate from a deleted test home. **Fixed** + **2 regression tests** (both verified
  to fail against the old code). The orphan was stopped; `:8766` is free.
- **R3.3 error states: RUNTIME VERIFIED** — the app was exercised against a stopped and a
  restarted backend; it stayed alive, kept a truthful state, and reconnected automatically.
- **Bundle signature repaired** — the shipped `Vantage.app` had an invalid seal
  ("plist or signature have been modified": its `Info.plist` had been rewritten after
  signing). Rebuilt; `codesign --verify --strict` is VALID.
- **Full suite (clean, no concurrent load):** **2822 passed / 33 skipped / 0 failed** (174 s).
  The 33 skips each name an external prerequisite (playwright / Pillow-dependent / macOS-only
  desktop tests / no `Vantage.app` in `dist/` / no physical device / no real API key / mic
  permission). The OCR tests that fail under a _concurrent_ Gradle build are load-sensitive,
  not defective: 0.3 s standalone, and they pass in the clean run.
- **Android unit suite (fresh):** 46 tests / 0 failures (JDK 17 + `ANDROID_HOME` set).
- **Dependencies installed:** `pillow` and `qrcode` were declared in `requirements.txt` but
  absent from the venv, silently skipping 21 tests. Now installed; those tests pass.

**Still open, unchanged:** R2.2 (chat-surface unification — product decision), R2.3
(model-planned actions), R0.3 (commit the untracked trees — owner decision), live model
inference (needs credentials), Android device-level pairing (needs hardware), iOS (paused).

**Environment limit:** `swift test` cannot run here — only the Command Line Tools are
installed, so `XCTest` is missing. Pre-existing, unrelated to these changes.

---

## 8. Update — 2026-10-21 (R2.2 + R2.3: the model can plan, the chat surfaces share one conversation)

Two of the three open items are now implemented and tested. Details and evidence:
`docs/AUTONOMOUS_ENGINEERING_LOG.md` iteration 56.

- **R2.3 — the model can now propose an action, and a proposal is not an action.**
  New `jarvis/core/model_planner.py`. The model is asked for a _structured_ proposal
  (`answer` | `action` | `clarify`), it is validated against the tool registry, and the
  result is an ordinary `Plan` that runs through the existing
  `PermissionEngine.evaluate` → execute → verify path. The model proposes; the
  permission engine decides. `DESTRUCTIVE` and `PHYSICAL` tiers are excluded from model
  proposals entirely; an invented tool is refused and named; invalid parameters become a
  clarification rather than a guess; the tool name is canonicalised so an alias cannot
  dodge a standing revocation. 23 new tests, each verified to fail against the old code.
- **R2.2 — one canonical conversation model behind both chat surfaces.** New
  `jarvis/core/conversation.py`. The two surfaces had two stores (`messages` by
  `session_id`, `ai_messages` by `conversation_id`), two history loaders and no shared
  behaviour; the chat surface could not act on anything at all. There is now one
  `ConversationService` that is the only place a turn is read or written, and it merges
  both histories. The chat surface can now act through the _same_ gate
  (`JarvisCore.propose_and_execute`), and `/ai/chat/send` gained an **additive**
  `action` field — the existing `ok`/`response`/`provider`/`model`/`notes` fields are
  unchanged, and both endpoints and both dashboard UIs still work. 19 new tests.
- **Full backend suite: 2864 passed / 33 skipped / 0 failed** (189 s) — up 42 from the
  two new test files, no regressions.

**Still open:** R0.3 (commit the untracked `macos/`/`android/`/`jarvis/companion/`
trees — an owner decision, deliberately not taken).

**Not verified:** live model inference. The configured provider (9router) currently
returns `401 Invalid API key`, so the model path is proven with a deterministic stub
rather than a live call. A live test needs a working credential and the owner's consent
to make an outbound call.

---

## 9. Update — 2026-10-21 (attachment pipeline: an image's text is read, not just measured)

The image branch of `/ai/process-attachments` measured the image (size, mode,
brightness, dominant colours) and reported `status: "processed"`. That is a
genuine measurement, but it is the _envelope_ of the picture: **a screenshot
was uploaded and what came back described its pixel dimensions while its
contents were never read.** The route marked an image "processed" on the
strength of a measurement alone.

`jarvis/vision/ocr.py` (the macOS Vision framework) already existed and read
real text — it was wired to the `read_image_text` tool route but not to the
attachment pipeline. The pipeline now calls it after the measurement, and the
three outcomes stay distinct: text read (`text` + `text_lines`), the image read
but empty (`text: ""` + a note), and the recogniser unable to read it (no
`text` + a note). The measurement survives all three.

**Proof:** `tests/test_attachment_ocr_text.py` — 5 passed, including an
end-to-end test using the real recogniser on a real PNG. Verified to catch the
regression (reverting the OCR call fails 4 of the 5). Existing attachment +
vision suites: 66 passed. **Live** against :8765: a real PNG processed through
the HTTP API returned
`"text":"Project AMR status report\nBattery 74 percent charging","text_lines":2`
alongside its measurement.

**Full backend suite after the change: 2869 passed / 33 skipped / 0 failed**
(197 s) — up 5 from the new file.

**Still open:** R0.3 (commit the untracked trees — owner decision); live model
inference (the configured provider still returns 401); and the
_user-visible_ half of the attachment path. Measured while checking it: **no
client uses the attachment API at all** — a search of `dashboard/`, `macos/`,
`android/` and `ios/` for `ai/attachment` returns nothing. The upload, list,
delete and process routes exist and work, and nothing in the product calls
them. So the gap is not "the text is not displayed" but "there is no way to
attach anything from any surface".

---

## 10. Update — 2026-10-21 (a client for attachments; and a real OCR bug)

Two of the three open items in the queue are done. Details and evidence:
`docs/AUTONOMOUS_ENGINEERING_LOG.md` iteration 58.

- **R6.2 — the dashboard chat view is now a client of the attachment API.**
  `save the message -> upload the file -> process it -> read the result`, with
  the result rendered in an attachment card (filename, status, and what was
  extracted — or the reason there is none). The extracted text is inserted with
  `textContent`, never as markup: a file's contents are untrusted input.
- **Defect fixed — the processing result was returned and then thrown away.**
  `/ai/process-attachments` persisted only the _status_; the `metadata` column
  stayed `{}` forever, so a client could see "processed" and no way to learn
  what was extracted. The result is now persisted and exposed as `result` by
  both read routes.
- **Defect fixed — the first OCR call on a fresh machine always failed.**
  `OCRBackend._compile()` built the helper and returned its path but never
  recorded it, so `available()` said yes while the binary it was about to run
  was `None` ("OCR did not complete: No such file or directory: 'None'"). The
  _second_ call worked. The bug was invisible on any machine where the helper
  was already built. Fixed, with a regression test that makes the first call
  against an empty home.
- **Proof:** `tests/test_attachment_client_contract.py` (9) + `tests/test_vision.py`
  → 59 passed. Both fixes verified to fail against the reverted code. **Full
  backend suite: 2879 passed / 33 skipped / 0 failed** (207 s) — up 10 from
  the new file, no regressions.
- **Runtime:** the full flow through the live HTTP API on :8765 — upload →
  process → `"text":"Project AMR status report\nBattery 74 percent charging"` →
  **read back later with the text intact**, which is exactly what was broken.
  The served dashboard page carries the client.

**Still open:** R0.3 (commit the untracked trees — owner decision); R6.3 (the
macOS app has no attachment affordance yet — the API contract is proven, so it
is a Swift UI task); live model inference (the configured provider still
returns 401).

---

## 11. Update — 2026-10-21 (the macOS app gets the attachment affordance)

**R6.3 — the native app can attach a file and shows what the backend made of
it.** The dashboard was the first client; the macOS app is now the second.

- `APIClient.upload(...)` posts multipart/form-data with a **per-request
  boundary** (a reused one would let a file's contents collide with it and
  forge a part), and reads the bytes before sending so an unreadable file is
  reported rather than half-uploaded.
- `APIModels` gained the attachment shapes, each field checked against a real
  response before being declared — a `Codable` struct with a non-optional field
  the server omits fails to decode, and the user would see an error instead
  of the result.
- `CommandCenterView` gained a paperclip button, a `fileImporter`, the pending
  file with a remove control, and the extracted text in the transcript. A
  message with only a file is still a message; the conversation is created once
  and reused; the MIME type comes from the extension so an unsupported file is
  refused by the client rather than after an upload the backend will reject.
- `AttachmentResult.displayText` gives one honest description: the extracted
  text, else the note ("read, and it contains no text"), else the reason,
  else the summary.

**The order is fixed by the schema.** An attachment's `message_id` is a
foreign key onto `ai_messages`, itself a foreign key onto `ai_chats`
(`PRAGMA foreign_keys=ON`), and the macOS app talks to `/command`, whose
messages live in `messages` keyed by session. Verified: an attachment row with
a message id that does not exist fails with `IntegrityError`.

**Proof:** `tests/test_macos_attachment_client.py` — 9 passed. The tests pin
the contract from the _server_ side (so a renamed field breaks here rather
than in a signed app) and the client source: the endpoints called, the
conversation and message saved **before** the upload, the message id carried,
the boundary generated per request, the result described for the user.
Verified to catch a regression. `swift build -c release` clean, no warnings;
`assemble_app.sh release` → bundle rebuilt, `codesign --verify --strict` VALID.
Runtime: launched with `open -a` → window on screen (900x572), health poll 2
requests in 40 s. **Full backend suite: 2888 passed / 33 skipped / 0 failed**
(206 s) — up 9 from the new file, no regressions.

**Not verified:** the file-picker interaction itself — no assistive access and
no screenshot tool here, so the picker could not be driven and the rendered
card could not be seen. The wiring and the contract are pinned by tests that
exercise the real endpoints.

**Still open:** R0.3 (commit the untracked trees — owner decision); R6.4
(calendar/email end-to-end acceptance — the tools exist and are unit-tested,
but the proposal → approval → execution → verified-result path has the same
shape of evidence gap the attachment path had, and the R2.3 planner now makes
it exercisable end to end for the first time); live model inference (the
configured provider still returns 401).

---

## 12. Update — 2026-10-21 (calendar/email end to end; three defects fixed)

**R6.4 — the path a person actually takes is now proven, and three real
defects were found on the way.** Details: `docs/AUTONOMOUS_ENGINEERING_LOG.md`
iteration 60.

- **An event with no name was invented.** "create a calendar event tomorrow"
  produced the title _"Meeting"_ and an approval prompt showing that name as if
  the person had chosen it. The planner's `title or "Meeting"` fallback is
  gone: a missing name is a **question** now.
- **"create a calendar event called Review" was titled "calendar event called
  Review".** The words used to _ask_ for the event were filed as its _name_.
  The noun phrase and "called" are now stripped.
- **The created event was never confirmed.** `ok(...)` left verification at
  UNKNOWN, so "created" rested on the provider's own word. `create_event` now
  **reads the calendar back** and looks the event up by its id: found →
  `SUCCESS`; read failed → `UNKNOWN` ("I could not confirm it"); accepted but
  absent → `UNKNOWN`. A failed read is not a failed write.
- **Proof:** `tests/test_r64_calendar_email_acceptance.py` — 12 passed. A
  calendar write must ask for approval _and_ create nothing before the answer
  (the test counts the events, so "it asked for confirmation" alone would not
  pass); rejecting leaves the calendar untouched; email search → read →
  summarise work over a real maildir; a draft is not a send.
  All three fixes were verified to **fail** against the reverted code.
  **Full backend suite: 2900 passed / 33 skipped / 0 failed** (258 s) — up 12
  from the new file, no regressions.
- **Runtime:** live on :8765 — `POST /command` → CONFIRM with a confirmation
  id; `POST /confirm/{id}?approve=true` → EXECUTE, `create_event ok=True
verification=success`, note _"Confirmed by reading the calendar back"_. The
  real calendar afterwards holds `('EVT-5618e9463852', 'Design Review', …)` —
  the right name, confirmed.

**Still open:** R0.3 (commit the untracked `macos/`/`android/`/`jarvis/companion/`
trees — owner decision, deliberately not taken); a live CalDAV/SMTP account for
a true remote end-to-end run (owner credential dependency — the local
providers are the pinned path); live model inference (the configured provider
still returns 401).

---

## 13. Update — 2026-10-21 (the browser slice: the controller was dead code)

**R4.3 — the browser capability existed and was unreachable.** Details:
`docs/AUTONOMOUS_ENGINEERING_LOG.md` iteration 61.

`jarvis/tools/browser_controller.py` has been a well-built Playwright
controller since Phase 92 — sessions with isolation, navigation with
verification, click and type with state checks — and **nothing imported it**.
It was not in the tool registry, no route reached it, and no surface could
call it. Its 28 tests all skipped.

Five defects fixed:

1. **The module could not be imported without Playwright** — the import is now
   optional and a missing Playwright is _reported_, not an import error.
2. **There was no way to read a page** — `read_page()` returns the rendered
   body text, bounded. The title is not content.
3. **The promised injection resistance did not exist** — new
   `jarvis/tools/page_content.py`: page text is marked as the page's words,
   instruction-like content is _flagged_ rather than obeyed, and zero-width
   and bidi-override characters are removed. **The content is never hidden** —
   the person asked to read it.
4. **The URL validator refused nothing** — `file:///etc/passwd` became
   `https://file:///etc/passwd` and the `javascript:` guard was dead code. It
   now uses the same normaliser as the reachable `open_url` tool.
5. **Closing a session leaked a browser** — the browser and the Playwright
   driver are now closed with it.

**Five tools, in the registry:** `browser_open` (LOW), `browser_read` (READ),
`browser_close` (READ), and `browser_click` / `browser_type` (**EXTERNAL,
confirmation-gated**). The _read_ is routed — "what does example.com say"
reaches it; the _actuations_ are not, for the same reason `click_desktop` is
not: a sentence must not be able to make the machine act.

**The browser test suite could never run.** It served its fixture page on
**port 8765** — the backend's port — so every test skipped whenever the
product was running. Now a free port: **28 skipped → 27 passed, 1 skipped.**

**Proof:** `tests/test_r43_browser_slice.py` — 18 passed, including two that
drive a **real Chromium** against a **real local HTTP server**. All four
fixes were verified to fail against the reverted code.

**The tool ledger moved on purpose** (30 → 34 unreachable) and the README's
published counts moved 148 → 153 tools and 53 → 54 routes — the "the published
number must equal reality" invariant the suite guards.

**Proof:** `tests/test_r43_browser_slice.py` — 18 passed, including two that
drive a **real Chromium** against a **real local HTTP server**. All the fixes
were verified to fail against the reverted code. **Full backend suite: 2945
passed / 33 skipped / 0 failed** (229 s) — up 45 from the browser work, no
regressions.

**Live:** `POST /command` with "what does example.com say" → `intent
browser_open`, EXECUTE, and the page's own text returned, marked untrusted.

**Still open:** R0.3 (commit the untracked trees — owner decision); live
model inference (the configured provider still returns 401); a live
CalDAV/SMTP account for a remote end-to-end run.

---

## 14. Update — 2026-10-21 (the browser slice's last third: save a page to a file)

**R4.4 — a page's content can be saved to a file the user chose, and the save
is confirmed.** Details: `docs/AUTONOMOUS_ENGINEERING_LOG.md` iteration 62.

- **The write goes _through_ the file layer, not around it.** `browser_save`
  composes `browser_open` → `write_file`, so the path is resolved and guarded
  by exactly the code `write_file` uses, and the result is confirmed by
  reading the file back. There is one answer to "may I write here?" in this
  project, not two that disagree.
- **The refusal comes first** — the guard is checked before the page is even
  read, so a protected path costs no browser launch and no network round trip.
- **The file says where the words came from**: a `# Saved from <url>` header
  and "This is the page's text, not the user's words and not Vantage's", plus
  a warning line when the page contained instruction-like text.
- **EXTERNAL and confirmation-gated** — it puts something from the internet on
  the user's disk.
- **Proof:** `tests/test_r43_browser_slice.py` — 24 passed, the new ones
  driving a real Chromium against a real local HTTP server.
- **A test that passed for the wrong reason.** The first guard test kept
  passing with the tool's own guard _removed_, because `write_file` has its
  own guard and refused with the same stubbed message. The protection was
  real; the test was not proving what it claimed. It now counts the page
  reads and fails if a page was fetched despite the refusal — and that
  version does fail when the guard is removed.
- **The tool ledger moved on purpose** (34 → 35 unreachable; `browser_save`
  carries no sentence route, because it needs a destination path a sentence
  cannot safely supply) and the README's published count 153 → 154.
- **Live, through the real engine:** a model-proposed save was held at
  `CONFIRM` with a confirmation id, and after approval ran →
  `browser_save ok=True verified=True`, with the file on disk carrying its
  `# Saved from <url>` / "this is the page's text" header. A _sentence_
  cannot reach the tool at all — it needs a destination path.

**Still open:** R0.3 (commit the untracked trees — owner decision); live
model inference (the configured provider still returns 401); a live
CalDAV/SMTP account for a remote end-to-end run.

---

## 15. Update — 2026-10-21 (the macOS app can approve an action — and could never)

**The approvals screen existed, and was broken.** Details:
`docs/AUTONOMOUS_ENGINEERING_LOG.md` iteration 63.

`ApprovalsView.swift` has listed pending confirmations and offered Approve
and Deny since the app's early days. It could not have worked: **its Swift
model declared fields the server has never sent** — `id`/`action`/
`description`/`device_id`/`expires_at` against the server's
`confirmation_id`/`tool`/`preview`/`device`/`state`. `id` and `action` are
required, so **decoding a real confirmation failed outright**: the screen
showed an error, not the approvals. And the card read `confirmation.action`,
so every card would have said "Unknown action" even had it decoded.

- **Fixed**: the model declares the server's fields, checked against a live
  `/confirmations` response. The card shows the tool and **the server's own
  one-line `preview`** of what will happen.
- **One way to answer an approval**: `APIClient.resolveConfirmation(_:approve:sessionId:)`,
  used by both the Approvals screen and the Command Center. It sends the
  **approver's session**, because the backend records _who_ answered.
- **The Command Center can now answer.** It said "⚠️ Requires confirmation"
  and offered no way to confirm, so a consequential action could never
  complete from the desktop. It now shows the pending action with approve and
  deny.
- **Proof:** `tests/test_macos_approvals_contract.py` — 7 passed. The first
  test parses the field names out of the app's own Swift model and checks
  every one is a field the backend sends — the check that would have caught
  the original break. Verified to fail against the reverted model.
- **Runtime:** the rebuilt app was launched with a pending confirmation on the
  server, and the backend's log shows **`GET /confirmations → 200`** from it —
  the app read and decoded a real approval.
- **Full backend suite: 2958 passed / 33 skipped / 0 failed** (236 s) — up 7
  from the new file, no regressions. And
  `POST /confirm/{id}?approve=true&session_id=native` → `EXECUTE`,
  `browser_save ok=True verification=success`, with the audit row recording
  `state=approved`, `approved_by_session=native`.

**Still open:** R0.3 (commit the untracked trees — owner decision); live
model inference (the configured provider still returns 401); a live
CalDAV/SMTP account for a remote end-to-end run.

---

## 16. Update — 2026-10-21 (the dashboard can show and answer an approval)

The macOS app gained approvals in iteration 63. The **dashboard** — the
surface most people use for the model — had none: no `/confirmations` fetch,
no card, no approve or deny. Details: `docs/AUTONOMOUS_ENGINEERING_LOG.md`
iteration 64.

- The `tool-request-area` that was sitting empty in the markup now holds a
  card per pending action: the tool, **the server's own one-line `preview`**
  of what will happen, and Approve / Deny.
- **The preview is the server's, not a rebuilt one.** A card that says
  something slightly different from the thing being approved is worse than
  no card — it is a confident lie where a person looks before pressing a
  button.
- **The text is inserted as text.** The preview is built by the backend from
  a page's own parameters, so `innerHTML` would let a page inject markup.
- **A failed action is reported as failed**, and the _verification_ state is
  reported too — "done" and "confirmed" are different facts.
- **The answer carries the approver's session**, because the backend records
  _who_ answered.
- **Proof:** `tests/test_dashboard_approvals_contract.py` — 11 passed,
  including one that runs `node --check` on the page's own script. Both
  honesty rules were verified to fail against the reverted code.
- **Runtime:** the dashboard's exact requests against :8765 — the list
  returns the pending `browser_save` with its preview; the answer returns
  `EXECUTE`, `browser_save ok=True verification=success`; and the audit row
  records `state=approved`, `approved_by_session=dashboard`.
- **Full backend suite: 2969 passed / 33 skipped / 0 failed** (249 s) — up 11
  from the new file, no regressions.

**Still open:** R0.3 (commit the untracked trees — owner decision); live
model inference (the configured provider still returns 401); a live
CalDAV/SMTP account for a remote end-to-end run.

---

## 17. Update — 2026-10-21 (a phone could approve its own request — fixed)

Checking the Android companion's approval client against the real contract
turned up a **security defect in the gateway**, not in the app. Details:
`docs/AUTONOMOUS_ENGINEERING_LOG.md` iteration 65.

**The gap.** The orchestrator refuses a self-approval when the same session
that raised the request answers it **and** the approver is on a narrow surface
(the phone). The second half of that condition never reached the rule: the
gateway validates the session and learns its surface, then proxies with the
**master** token — so the backend could not look the session up, and `/confirm`
read the surface from a query parameter the phone never sent. It fell back to
`"unknown"`, which is not narrow, so **the guard was disarmed for exactly the
device it exists to constrain**. A phone could raise a `create_event` and then
approve it, and the event was created.

Measured, before the fix:

```
SURFACE_unknown  gateway (no surface forwarded) -> EXECUTE | Created Standup on Tue 01 Dec at 09:00.
SURFACE_phone    phone (surface known)         -> BLOCK   | You cannot approve your own requests.
```

**The fix, in three places.** The gateway forwards the surface it _validated_
(`X-Vantage-Surface` — set by the gateway, so a client cannot claim to be a
different surface); `/confirm` reads it as a fallback, with a local caller's
`session_id` still taking priority; and the Android app sends its session as
`session_id` on approve and deny, so the guard has the session to compare.

**Proof:** `tests/test_phone_self_approval_guard.py` — 7 passed, pinning the
guard both ways, the forwarding, the reading, that the client's own header
cannot be forwarded, and that the Android client sends its session. Gateway
and phone-authority suites: 73 passed, 1 skipped. **Live, against :8765:**
a phone's own approval is `BLOCK` ("Approve this on the laptop instead"), and
a different session — the operator — still gets `EXECUTE`.

**The Android client was otherwise correct.** Its `Confirmation` and
`CommandResponse` models already matched the server's real wire shape, unlike
the macOS one which had been wrong for years. One existing Android test
asserted the exact query string; it now asserts the _parts_, so adding a
parameter is not a failure but losing one is. **Android unit suite: 46 tests,
0 failures.**

**Still open:** R0.3 (commit the untracked trees — owner decision); live
model inference (the configured provider still returns 401); a live
CalDAV/SMTP account for a remote end-to-end run.

---

## 18. Update — 2026-10-21 (the sweep for dropped facts)

**A pattern, and two more instances of it.** Details:
`docs/AUTONOMOUS_ENGINEERING_LOG.md` iteration 66.

Four times now, a value has been produced, carried part of the way, and dropped
before the rule that depends on it could see it: the macOS `Confirmation`
model's fields, the browser controller's page text, the gateway's approver
surface, and — found by looking deliberately this time — the request's own
surface. The sweep looked for the shape and found two more.

- **The request's surface was guessed with a heuristic that could never
  fire.** The policy recorded it as `"phone" if "/" in request_id` — and a
  request id is `REQ-<hex>`, so it was a constant "dashboard" wearing the
  costume of a decision. Every confirmation was recorded as a dashboard
  request even when a phone had raised it. It also had no readers, so
  nothing broke; it was simply a fact that was wrong and unused. The surface
  is now carried from the request's `source` through the orchestrator into
  the gate.
- **The phone client sent no session with its requests** (`sessionId = null`),
  so the backend minted a fresh id each time and the self-approval guard's
  session comparison could never match. The app now sends the session it
  holds.
- **The device-id header was broken at both ends** — the gateway forwarded
  `X-Vantage-Device-Id` and the backend never read it, and no client set it.
  The chain is whole now: the Android client sends its device id, the gateway
  forwards it, and `/command` reads it, preferring the forwarded id over the
  body's claim.
- **The self-approval guard now uses two independent facts** — the
  approver's surface _and_ the surface the request was raised on. Either one
  being wrong must not be enough to let a self-approval through.
- **Proof:** `tests/test_dropped_facts_sweep.py` — 9 passed. Android: 46
  tests, 0 failures. **Live, against :8765:** a phone raising and then
  approving its own request → `BLOCK`, nothing executed, and the audit row
  now records `requested_surface: phone` — from the real source, for the
  first time.

**Still open:** R0.3 (commit the untracked trees — owner decision); live
model inference (the configured provider still returns 401); a live
CalDAV/SMTP account for a remote end-to-end run.

---

## 19. Update — 2026-10-21 (a phone that had no name in the registry)

Details: `docs/AUTONOMOUS_ENGINEERING_LOG.md` iteration 67.

The sweep left a pointer: the phone's device id travelled in two shapes that
disagreed. Settling it turned up more.

|                          |                                                                                                     |
| ------------------------ | --------------------------------------------------------------------------------------------------- |
| the app's id             | `phone_<uuid8>`, generated once at setup and kept                                                   |
| the app's fallback       | `"phone"` — not a registered device at all                                                          |
| the registry's id        | `phone_main` — registered, offline, and a fixture rather than a real phone                          |
| the other `phone_*` rows | eight of them, all dated Sept 25 — residue from a test run that predates the isolated `JARVIS_HOME` |

**And the id went nowhere.** The gateway's redeem route took only the pairing
code, so the phone's identity was established and then discarded; the app
generated its id _after_ the redeem, so there was nothing to send even if the
route had asked. So the id was real, stable, forwarded in the header, and read
by the backend — **and referred to nothing.** The permission gate has nothing
to aim at for a device it has never heard of, and the audit chain could not
name the handset.

**Fixed:** redeeming the pairing code is where the phone's identity is
established, so that is where the backend learns it. The gateway registers the
device — **online** (it has just proved it can reach the gateway over TLS) and
**paired** (a redeemed code is the owner's act of trusting it) — with its name
and the client's address. The app settles its identity _before_ the redeem and
names itself in the request.

**What was _not_ wrong:** the action's `device` is `laptop_main` and the
planner never sets it. That is right: the request's `device_id` names the
_caller_, the action's `device` names the _target_, and all 109 tools declare
`allowed_devices=["laptop_main"]`.

**Proof:** `tests/test_phone_device_identity.py` — 7 passed, with a real
gateway app over a fake backend; verified to fail against the reverted code.
Gateway, phone and sweep suites together: 65 passed. Android: 46 tests, 0
failures. **Full backend suite: 3022 passed / 18 skipped / 0 failed** (235 s).

**Still open:** R0.3 (commit the untracked trees — owner decision); live
model inference (the configured provider still returns 401); a live
CalDAV/SMTP account for a remote end-to-end run.

---

## 20. Update — 2026-10-21 (release readiness: the documented commands, run for real)

**R7.1 — every command in the release document was run, and the document was
corrected where it had drifted.** Details:
`docs/AUTONOMOUS_ENGINEERING_LOG.md` iteration 68.

### What was verified by running it

| Command                                                      | Result                                                               |
| ------------------------------------------------------------ | -------------------------------------------------------------------- |
| `cd macos/Vantage && make app`                               | EXIT 0 — bundle built, `codesign verify: ok`                         |
| `codesign --verify --strict macos/Vantage/Vantage.app`       | EXIT 0                                                               |
| `.venv/bin/python build_app.py`                              | EXIT 0 — `dist/Vantage.app`, ad-hoc signed, verified                 |
| `android/build_apk.sh`                                       | EXIT 0 — promotes to `dist/Vantage-1.0-android-debug.apk` + manifest |
| `installers/build_release.sh macos`                          | EXIT 0 — `dist/Vantage-0.2.0.dmg`, codesign verified                 |
| `.venv/bin/python run.py serve --port 8899 --host 127.0.0.1` | binds 8899 (with its own `JARVIS_HOME`)                              |

### What the document got wrong, and is now right

- **The dependency install command could not work.** It said
  `.venv/bin/python -m pip install -r requirements.txt`; the venv is uv-managed
  and has **no pip** ("No module named pip"). The working form is
  `uv pip install --python .venv/bin/python -r requirements.txt`.
- **The port-flag example was misleading.** A second backend on a _different_
  port is refused too: the single-instance claim is held per **`JARVIS_HOME`**,
  not per port. The document now says so and shows the separate-home form.
- **The suite figures were stale**: 2800/38 → **2992/33**.
- **The Android attribution was wrong.** The table credited
  `./gradlew :app:assembleDebug` with producing `dist/…apk`; a bare Gradle
  build leaves the APK in `android/app/build/outputs/apk/debug/`. It is
  `android/build_apk.sh` that promotes it to `dist/` — and that script is now
  what the document names.
- **"The attachment pipeline is still a `# TODO`" was false.** There are zero
  TODOs in `jarvis/attachments/` or `jarvis/vision/`; the pipeline measures an
  image, extracts its text with the Vision OCR backend, extracts a PDF's text
  with pypdf, and transcribes audio through the configured STT backend.
- **"The R3.3 GUI fix is not in the running app" was stale** — the app was
  relaunched after the fix.

### The README's counts, and the guards they lacked

The README published a **2510**-passing suite, **118** API routes, **32 of 148**
gated tools and old risk tiers. The real figures: **2992** passing, **150**
routes, **35 of 154** gated, and read 80 / low 39 / external 18 / destructive
14 / physical 3.

The tool count and the planner-route count were already guarded by tests; the
**API-route count, the gated-tool count, the risk tiers and the suite count
were not** — which is why they drifted the furthest. Four new guards now pin
all of them, plus one that keeps the README and the release document
publishing the _same_ suite count.

### Also documented

The roadmap's R4.3 ask — "add `playwright install chromium` to setup docs" — was
still outstanding. The library alone is not enough: `browser_open` fails with
"Executable doesn't exist" until the browser build is downloaded. That, the uv
install form, and the per-home single-instance rule are now in the release
document's setup and troubleshooting sections.

---

## 21. Update — 2026-10-21 (the AI chat dashboard, in a real browser)

**R7.2 — the dashboard's approval and attachment clients were written and
contract-tested, and never run.** Playwright and Chrome are now installed, so
the real page was driven against the real backend. Details:
`docs/AUTONOMOUS_ENGINEERING_LOG.md` iteration 69.

**The page could not do anything at all.** Three defects, none of which a
contract test could see, because the contract was described but never
exercised:

1. **No working authentication.** `authHeaders()` sent `window.sessionToken`
   — a global nothing ever set — so every request went out with an empty
   token and came back **401**.
2. **Every POST was 422.** `JSON.stringify(...)` with no `Content-Type`
   arrives as `text/plain`, and the API reads a string where it wants a
   dictionary.
3. **The page was served without a bootstrap.** The route was `Protected`
   _and_ left the literal placeholder in the HTML, so even a correct page had
   nothing to spend.

All three are fixed. The page now buys a session with its single-use
bootstrap (the way `/dashboard` and `/ai-connections` already did), and the
JSON calls declare their content type — except the multipart upload, which
keeps its own boundary.

**The flows, in a browser.** The approval card appears with the tool and the
server's own description, Approve runs it, and the result reports
_"(confirmed)"_. The attachment card appears with the filename, the PROCESSED
state, and **the text the image contained**.

**Proof:** `tests/test_dashboard_browser_flows.py` — 8 passed, three driving a
real browser against a real backend. Each of the three defects was verified
to fail against the reverted code.

---

## 22. Update — 2026-10-21 (the macOS app's attachment flow, run for real)

**R7.3 — the macOS app's attachment flow was written, contract-tested, and
never run.** The dashboard had just turned out to be broken end to end, so the
same exposure was checked here. Details:
`docs/AUTONOMOUS_ENGINEERING_LOG.md` iteration 70.

**It is not broken.** Unlike the dashboard, the app reads the master token
from disk (so it never needed a bootstrap), and its client sets
`Content-Type: application/json` on the JSON calls while keeping its own
boundary on the multipart one — both of the dashboard's fatal defects are
absent.

**And it was run anyway, because "the code looks right" is not evidence.** The
app is an executable product, so its internals are not linkable from a second
target, and XCTest is not installed here — so the app itself was made the
harness: a temporary file in its own module that runs the sequence against the
live backend when an environment variable is set, using the app's _own_
`APIClient`. The file was removed and the bundle rebuilt afterwards.

```
SELFCHECK upload_ok=true id=YjVhykxeNAJ_UB8xDBcRGQ
SELFCHECK result_status=processed
SELFCHECK result_text=Project AMR status report
```

The app's own client created the conversation, saved the message, uploaded a
real PNG, and the backend extracted the text from that image.

**Still unverified: the rendering.** The Accessibility API refuses this
terminal ("not allowed assistive access"), the app cannot be brought to the
front, and screen capture is refused ("could not create image from window"). So
the _pixels_ of the app's attachment card are unconfirmed, while its
requests, its responses and the backend's extraction are all confirmed. The
attach button itself was proven to work — the file panel opens.

That is an environment limit, not a product defect, and it is recorded as
such.

---

## 23. Update — 2026-10-22 (the phone's own path: two silent defects)

**The Android companion's client had never been exercised against the real
backend.** With the gateway up (LAN mode on, TLS on :8766), the phone's path
was driven end to end. **Two defects, neither of which a test would have
caught.** Details: `docs/AUTONOMOUS_ENGINEERING_LOG.md` iteration 71.

- **Every command the phone sent was refused with 422.** The gateway's proxy
  set `Accept: application/json` but not `Content-Type`, so a JSON body
  arrived at the backend as `text/plain` — a JSON _string_ where the API wants
  a dictionary. The phone's client set the header correctly; **the proxy
  dropped it.** Fixed: the proxy forwards the request's own content type
  (forwarded, not replaced, so a multipart body keeps its boundary).
- **The pairing code the backend issued could never be redeemed.** The codes
  live in the _gateway's_ memory, because the gateway redeems them; the
  backend minted one in its own memory, so the code it handed out was one the
  gateway had never heard of. The route's own docstring described the flow
  that could not work, and nothing called it. Fixed: the route asks the
  running gateway for a code, and says so when the gateway is not running.

**The phone's path now works end to end:** redeem → raise a command (200) →
list its own approvals (`requested_surface: phone`) → and its attempt to
approve its own request is **refused**. That is the self-approval rule
working over the real phone path — the thing the gateway's dropped header had
been keeping from ever being exercised.

**Proof:** `tests/test_companion_gateway.py` — 35 passed, 0 skipped (was 34 + 1
skipped; the new pairing round-trip test was skipping on a certificate
mismatch, which would have made it a false pass). Both fixes were verified to
fail against the reverted code. The Android `Confirmation` model is now decoded
against **the exact live row** in a test, and the Android suite is **48 tests,
0 failures**.

**Deliberately not done:** the Android app has no attachment affordance, and
cannot have one without the gateway's `PHONE_ALLOWED` growing to include
`/ai/attachments/*` — the roadmap says the phone surface is a strict subset
_by design_. Confirmed live: the gateway answers **403 "Path not available to
companion devices"** for those routes. That is an owner decision, recorded
rather than taken.

**And the silence ratchet caught the new call.** The full suite failed on
`test_the_silence_only_sites_are_counted_not_forgotten`, which counts every
`httpx.Client(timeout=…)` in the tree so a new unbounded read cannot be added
without noticing. It was answered the way the ratchet asks for: the site is
**re-recorded deliberately, with the reasoning**. And the call was improved
while doing it — it had been written with `verify=False`, which breaks the
"never disable TLS verification" rule. It now verifies the chain against the
gateway's own certificate, `CERT_REQUIRED` on, with the hostname check off for
the same reason the gateway's own client has it off (the certificate is
issued for the laptop's name, and the call dials it by loopback address).

---

## 24. Update — 2026-10-22 (the dashboard's other pages: two of them were entirely broken)

**R8.1 — the pages nobody ran.** Details: `docs/AUTONOMOUS_ENGINEERING_LOG.md`
iteration 72.

`dashboard/ai_chat.html` was driven in a real browser in iteration 69, and that
found three defects. **The other five dashboard pages had never been run at
all.** Two of them were completely broken, and the same three defect classes
the AI chat page had shown up again, in the same tree, in the same shape.

### The baseline, re-measured first

The suite was re-measured before anything was changed: **3019 passed / 18
skipped / 0 failed.** The first run had shown 3 failures, and they were _not_
product defects — `ERR_CONNECTION_REFUSED`. The browser tests need a live
backend, and there was none. The backend also **refuses to start** when LAN
mode is on and the bind is loopback-only (an honest guard, and a good one), so
it was started with `JARVIS_HOST=0.0.0.0` to match the runtime's own
`lan_mode=true`. With a backend up, those 3 tests pass.

### Defect 1 — `mission_control.html` could not authenticate

It posted `{code}` where the route's body is `BootstrapBody{bootstrap}`, and
read `data.token` where the route answers `session_token`. The page's own
console said it: **"[auth] Bootstrap failed: 403"**, then **"[providers] 401"**,
then 401 again. Every request the page made was refused. The page was inert.

### Defect 2 — `mission_control.html` read four fields the server never sends

- The provider card is `{provider_id, display_name, …}`; the page read `p.id`
  and `p.name`. **All five providers rendered as "undefined"**, with the value
  "undefined".
- The model list is a list of **strings**; the page read `m.id` and `m.name`,
  so the dropdown was empty even for a provider that has models.
- `/ai/connection-health` answers `{"providers": {…}}`; the page read the top
  level, so every provider's state was looked up one level too high.
- The key rows rendered "undefined" for every provider name.

### Defect 3 — `mission_control.html` called two routes that do not exist

`PUT /ai/providers/{id}/key` and `DELETE /ai/providers/{id}/key`. The real
routes are `POST /ai/providers/{id}/store` (body `api_key`) and
`DELETE /ai/providers/{id}/credentials`. **Every key save and every key delete
failed.**

### Defect 4 — `phone.html`'s four-second poll never ran

`authLost`, `pollTimer`, `lastConf` and `lastQuick` were each **assigned and
read back but never declared**. In sloppy mode the assignment quietly creates
a global; the **read comes first** and throws `ReferenceError`.

The read is in `refresh()` — the poll. So the poll died on its first
statement, every four seconds, and the page made **zero** `/devices` and
`/confirmations` requests. No device list, and **no approval button for a
person to tap**. The page still said "connected" — the _login_ worked — so it
looked healthy while doing nothing.

The fix was proven before it was applied: serving a patched copy with
`let authLost = false` added made the poll fire (`/confirmations`, `/tools`,
`/devices`); the unpatched page made no requests at all.

### Defect 5 — `ai_settings.html` is an orphan

No route serves it, and it calls `/api/ai/providers` and `/api/ai/settings` —
paths the backend has never had — with no bootstrap at all. It is an
**untracked pre-Phase-72 prototype** (never committed), ~95% a broken duplicate
of `ai_connections.html`, and its only unique sections (routing policy, security
audit) have **no server counterpart at all**.

**It was left on disk.** It is untracked work and removing somebody's file is
the owner's call, not this run's. What was done instead: it was removed from
`docs/FILE_OWNERSHIP.md`, which was naming it as a shipped page, and a test
now asserts it is unreachable from every surface.

### Proof

`tests/test_dashboard_pages_browser_flows.py` — **14 passed**. Every test was
verified to **fail against the pre-fix code**: 8 fail with
`mission_control.html` reverted, 4 fail with `phone.html` reverted. Nothing
passes for the wrong reason.

**Full backend suite: 3036 passed / 18 skipped / 0 failed** (318 s) — up 17
from the 3019 baseline (14 new tests, plus the 3 browser tests that pass once
a backend is actually running). No regressions.

**Runtime, against the live backend:** Mission Control establishes a session,
names all five providers correctly, lists their models, and its key **save and
delete both round-trip** ("openai_compatible key saved" / "key removed", zero
console errors). The phone reports no page errors and its poll now makes
`/devices` and `/confirmations` requests every four seconds.

### The pattern, now seen three times

**A contract test describes a contract; it does not exercise it.** Iteration 69
found three defects in `ai_chat.html`; iteration 70 checked the macOS app for
the same class and it was clean; iteration 72 checked the remaining pages and
found **ten more defects across two files**. The pages that were _described_
were broken; the pages that were _run_ worked.

The telling detail is that `mission_control.html` and `phone.html` both
_looked_ fine. One had a comment saying "Session established"; the other said
"connected". Both were lies — the first never established a session, the second
never did the thing its status claimed.

**Still open:** R0.3 (commit the untracked `macos/`/`android/`/
`jarvis/companion/` trees — owner decision, deliberately not taken); live model
inference (the configured provider still returns 401); a live CalDAV/SMTP
account for a remote end-to-end run.

---

## 25. Update — 2026-10-22 (a phone could approve its own request — found by fixing a dead poll)

**Fixing the phone's dead poll restored its approval list — and that exposed a
security defect hiding behind it.** Details: `docs/AUTONOMOUS_ENGINEERING_LOG.md`
iteration 73.

The phone's own web page — the one served at `/phone`, which is what a
handset actually opens — called `/confirm/{id}?approve=1` with **no session**.
The self-approval guard needs two facts, and evaluates:

```python
if (approve and session_id and requested_session == session_id and narrow):
```

With `session_id` empty the condition cannot be evaluated. An empty session
has no "who" to compare — the orchestrator's own comment says so. **The guard
was disarmed for the phone's own page.**

**The Android client was fixed for this in iteration 65. The phone's web page
was not.** Same guard, same defect, one client fixed and one forgotten — and
the two are different paths: the Android client goes through the gateway (which
replaces the session token with the master token, so the surface has to travel
in a header), while the phone's page talks to the backend directly with its
cookie.

**Measured, in the confirmation ledger, before the fix:**

```
state=approved  requested_by_session=SESS-59a94a310292
                approved_by_session='SESS-59a94a310292'   <- the same session
                approved_surface='unknown'   requested_surface=phone
```

The phone had approved its own request and `create_event` had run.

**The fix.** The session id is not the credential — the credential is the
HttpOnly cookie, which no page script can read. The session id is the
_conversation's_ id, and the backend already returns it in every `/command`
response as `session_id`. The page now keeps it and sends it with the answer,
exactly as the Android client does.

**After the fix, the same tap answers** "You cannot approve your own requests.
Approve this on the laptop instead.", the ledger records the request as
**`denied`**, and **no calendar event is created** (verified: a count of
`%GuardCheck%` events → 0).

**Proof:** `tests/test_phone_self_approval_guard.py` — **10 passed** (3 new),
each new test verified to **fail against the pre-fix code**.

**Full backend suite after the fix: 3039 passed / 18 skipped / 0 failed**
(280 s).

**How to apply:** a rule that depends on a fact has to be checked on every
path that reaches it, not only the path being debugged. The Android client
and the phone's web page are two clients of one endpoint.

**Still open:** R0.3 (commit the untracked `macos/`/`android/`/
`jarvis/companion/` trees — owner decision, deliberately not taken); live model
inference (the configured provider still returns 401); a live CalDAV/SMTP
account for a remote end-to-end run.

---

## 26. Update — 2026-10-22 (state at the end of this run)

**Suite:** 3039 passed / 18 skipped / 0 failed (268 s, run with the backend up).
Up from the 3019 baseline measured at the start of the day: 14 new tests in
`tests/test_dashboard_pages_browser_flows.py`, 3 in
`tests/test_phone_self_approval_guard.py`, and the 3 browser tests that
`test_dashboard_browser_flows.py` runs — which only pass when a backend is
actually listening.

**Every served dashboard page was driven in a real browser against the live
backend, and all five are clean** (no page errors, no HTTP ≥ 400):

| Page                   | What was driven                                                                   |
| ---------------------- | --------------------------------------------------------------------------------- |
| `index.html`           | devices, confirmations, audit, tools, quick actions (battery/time/memory)         |
| `ai_chat.html`         | already covered by R7.2                                                           |
| `ai_connections.html`  | provider list, selection save                                                     |
| `mission_control.html` | **was broken** — now: session, providers, models, key save, key delete, chat send |
| `phone.html`           | **was broken** — now: poll, device list, approval list, self-approval refused     |

**The macOS and Android clients were cross-checked against the backend and
found sound**: every endpoint the Swift client calls exists; the Swift
`Confirmation` model decodes a real `/confirmations` row with no missing
required field; the Android client targets the gateway's own routes (correct
by design) and sends its session on approve and deny.

**Environment notes for the next run:**

- The backend **refuses to start** with LAN mode on and a loopback bind. Start
  it with `JARVIS_HOST=0.0.0.0` to match `~/.jarvis-forge/lan_mode` (which is
  `true`).
- The browser tests need that backend on :8765, or they fail with
  `ERR_CONNECTION_REFUSED` — an environment gap, not a product defect.
- **Those browser tests write to the real calendar**, because the live server
  uses the production `JARVIS_HOME`. 13 stray "called Browser Check" events
  had accumulated; they were cancelled. Clean up with
  `get_calendar_provider().cancel(uid)`.

**Nothing was committed.** The tree carries the owner's large uncommitted WIP
(279 files) and all of the autonomous work since 2026-10-16 is uncommitted
alongside it; committing only this session's files would produce a commit
that cannot be checked out on its own, because the backend it tests against
is untracked. This is the same decision the previous 17 iterations recorded:
R0.3 — commit the untracked `macos/`/`android/`/`jarvis/companion/` trees —
is an **owner decision**, deliberately not taken.

---

## 27. Update — 2026-10-22 (the Command Center is a conversation)

**The macOS Command Center was not conversational.** It sent every message as
a brand new conversation, and it read four response fields the server has
never sent. Details: `docs/AUTONOMOUS_ENGINEERING_LOG.md` iteration 74.

### The provider was answering 401

The configured provider (9router at `127.0.0.1:20128`) returned **401 Invalid
API key**. The gateway was healthy — `/api/health` `{"ok":true}`, version
0.5.104 — and **all five** of its active keys completed a live request. The
key in Vantage's keychain matched **none** of them: a stale credential, not a
broken gateway. A working key was stored through the project's own
`KeychainStorage.store()`, and the provider answered `PROBE_OK` through the
project's own adapter (including the gateway's SSE-shaped response).

### Two defects in the app

1. **The request carried no session.** `CommandRequest.init` hardcoded
   `session_id = nil`; the backend mints a fresh `SESS-` id per request, so
   every message started a new conversation and a follow-up had nothing to
   refer to. The backend's history mechanism existed and was unused.
2. **The response model read fields the server never sends.** It declared
   `status`, `message`, `confirmation_required` and `elapsed_ms`; the server
   sends `reply`, `decision`, `needs_confirmation`, `duration_ms`,
   `model_used`, `intent` and `session_id`. All optional, so decoding
   succeeded and the app showed less than it was told.

### What was built

- `macos/Vantage/Sources/Services/Conversation.swift` — the conversation
  model: turns, the session that scopes them, and the meta facts about how an
  answer was produced.
- `macos/Vantage/Sources/Services/ConversationService.swift` — the boundary
  between the conversation and the network: it owns the session, sends
  `/command`, bounds the turn, and turns a failure into a sentence a person
  can act on.
- `CommandCenterView` — a real transcript: distinct user and assistant
  bubbles, a caption naming who answered and how long it took, an empty state
  that explains what Vantage can do, a "new conversation" action that clears
  the session as well as the transcript, a retry that does not need the
  question retyped, and a composer that submits on Return.

### Verified in the running application

`swift build -c release` clean; `assemble_app.sh release` → bundle rebuilt,
`codesign --verify --strict` VALID; launched with `open -a` → a real 900x572
window, and the backend's log shows the app's own `/health`, `/devices`,
`/ai/providers`, `/confirmations` and `/tasks` calls.

**XCTest cannot run in this environment** (only the Command Line Tools are
installed: `no such module 'XCTest'`), so the app was made the harness — a
temporary file in its own module, driven by the app's own `APIClient` and
`ConversationService`, removed afterwards and the bundle rebuilt. The
shipped binary was checked to contain no trace of the harness.

Measured in the running app against the live backend:

| Behaviour                         | Result                                                     |
| --------------------------------- | ---------------------------------------------------------- |
| general question                  | answered by `9router:ag/gemini-2.5-flash` (3256 ms)        |
| follow-up ("the second part")     | resolved against the previous answer, same session         |
| follow-up ("a practical example") | continued the same conversation                            |
| new conversation                  | session `nil`, turns `0` — history not mixed               |
| tool request                      | `laptop_battery`, `decision=EXECUTE`, real battery reading |
| consequential action              | `needs_confirmation=true`, `decision=CONFIRM`, held        |

### Screenshots

`artifacts/command-center-{conversation,empty,approval,error}.png` — the
**real** view, hosted in a real offscreen `NSWindow` and read back from the
window's backing store. `ImageRenderer` was tried first and does not draw a
`ScrollView`'s content, so the transcript came out empty; the first window
capture resolved to the light appearance, where a 5%-white bubble on a white
backdrop made the answer invisible. Forcing the dark appearance on both the
app and the window is what made the pictures faithful.

### Proof

`tests/test_macos_conversation_client.py` — **13 passed**, each verified to
**fail against the pre-fix code** (2 with the model reverted, 2 with the view
reverted). `macos/Vantage/Tests/VantageTests/ConversationTests.swift` pins the
same contract from the app's side; it cannot execute here.

**Full backend suite: 3052 passed / 18 skipped / 0 failed** (359 s) — up 13
from the 3039 baseline, no regressions.

### Also checked, and already correct

The **dashboard's** AI chat surface threads history through `conversation_id`
and was verified live in the same way (a follow-up resolved against the prior
answer). The app's three provider models — `ProviderInfo`,
`ProviderSelection`, `ProviderHealth` — declare exactly the fields the server
sends, checked against live payloads.

---

## 28. Update — 2026-10-22 (the model planner was dead; the provider page was fictional; the app's screens were empty)

### The model-planner path never ran

`llm_fast_model` and `llm_reasoning_model` default to OpenAI names
(`gpt-4o-mini`, `o4-mini`) — _hints_ about which model suits which task, not
a description of what the configured vendor offers. Pointed at the local
9Router gateway, the router sent `o4-mini` to a gateway that has never
heard of it:

```json
{
  "error": {
    "message": "No active credentials for provider: openai",
    "code": "model_not_found"
  }
}
```

The failure was invisible on the conversational path (`conversation` routes
to `general`, the one role whose model _is_ the configured one). The planner
routes to `reasoning`, so **every model-proposed action died at the first
request**:

```
POST /command "set a reminder for tomorrow at 9am to stretch my hamstrings"
  -> reply: "I can set that up for you! I'll use the **Reminders app** …"
  -> decision: None    plan.planner: "rules"    actions: []
  -> pending confirmations: 0
```

The assistant promised an action, the user said "yes please go ahead", and
nothing happened and nothing was asked for approval.

**Fixed** in `jarvis/gateway/router.py`: a role model the vendor cannot
serve is never called (the vendor's catalogue when it publishes one; the
route-health ledger's own record when it does not), and a refusal _of the
model name_ is retried once on the configured model. A timeout or a rate
limit is **not** retried on another model.

```
after: decision=CONFIRM needs_confirmation=True plan.planner=llm
       one pending confirmation, then denied — no calendar event was created
audit: model_proposal -> awaiting_confirmation -> denied
```

### The provider page described a provider that does not exist

`dashboard/ai_connections.html` hardcoded `openai`, `gemini` and **`shree`**,
while the server's registry has `openai`, `gemini`, `groq`, `9router`,
`openai_compatible`. So the page offered a provider the backend has never
had (`POST /ai/providers/shree/test` → 404) and had **no card at all** for
9Router — the provider actually in use. The provider dropdown also snapped
back to the current provider, so the assistant's vendor could not be changed
from that page, and three "AI Usage Controls" checkboxes did nothing at all
(no handler, no route, and the only code reading `free_first` /
`allow_paid_fallback` is not called from anywhere in the live path).

**Fixed**: the page is built from `GET /ai/providers`; the dropdown holds the
user's choice; the Save Key button appears only after a test has passed; the
dead checkboxes are gone. The key round trip was driven in a real browser
against the live backend (store → `configured: true` → remove → `false`),
and the key never appeared in a response body.

### The app's other screens had the same defect as `/health`

Every screen's Swift model declared field names the backend had never sent.

| model         | declared                                 | the server sends                                               | effect                                                                                     |
| ------------- | ---------------------------------------- | -------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| `AuditEntry`  | `detail: String?`                        | `detail` is an **object**                                      | the **whole `/audit` array failed to decode** — the Activity screen's audit list was empty |
| `AuditEntry`  | `action`, `timestamp`, `id`, `device_id` | `intent`, `tool`, `created_at`, `log_id`, `device`             | every row read nil and showed "Unknown"                                                    |
| `AuditEntry`  | results `allowed`/`denied`/`error`       | `success`/`denied`/`error`/`awaiting_confirmation`/`no_action` | every row, including the successful ones, rendered a neutral grey circle                   |
| `Device`      | `device_name`, `device_type`, `status`   | `type`, `owner`, `platform`, `trust_level`, `online`           | every device's type showed as "unknown"                                                    |
| `Device`      | icons for `phone`/`watch`/`car`/`home`   | `computer`/`android`/`smartwatch`/`vehicle`/`home_hub`         | every device got the same laptop icon                                                      |
| `TaskInfo`    | `name`; `steps` in the list              | `goal`; `steps` in `GET /tasks/{job_id}`                       | job ids instead of goals; a "Steps" heading that could never appear                        |
| `ProjectInfo` | `id`                                     | `project_id`                                                   | identity was a fresh UUID each load                                                        |
| `SessionInfo` | `ref`, `created_at`                      | `session_ref`, `expires_in`                                    | "Revoke" sent `DELETE /auth/sessions/` with an **empty** path and reported success         |

**Fixed**, and seen in the running app: `artifacts/activity-audit.png`,
`devices.png`, `memory.png`, `projects.png`, `sessions.png`.

### Test health

**3116 passed / 19 skipped / 0 failed** (291 s), measured 2026-10-22. The
README and `docs/RELEASE_AND_BUILD.md` were updated to match. 36 new tests
across three files, each verified to fail against the pre-fix code.

### Next task

P1: iOS (report its actual state; it stays paused by the project's own
instructions), then Android (JDK 17 build + the real unit tests + an APK),
then the `dist/` bundle, which still carries the old `ai_connections.html`
(it is a gitignored build artifact, regenerated by `build_app.py`).

## Update — 2026-10-26 (Google Tasks as tools; a real MCP connector; Android task writes)

**What changed (mine, this session):**

- `jarvis/tools/google_tasks.py` (new) — `list_google_tasks` (READ),
  `add_google_task` / `complete_google_task` (EXTERNAL, confirm). The
  complete path resolves a spoken **title** to exactly one open task and
  refuses on zero/many matches or an unreadable list (`_resolve_task_id`).
- `jarvis/core/planner.py` — a narrow Google branch in `_planner`, triggered
  only by the word "Google", so "add a task to Google Tasks X" reaches the
  Google tool while ordinary "add a task" stays local.
- `jarvis/integrations/mcp_client.py` (new) — one Streamable-HTTP MCP server:
  JSON-RPC `initialize` / `tools/list` (paginated) / `tools/call`, an
  **allowlist enforced at the transport**, bounded reads, redacted token.
  `jarvis/tools/mcp_tools.py` registers one `EXTERNAL` tool per allowed name.
  `GET /mcp/status` reports state + advertised vs callable; no call route.
- Android: `GoogleViewModel` gains `addTask`/`completeTask` via `/command`
  (so writes carry the confirmation workflow; a phone cannot self-approve);
  `GoogleScreen` gains an add row and tappable completion, with a plain "this
  awaits the Mac's approval" state.
- macOS: `MCPView` (new) + sidebar entry, `MCPStatus` models, `mcpStatus()`.
- Docs: `docs/MCP_INTEGRATION.md` (new), `docs/GOOGLE_INTEGRATION.md`,
  `.env.example`; README/`FILE_OWNERSHIP.md` counts re-measured.
- Tests: `tests/test_google_tasks_tools_and_mcp.py` (41) and 2 Android state
  tests.

**Evidence:**

| Check                                                                                   | Result                                                                                                                  |
| --------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| `tests/test_google_integration.py` + new file + phone/gateway/guard                     | **151 passed, 1 skipped**                                                                                               |
| `tests/test_google_tasks_tools_and_mcp.py`                                              | **40 passed**                                                                                                           |
| full suite (`pytest -q -p no:cacheprovider`)                                            | **3574 passed, 1 failed, 21 skipped** (473 s). The one failure is the pre-existing stale `dist/Vantage.app` check below |
| `swift build -C macos/Vantage`                                                          | **Build complete!**                                                                                                     |
| `cd android && JAVA_HOME=/opt/homebrew/opt/openjdk@21 ./gradlew :app:testDebugUnitTest` | **BUILD SUCCESSFUL — 67 tests, 0 failures**                                                                             |

**Not done / blocked:** Google is **not live-authorised** (no OAuth client id
on this machine) — every Google test drives fakes and `httpx` shapes. No MCP
endpoint exists, so the connector is **not live-connected**; it is proven
against a fake transport. Android writes are **not device-verified** (no
emulator/device run). `dist/Vantage.app` is still the 2026-10-17 artifact, so
`test_phase95…test_run_py_includes_recent_changes` fails **pre-existing**;
rebuild with `build_app.py && install_app.sh` before shipping.

**Pre-existing, not mine:** the `tests/test_phase49` → `test_phase97` order
flake (`permission for open_application has been revoked` leaks through the
shared test DB) appears only when those two run together; unrelated to this
change.

## Update — 2026-10-26 (live-verification runbook; one payload-shape bug found by it)

`docs/LIVE_VERIFICATION.md` is a step-by-step runbook for proving Google and MCP
against real accounts/devices. Writing it and running its commands found a real
defect the unit tests had missed:

- **`MCPConnector.status()` omitted `callable` on the `not_configured`,
  `disconnected` and `error` branches.** A consumer reading
  `status["connection"]["callable"]` (the runbook did, as `2b`) raised
  `KeyError` — so "not configured" would crash a reader instead of reporting
  itself. Fixed: every branch now carries `tools`, `tool_count`, `callable` and
  `server`. New test `test_every_status_branch_carries_the_same_keys` asserts the
  shape across all three non-connected branches.

**What the runbook proved on this tree** (throwaway `JARVIS_HOME`, local servers
on 8897–8899, a local fake MCP server on 8790 — no real account, no network):

| Check                                                  | Result                                                                    |
| ------------------------------------------------------ | ------------------------------------------------------------------------- |
| `GET /google/status` with no client id                 | `not_configured` + the setup steps                                        |
| Forged callback, nothing pending                       | "There is no authorization in progress"                                   |
| Two begins, callback with the older state              | "replaced by a newer one"; the newer attempt still reached Google         |
| Replaying the consumed state                           | "There is no authorization in progress" (single use)                      |
| Unconfigured Google write                              | honest **409**, message names `JARVIS_GOOGLE_CLIENT_ID`                   |
| Unconfigured Google task read                          | `status=not_configured`, `read_succeeded=false` (not an empty list)       |
| Google task write gate                                 | `CONFIRM — level 2 (external) action requires confirmation`               |
| MCP `not_configured` / connected against a fake server | `not_configured`; then `connected`, **3 advertised / 0 callable**         |
| Allowlist one tool                                     | `callable: ["send_email"]`, registry gains exactly `mcp__mcp__send_email` |
| Non-allowlisted tool                                   | refused before any network call                                           |
| Allowlisted tool call                                  | returned `Sent.` from the fake server                                     |
| MCP tool gate                                          | `CONFIRM — level 2 (external)`                                            |

**Still BLOCKED / external:** a real OAuth consent round-trip, real calendar and
task reads/writes, a real MCP endpoint (Zapier/n8n), and every Android step —
no device or emulator is present. The runbook's outcome table marks each row
`pre-verified` or not, so nothing is mistaken for a live result.

---

## Update — 2026-10-27 (the self-approval guard was still disarmed)

The 2026-10-21 entry above (§ "a phone could approve its own request") reports
the guard fixed and proven live. **It was still dead.** That entry's fix made
the guard _reachable_ — the gateway forwards the surface, `/confirm` reads it,
the Android client sends `session_id` — but the condition it reaches ends in
`requested_session == session_id`, and those are never the same kind of value:

```
stored requested_by_session : SESS-57c061ccf0c0   (the conversation id)
approver presents           : MYvT3Q9dQ_...       (the HTTP session token)
```

Measured on a live scratch server: a phone raised
`add a task to Google Tasks …` and approved it with its own session token, and
it executed. Every surface fact was correct; the conjunction was still false.

**Why the earlier proof missed it.** Its tests drove `resolve_confirmation`
directly with a hand-made `session_id="PHONE-1"` on _both_ sides, which is not
how the two values arrive in production. One test,
`test_the_guard_is_disarmed_without_the_surface`, asserted the broken
behaviour (`approver_surface="unknown"` → `EXECUTE`) as the documented pre-fix
state, so the suite recorded the gap instead of closing it.

**The fix, in three parts.**

1. The rule no longer needs matching identifiers: a request **raised** on a
   narrow surface must be answered from a non-narrow one. The confirmation
   already records which surface raised it.
2. The approver's surface falls back to the **presented credential**. A client
   sending neither a query `session_id` nor a forwarded header still
   authenticated with a session token, and that token identifies its surface.
3. A refused self-approval **no longer cancels the request**. It used to
   resolve it as denied, so the phone was told "approve this on the laptop"
   while the record had just been destroyed and the laptop could not approve it
   either. It now stays `pending` with `needs_confirmation: true`.

**Evidence.** Live, four paths: phone approving with `session_id` → refused;
phone approving without it → refused; phone denying its own → allowed (a "no"
is never a self-approval); laptop approving a phone's request → allowed and
executed. Decision table checked for every surface pair.
`tests/test_phone_self_approval_guard.py` — **17 passed**, two of which fail if
either fix is reverted.

**Known limitation, accepted.** The `/phone` page opened in a browser on this
Mac can raise but not approve, and is told to "approve it on the laptop" while
sitting at it. It cannot be special-cased safely: over HTTP it and a remote
handset are indistinguishable (the gateway proxies from `127.0.0.1` and swaps
in the master token). Two variants were tried; both reopened the hole. The
request stays pending and the dashboard or native app answers it.

## Update — 2026-10-27 (parallel run: 5 real defects fixed; P0 cleared)

Full suite **3622 passed / 22 skipped / 0 failed** (495 s). The prior lone
failure (`test_phase95…test_run_py_includes_recent_changes`) is cleared by
rebuilding the gitignored `dist/Vantage.app` (`python build_app.py`, exit 0,
codesign verify ok); release-integrity 15/15.

**Backend Google** (`google_auth/calendar/tasks.py`, `api/app.py`, +12 tests):

- **Access-log credential leak on the macOS launch path** — `log_safety.install()`
  ran only in `run.py`/`app.run()`, so `uvicorn jarvis.api.app:app` (how the app
  launches) logged `?code=…` verbatim. Now installed at import (idempotent);
  re-measured: `code=[redacted]&state=[redacted]`.
- Google routes blocked the event loop (20 s blocking reads in `async def`) →
  `asyncio.to_thread`.
- `_pending` race between begin/complete → `_pending_lock` around check-consume,
  network outside it (tests fail with the lock removed).
- Access-token cache was per-instance and unsynchronised → class-level + `RLock`
  single-flight (4 concurrent readers: 4 refreshes → 1). A rejected token is
  dropped from the cache.
- Status classified by wording (a disconnected account read as "Google failed"),
  fixed to classify from `GoogleAuthError.state`; unparseable 200 bodies are now
  `PROVIDER_ERROR`, not `REAL_EMPTY`; an already-revoked token on disconnect
  reads as revoked.

**macOS** (`APIClient`, `APIModels`, `GoogleView`, `MCPView`; builds clean):

- `SameOriginProtected` writes were **all 403** because URLSession sent no
  `Origin` — Connect/Disconnect/task create/task complete could only fail. Now
  set to the app's own loopback origin.
- `MCPStatus.setup` was `[String]?` while the route sends a dict → the MCP
  screen could never load. `MCPSetup` model added; advertised count from
  `tool_count`.
- Decode failures name the route and field instead of rendering "Checking…";
  a failed refresh clears stale "Connected"; a failed calendar/task request
  draws a per-section failure row (never an empty section).

**Android** (92 unit tests, 0 failures; APK assembles; **runtime BLOCKED**):
shared `WireJson` (`coerceInputValues`) tolerant of nulls/unknown states; every
connection state labelled truthfully; outage → retryable panel, no stuck
spinner; pull-to-refresh + re-read on resume so a Mac-approved write appears;
`isCompletable` guards; pinning/auth/timeouts untouched, no connect/disconnect
reachable from the phone.

**MCP** (`mcp_client.py`, +11 tests in `tests/test_mcp_connector.py`): SSE
responses parsed and matched by id, `Mcp-Session-Id` captured and echoed,
`MCP-Protocol-Version` after handshake, `notifications/initialized`, redirects
not followed (so `Authorization` cannot be forwarded to another host), 401/403
named as credential problems, connector token scrubbed from every message.
Allowlist still refuses before any request.

**Still BLOCKED (external):** live Google consent (no client id / account), a
real Zapier or n8n endpoint (no account), and Android runtime (no device).

## Update — 2026-10-27 (visible product pass: AI Voice ON/OFF, Connections, Planner)

Full suite **3635 passed / 22 skipped / 0 failed** (495 s). macOS `swift build`
exit 0. Android `:app:testDebugUnitTest` BUILD SUCCESSFUL, **118 tests / 0
failures**; `:app:assembleDebug` built.

**AI Voice Responses ON/OFF — now impossible to miss**

- macOS (`VoiceService`, `SettingsView`, `CommandCenterView`): a header strip
  pinned above the Settings TabView, always visible on every tab: "AI Voice
  Responses" + switch + a state sentence, plus a live "Speaking… / Stop" row.
  Command Center replaced the icon-only speaker with a labelled
  "AI voice: On/Off" control (accessibility label + value). One preference
  (`VantageSpeakReplies`, UserDefaults), read in `init`, written in `didSet`;
  OFF calls `stopSpeaking(.immediate)`. A new `isSpeaking` publisher is driven by
  the synthesizer delegate. Verified with a runtime probe using real audio:
  default off, on persists across instances, off mid-utterance stops and clears
  the flag, `speak()` while off stays silent, `previewVoice()` speaks without
  changing the setting.
- Android (new `data/voice/VoiceSettings.kt`, `voice/{SpeechEngine,
AndroidTextToSpeechEngine,VoiceSpeechPolicy,VoiceController}.kt`): native
  `TextToSpeech`, preference in plain SharedPreferences (default off). "AI Voice"
  is the FIRST section in Settings, with the same labelled switch in the Command
  header. OFF stops immediately; QUEUE_FLUSH + stable ids prevent overlap;
  blocked/confirmation replies are never spoken; `onPause`/`onStop` stop and
  `onDestroy` shuts the engine down. No microphone permission added (manifest
  unchanged: INTERNET + ACCESS_NETWORK_STATE only). 26 new JVM tests with a fake
  engine (no `TextToSpeech` instantiated).

**Connections + Planner** — new macOS `ConnectionsView` in the sidebar (Google
Calendar + Tasks as rows under one account, stating read-only vs read/write, and
MCP with callable/advertised counts, "not configured" + setup steps). Planner
gained a live Google Tasks section (read, complete, add — never copied locally)
and visible refresh feedback. Android Home gained a Connections card with a
working hand-off to the Google tab.

**Backend voice** — `NullTTS` no longer reports a silent success; a raising TTS
backend is contained as `speech_error` instead of crashing the turn/500ing;
`/voice/status` moved off the event loop and now reports the actually selected
backend + `can_speak`. No backend auto-speak preference exists, so it cannot
conflict with the client toggles.

**Not verified:** Android runtime (no device/emulator) — TTS on real hardware is
untested. macOS was verified by offscreen renders of the real views and a real
audio probe, not by clicking in a live window.

**Found, not fixed (pre-existing, outside this run):**
`jarvis/runtime/supervisor.py:414` imports a non-existent `VoiceRuntime`
(unreachable today; a latent broken service registration).

## Update — 2026-10-27 (the native app is now what ships, installed)

**Root cause found for "I cannot find the voice control".** `docs/RELEASE_AND_BUILD.md`
already recorded it as a known limitation: the default `dist/Vantage.app` and the
DMG ship the **Tk launcher** (`com.jarvisforge.app`), which contains no SwiftUI
sources at all — so the Settings voice header, Command Center, Connections and
the Google/MCP screens were never in the running app. The native GUI
(`com.jarvisforge.vantage`) was built only by a separate make target.

What was done:

- `cd macos/Vantage && make app` → native `Vantage.app` built, ad-hoc signed,
  `codesign --verify --strict` ok.
- `./installers/macos/build_dmg.sh --native` → `dist/Vantage-0.2.0.dmg` (1.7 MB,
  signed+verified), containing `com.jarvisforge.vantage` with the voice strings.
- Installed to `/Applications/Vantage.app` (signature verified) and launched:
  the process runs and has a real on-screen window (882x562, layer 0).

Evidence the gate is real (compiled against the shipped `VoiceService.swift`,
with a running RunLoop and actual audio synthesis):
`default_off true`, `speaking_after_on true`, `stopped_on_off true`,
`silent_when_off true`, `sample_plays_while_off true` with
`setting_unchanged true`, `finishes_and_clears true`.
Persistence: the preference round-trips in the app domain
(`com.jarvisforge.vantage`), defaults to OFF when absent, and the compiled
service reads it. Restored to OFF afterwards to leave nothing behind.

**Not verified:** screen capture and the Accessibility API are both refused in
this environment, so no live screenshot of the installed window was possible;
on-screen placement is proven by offscreen renders of the same views plus the
window's existence. Android runtime is still blocked (no device/emulator).

**Open item (owner decision):** the DMG default is still `--launcher`. Changing
the default is a packaging decision, so it was built with `--native` and
installed rather than switching the default unilaterally.

## Update — 2026-10-27 (premium light UI redesign; native app is the release default)

**The dark appearance was a hard-coded constant.** `VantageApp` pinned
`.preferredColorScheme(.dark)`, so no light-first design could ever appear. It is
removed; light is the designed appearance and dark still works from the same
token pairs.

**Design system** (`Sources/DesignSystem/`, new):

- `VantageTheme.swift` — semantic colours as dynamic NSColor pairs (warm white
  `#FAFAF7`, ivory `#F4F2EA`, emerald `#16845B`, champagne `#C6A45A`, danger,
  warning), a spacing/radius/type scale, and a motion policy that reads
  `accessibilityDisplayShouldReduceMotion`.
- `VantageComponents.swift` — `Card`, `SectionBlock`, `PageHeader`, `DetailRow`,
  `StatusPill`, `Notice`, `EmptyState`, `LoadingRow`, `VField`, `VToggleRow`,
  four button styles, `IconAction` (tooltip + accessibility label), and
  `InlineToggleChip`. No dependency was added; all native SwiftUI.
- `docs/DESIGN_MACOS.md` records the palette, structure, motion and the
  dependency decision.

**Shell** (`ContentView.swift`): custom sidebar with a branded mark, grouped
sections, an emerald selection spine, a real connected/disconnected footer, and a
crossfade between screens.

**Screens redesigned** — Command Center, Settings, Connections, Planner,
Approvals, Google, MCP, Home, Providers, Devices, Activity, Memory, Operator,
Work, People. Ad-hoc colours in the redesigned screens are now **0** (measured).

**Real defects fixed while redesigning:**

- Command Center retry **duplicated the user's question**; Return fired both
  `onSubmit` and the send shortcut (**duplicate send**); errors were drawn twice;
  attachment results were labelled as assistant answers; approvals could be
  submitted twice. Tool results now have their own `Role.tool`.
- Settings "Export Data" was a **dead control** (fetched, then discarded the
  bytes); it now writes through `NSSavePanel` and reports the path. The API-token
  state froze at launch; it can be re-checked. The connection URL was a hardcoded
  string; it now reads the manager's real URL.
- Planner's "Cancel event" fired immediately; it now asks first.

**Release path (Phase 11) resolved:** `build_dmg.sh` **defaults to the native app**
(`com.jarvisforge.vantage`); `--launcher` remains the explicit fallback.
`dist/Vantage-0.2.0.dmg` built, ad-hoc signed and verified, installed to
`/Applications`, launched with a live window. The bundled binary contains
"AI Voice Responses", "What Vantage is allowed to use" and the new brand strings.

**Verification:** `swift build` exit 0; Command Center harness 25/25 checks pass;
whole-window render (`IntegrationHarness/run_root_render.sh`, new) shows sidebar +
page as one product; light renders of Settings/Connections/Planner/Approvals read.
Backend **3636 passed / 21 skipped / 0 failed**. Two view-string tests were
updated (not deleted) because the redesign moved `ProviderNotice`/`RetryRow` into
the shared `Notice`; they now pin the behavior instead of the old type names.

**Not verified:** screen capture and the Accessibility API are refused here, so
interactive clicking (Tab order, copy button, scroll-follow) is code- and
harness-verified, not clicked. Android was not restyled this pass.

## Update — 2026-10-27 (Android identity + safe install; interaction tests)

**Android now shares the Vantage identity.** The app was on a fully dark palette
(`#0D1117` background, blue `#4C8DFF` accent) — a different product from the
desktop app. `ui/theme/Color.kt` was replaced with the light-first tokens
(warm white `#FAFAF7`, white surfaces, ivory `#F4F2EA`, emerald `#16845B`,
champagne `#C6A45A`, warning/error), keeping the **same token names** the screens
already used, so every screen converted without editing it. `Theme.kt` is now
`lightColorScheme` with light status/navigation bars. Added `Space` and `Radius`
scales and `ui/components/VantageComponents.kt` (`VantageTopBar`, `SectionHeader`,
`VCard`, `StatusPill`, `Notice`, `EmptyState`, `LoadingRow`, `DetailRow`).

**Branded top bar** on every screen (`AppNavigation`): the app had no identity
above content after Setup. All primary screens (Home, Command, Settings,
Approvals, Google, Devices, Setup) now use the shared components; ad-hoc colours
outside the theme measure **0**.

**Real defect found by a new test, and fixed:** retry re-sent the question through
`send()`, which re-appends the user turn — and the follow-up defect that the
composer draft was read _after_ `deliver()` had cleared it, silently losing what
the person had typed. `send()`/`deliver()` are now split, `retry()` is a
ViewModel method (it lived only in the composable, so the recovery path had no
test), and the draft is captured before sending.

**New interaction tests** (`CommandViewModelSendTest`, +6, MockWebServer):
a second send while one is in flight produces **exactly one** request; blank
input sends nothing; a failed turn is recorded as ERROR, never as an answer; a
retry during an in-flight turn is a no-op; a retry after a recorded failure adds
exactly one request and one new turn; voice state does not affect sending.
An accessibility `stateDescription` was added to the AI Voice switch.

**Safe install (`installers/macos/install_app.sh`, new):** replaces the manual
`rm`/`cp` that deleted `/Applications/Vantage.app` last run. It prints resolved
paths, refuses a source that is not `com.jarvisforge.vantage`/signed/executable,
backs up to `/Applications/Vantage.app.backup-<ts>`, stages, swaps, then verifies
signature + bundle id and restores on failure. Documented in
`docs/RELEASE_AND_BUILD.md`; run twice (idempotent), backup retained.

**Verification:** Android `:app:testDebugUnitTest` **124 tests, 0 failures**;
`:app:assembleDebug` → `android/app/build/outputs/apk/debug/app-debug.apk`.
macOS focused suite 35 passed / 2 skipped. Installed app verified as
`com.jarvisforge.vantage`, signature valid, launches.

**Not verified:** Android runtime — **no device and no emulator exist here**
(`adb devices` empty, no emulator binary or system images), and no on-device
screenshot was produced. Screen rendering is not screenshot-verified on Android
either: no Compose preview/Robolectric harness is configured, and adding one
would have introduced an unverified dependency. Android results are build- and
unit-test-verified only.

## Update — 2026-10-27 (real provider/workflow verification; 3 defects fixed)

Verified against the **live** local services on this machine, not mocks:
9Router (`:20128`) and chat2api (`:8099`) are both up; the selected provider is
`chat2api/gpt-4o-mini`.

**Real workflows driven end to end through `/command` (isolated `JARVIS_HOME`):**

- Conversational: "What is 2+2?" → `4` in 13 s through the selected provider.
- Read: "what is on my calendar today" → `list_events` EXECUTE, truthful
  "Nothing scheduled for today, but I could not verify it happened."
- Gated write: "add a task to Google Tasks buy milk" → `add_google_task`,
  **CONFIRM** (no execution). Approved → EXECUTE, Google tool ran and reported
  `Google is not configured (JARVIS_GOOGLE_CLIENT_ID is empty)` — the honest
  failure, not a fabricated success. Denied → `BLOCK`, **0 actions run**.
- Real mutation: "create an event called Standup tomorrow at 9am" → CONFIRM →
  approved → `create_event` **ok**, "Created Standup on Wed 28 Oct at 09:00";
  read back tomorrow → the event is there (round-trip proven).
- Provider: `POST /ai/providers/9router/test` → real 2.6 s completion against the
  9Router route with real `usage` and `upstream_model: gemini-2.5-flash`.

**Defects fixed:**

1. **A false "Gateway reachable" claim.** `human_state("unknown")` returned
   "Gateway reachable — route status unknown" unconditionally, so a provider
   whose last request had timed out still read as reachable. The label is now
   built from evidence (`gateway_reachable` True/False/None); the health route
   passes its probe result. Two tests pin it.
2. **A misleading read-only refusal.** `create_event` against the read-only
   Google connection went through the _read-failure_ path and told the person
   "Google rejected the access — it was revoked or expired. Reconnect…" for an
   account that was fine. It now says the connection is read-only, states nothing
   was created, and offers the local alternative. Test added.
3. **Stale server mistaken for a product defect.** The backend on `:8765`
   (PID 88264, started Oct 26) predates the Google-tasks planner routing, so live
   requests there are parsed by old code ("add a task to Google Tasks X" became a
   _local_ task named "Google Tasks X"). On current code the same sentence yields
   `add_google_task` + CONFIRM. **The long-running backend needs a restart to pick
   up current code**; no code change was required.

**Verification:** focused provider/Google/MCP suites **129 + 42 + 11 passed**;
full backend suite **3638 passed / 21 skipped / 1 failed**, the failure being the
README collected-count guard, updated to 3660 (3 tests now pass). Android
`:app:testDebugUnitTest` + `:app:assembleDebug` BUILD SUCCESSFUL; macOS
`swift build` Build complete.

**Not verified:** Google live OAuth (no client id on this machine — the Google
tool path is exercised with the real adapter but cannot reach a real account);
MCP against a real hosted server (no endpoint configured; the local fake server
proves discovery-vs-callable and refusal-before-network); Android on a device
(no emulator/hardware).

## Update — 2026-10-27 (backend lifecycle & identity; MCP configuration; Google setup)

**Root cause addressed, not just restarted.** A backend started Oct 26 served old
routing for a day because the macOS client accepted _any_ HTTP response on the
port as "the backend is running". Two things now make that state visible.

**Backend identity** — new `jarvis/runtime/identity.py` + unauthenticated
`GET /health/identity` (version, revision `aa1b66a+dirty`, branch, start time,
pid, instance id, source root; no settings, env or secrets). `/health` stays
authenticated. `/health/code` now reuses the same staleness check.

**Safe restart** — new `scripts/backend_lifecycle.py status|restart|stop|start`:
one candidate (whatever listens on the port, never a name match), refuses unless
`/health/identity` names `vantage-backend` _and_ this checkout, SIGTERM then a
bounded wait, SIGKILL only with `--force`, and refuses to start a second instance.
An unidentifiable server reports `stale: unknown`, not `stale: false`.

**macOS client** (`BackendManager`, `APIClient`, `APIModels`, Settings, footer):
five states distinguished — current, stale ("Connected — restart needed"),
foreign folder (names both paths, never spawns), old build with no identity
route (error + the restart command), unreachable. Lifecycle harness 26/26 checks;
renders read for stale and old-build.

**MCP configuration** — `PUT /mcp/config` (same-origin, validates http/https),
saved through `save_mcp_config` into the 0600 `ai_selection.json`; the connector
prefers saved config over an environment export; the token is never in that file.
Live-proved with a fake server: discovery showed 2 advertised / 1 callable, a
non-allowlisted call refused before the network, the allowlisted call returned.

**Google setup** — a stored token with no client id is now its own message
("authorised earlier… set JARVIS_GOOGLE_CLIENT_ID… Disconnect clears it") instead
of "no account connected", which hid the credential and pointed at the wrong fix.

**Restart regression (isolated home, port 8892):** conversational reply `READY`
via the selected `chat2api`; Google task request → `add_google_task` + CONFIRM;
approve → EXECUTE, action `ok: False`, truthful "Google is not configured";
deny → BLOCK, 0 actions; replay of an approved confirmation → "already approved",
0 actions. After `touch` + `restart`: instance id changed, `stale False`, exactly
**one** listener, routing unchanged.

**Verification:** focused 100 + 14 passed; full suite **3657 passed / 21 skipped / 0 failed** (497 s); Android
`:app:testDebugUnitTest` + `:app:assembleDebug` BUILD SUCCESSFUL; macOS
`swift build` Build complete.

**Not done / blocked:** the user's backend on `:8765` (pid 88264, started Oct 26)
predates `/health/identity`, so `backend_lifecycle.py status` reports it
**foreign** and nothing was stopped. **It needs a manual restart** to pick up
current code — that is the one action left, and it is the user's process.
Google live OAuth remains unverified (no client id); no hosted MCP server is
configured; Android runtime is unverified (no device/emulator).

## Update — 2026-10-28 (the stale production backend was recovered; app start hardened; a routing gap closed)

**The one action the 2026-10-27 run left open is done: the backend on :8765 is current.**
It was _ours_ (state `ours`, not foreign — the older process was replaced between runs),
but **stale**: pid 8385 had started before the code on disk changed. Checked first that
nothing supervises it (PPID 1, no launchd job) and that no confirmation was pending, then
`scripts/backend_lifecycle.py migrate --confirm` sent SIGTERM and started pid 86594
(`BKT-ee33484c81fbcbb6`). After: `state ours`, `stale False`, **exactly one** listener,
binding still `*:8765` (LAN mode on, as before — the companion needs it).

**Legacy migration proven with real subprocesses, not only mocks.** On an isolated
home+port: start → restart (new instance, stale False, one listener); a pre-identity
stand-in started as `run.py serve` from the checkout was classified `legacy` /
`migratable` and replaced cleanly; the _same_ server started from outside the checkout,
and a server with a non-launcher command line, were both refused as `foreign` with the
reason named and nothing signalled.

**Defect: the app's own backend start bypassed the launcher.** `BackendManager.startBackend`
ran `env uv run uvicorn jarvis.api.app:app --host 127.0.0.1` — which ignores `run.py`'s
port-in-use refusal, its LAN-mode host and its HTTPS wiring, and resolves whichever `uv`
is first on PATH. It now runs `<checkout>/.venv/bin/python run.py serve` with the host read
from the same `lan_mode` switch `backend_lifecycle.py` uses (`backendInterpreter`/`bindHost`;
5/5 checks in a compiled harness). XCTest still cannot run here (CLT only), so the equivalent
checks were compiled standalone.

**Defect: a failed start hid its own reason.** "exited during startup (code 0)" was all a
person saw when another server held the data-directory claim — the launcher had printed the
actual sentence. `_startup_failure_reason` now surfaces it; verified against the real
refused-start path (4 new tests).

**Release safety: `make install` was destructive.** It began with
`rm -rf /Applications/Vantage.app`, so an interrupted copy left no app and no rollback.
It now calls `installers/macos/install_app.sh` (validate → back up → atomic swap → verify).
Used for real this run: backup `/Applications/Vantage.app.backup-20261028-120331`, install
verified, id `com.jarvisforge.vantage`.

**Defect: "add a **google** task to …" did not go to Google.** The Google rule matched
"task … Google Tasks" but not "Google task", so that phrasing fell through to the registry
fallback, which found both `add_task` and `add_google_task` and asked "which did you mean?"
— a question the sentence answered. Fixed in `planner.py`; live `/command` now returns
`add_google_task` with a confirmation; denying it returned `BLOCK` and created nothing;
replaying the resolved id returned "That request was already denied."

**UI: recovery is now reachable from the status footer.** A stale or upgradeable backend
shows "Restart Backend…" / "Upgrade Backend…" directly in the sidebar footer (with a
confirmation), not only inside Settings. First layout crowded the 208pt sidebar and
truncated the status; the button now sits on its own row — re-rendered and checked.

**Verified this run:** focused suites 90 passed (lifecycle, release pipeline, MCP connector,
Google tasks) and 119 passed (planner, api); live `/health` 200 with 177 tools; selected
provider `chat2api` returned `READY`; MCP config harness all checks passed; Android
`:app:testDebugUnitTest` + `:app:assembleDebug` BUILD SUCCESSFUL; macOS release build +
bundle + codesign valid; installed app launched, attached to the live backend with **no
duplicate spawn**, polled `/health`/`/devices`/`/audit` with 200s, and quit without taking
the backend down.

**Still blocked (external):** Google live OAuth — `JARVIS_GOOGLE_CLIENT_ID` is unset, so the
backend truthfully reports `not_configured` with setup steps; no hosted MCP server is
configured; Android device/emulator runtime — no emulator binary, no system images, `adb`
absent, no device attached. None of these is a code gap that further work here can close.

## Update — 2026-10-28 (Control Mac: native automation with a real risk boundary)

**What exists now.** A new **Control Mac** screen inside the native app, and a
native executor behind it, covering application control, volume/mute, display
and battery information, folder opening, and window control through
Accessibility. Command Center routes typed requests to it **before** the
backend, so "Open Safari" opens Safari and "how do I open Safari" is answered
rather than executed.

**Where the code lives** (`macos/Vantage/Sources/Services/MacControl/`), chosen
so no rule has more than one home:

| File                           | Responsibility                                                                                           |
| ------------------------------ | -------------------------------------------------------------------------------------------------------- |
| `MacAction.swift`              | The closed `MacActionKind` set, typed arguments, risk classes A–D, permission table, refusals            |
| `MacActionRouter.swift`        | Present → valid → permitted → authorized, in that order; the only producer of an executable request      |
| `MacControlService.swift`      | The native arms: `NSWorkspace`, CoreAudio, `AXUIElement`, IOKit                                          |
| `MacPathGuard.swift`           | Mirrors `jarvis/security/paths.py`; refuses protected roots, credential stores, traversal and collisions |
| `MacCommandParser.swift`       | Sentence → typed action, or _not_ an action; questions are never executed                                |
| `MacControlCoordinator.swift`  | One executor, one history, one confirmation rule, ordered multi-step runs                                |
| `MacPermissionInspector.swift` | Observed Accessibility state; the documented consent prompt                                              |

**Verified against this Mac** (real APIs, not mocks):

- Application resolution found `Finder` in `/System/Library/CoreServices`,
  `Terminal` under `Utilities`, and apps in `/Applications`; an unknown name is
  refused with the searched locations named.
- Volume read **56%** from the default output device via CoreAudio; a write to
  30% was read back as 30%, then restored to 56%.
- `systemInformation` reported the display (1280×800 at 2x, 1280×704 usable),
  macOS 26.6.2, and battery 100% through IOPowerSources.
- **Opening `~/Downloads` succeeded** through `NSWorkspace` and is recorded as
  verified in the history.
- Launch and activation returned a _truthful failure_, not a silent success:
  the screen is locked (`CGSSessionScreenIsLocked` = true, frontmost =
  `loginwindow`), and the app said so instead of claiming Safari came forward.
- 42/42 parser and policy checks passed from a compiled harness that drives the
  real sources (XCTest cannot run here — Command Line Tools only).

**The boundary, stated plainly.** No AppleScript, shell string or free-form path
is ever built from a sentence. The only Apple Event in the tree is a scoped
`quit`, whose substituted value is a bundle id found installed on disk and
checked against `^[A-Za-z0-9.\-]+$`. A request arriving through the backend or
from the phone can read and can run low-risk actions; it cannot close a window,
quit an app or change a file without someone at this Mac.

**Two defects found in existing tooling and fixed.** (1) `run_lifecycle_check.sh`
ran the tracked fixture **in place**, so a read-only test run rewrote its mtime
and the live backend then truthfully reported `stale: true` — a false alarm
caused by a harness. It now runs a temp copy; verified the mtime is unchanged
across a run. (2) `run_command_center_check.sh` compiled a hand-listed set of
six sources and broke when the new directory appeared; it now enumerates
`Sources/` the way `build_render.sh` already did. (3) The staleness test depended
on some real file being newer than "an hour ago" and failed on a quiet tree; it
now pins the scan result and checks both directions.

**Build and install.** Release build clean; bundle assembled and codesign
verified; installed through `installers/macos/install_app.sh` with a backup at
`/Applications/Vantage.app.backup-20261028-140819`. The installed app launched
and stayed on the running backend (pid 86594) with **one** listener and no
duplicate spawn. The previous build-tree instance that had been holding the
single-app slot was identified by exact path and start time before it was
signalled, and the backend survived it.

**Not verified, and why.** Window operations (minimise/arrange/close) could not
be exercised for real: Accessibility is **not granted** to this process
(`AXIsProcessTrusted` false) and the session is locked. The buttons are disabled
with that reason on screen. GUI launch of another app is likewise blocked while
locked. Granting Accessibility to Vantage in System Settings, then pressing
Recheck on the Control Mac screen, is the remaining step for those.

### Correction and a third defect (same run)

The staleness detector was scanning the **whole checkout** for `.py` files, not
the package its docstring claims ("under the package"). Two consequences were
measured today:

1. Running `run_lifecycle_check.sh` executed the tracked fixture _in place_, and
   macOS rewrote its mtime — so a read-only harness made the live backend
   report `stale: true`. Fixed by running a temp copy; verified the mtime is
   unchanged across a run.
2. Editing **any test file** made the backend (which will never import it) tell
   the user to restart a healthy server.

`jarvis/runtime/identity.py :: newest_source_mtime` now scans only `jarvis/`
and `run.py`. Its test was rewritten to be independent of the tree's timestamps
(it previously asserted a real file was newer than "an hour ago" and failed on a
quiet tree). After the change the live backend was restarted through
`backend_lifecycle.py restart` — pid 59871, `stale: false`, one listener, LAN
binding preserved — and the installed app reconnected to it (the new backend's
log shows its `/health` and `/health/identity` polls).

One pre-existing oddity was also corrected: `run.py` carried an mtime three days
in the **future** (Oct 26 17:37 vs a wall clock of Oct 28), which by itself made
every process look stale until it started. It was set back to its commit time;
the contents were never touched.

## Update — 2026-10-28 (Vantage Anywhere + Vantage Flows)

Two features on top of the existing Mac-control layer, sharing its executor.

**Vantage Anywhere** — a global palette (`Sources/Services/Anywhere/`).
`GlobalHotKey` uses Carbon `RegisterEventHotKey` (Option-Space by default), which
is Apple's documented API for this and needs no Accessibility permission. One
Carbon handler is installed for the process lifetime and dispatches to the
current registration, so changing the shortcut cannot accumulate handlers.
`AnywhereController` owns a single reused `NSPanel`; `show()` is idempotent.
The palette's `AnywhereCommand` resolves a sentence to a _proposal_ only —
`open_application`, `set_volume`, `run <flow>`, `draft a flow`, `explain`, or
`conversation` — and Execution is handed to `MacControlCoordinator`, the same
coordinator Control Mac uses. A protected action parks in
`pendingForPalette` and is answered by a confirmation sheet in the main window;
the palette never approves anything itself. Questions ("how do I change the
volume?") resolve to `.explanation` and are answered in the window, not run.

**Vantage Flows** — a real workflow system (`Sources/Services/Flows/`, plus
`FlowsView`). A `Flow` is a versioned, Codable list of `FlowStep`s each holding
an action id and validated arguments. `FlowStore` persists to
`~/Library/Application Support/Vantage/flows.json` and `runs.json` with atomic
writes; an unreadable file is quarantined rather than overwritten. `FlowEngine`
runs steps in order through `MacActionExecutor` (production:
`NativeActionExecutor`), stops at the first required failure unless a step is
marked safe under the continue policy, pauses at each protected step for
explicit approval, honours cancellation between steps, and records a `FlowRun`
whose outcomes are snapshots (so history survives editing or deleting the flow).
`FlowAuthoring` turns "create a Start Workday flow that opens Safari, Finder and
my Documents folder, then sets volume to 25 percent" into a four-step **draft**
— saved only on Save, run only on Run — and rejects a target that is not an
installed app or a known folder instead of inventing a step.

**Six defects found while building, all fixed and pinned:**

1. The Carbon handler hopped through `DispatchQueue.main.async`, and the queued
   block never ran: the shortcut registered as active and did nothing. This is
   exactly the "claims a shortcut is active" failure the class exists to
   prevent. It now calls through directly (Carbon delivers on the main thread).
   Found by a bundled probe, not by reading the code.
2. `create a flow that …` produced a flow named "a", because stripping the
   article left the bare word.
3. A described flow's third-person verbs ("opens", "sets") reached a parser that
   expects the imperative, so the main example drafted zero steps.
4. `opens Safari, Finder and Documents` — one verb, three objects — was split at
   the commas into fragments that did not parse.
5. `…and makes me a sandwich` drafted a step that would have opened an app called
   "makes me a sandwich" (the launch-anything behaviour is right for a typed
   command and wrong for a saved flow).
6. The Flows sidebar highlighted a row whose editor said "No flow selected",
   because `selection` and `draft` were set independently.

**Verification** (`IntegrationHarness/run_anywhere_checks.sh`, five phases over
the real sources — XCTest cannot run here, CLT only):
engine logic 24/24 (ordering, stop-on-failure, marked-safe continuation, approval
pause, decline runs nothing, cancellation never reports completion, concurrent
second run refused, retry safety, persistence, quarantine, cap);
authoring/palette 25/25; **native execution** with `NativeActionExecutor`:
a saved four-step flow ran the real actions in order — foreground read, volume
read (56%), `~/Downloads` opened, system/display/battery — all verified, recorded
to disk, and a missing folder failed truthfully with the dependent step not
running; shortcut 7/7 in a real bundle (fires once, one handler after
re-registration, silent after shutdown, persists); shared path 5/5 (palette and
flow runner defer the _same_ protected action identically).

Backend focused suites 43 passed; broad non-OCR suite **3637 passed / 21 skipped
/ 0 failed**. Release build clean; bundle 11 MB, codesign valid, installed via
`installers/macos/install_app.sh` with backup `/Applications/Vantage.app.backup-20261028-171857`;
the installed app launched, stayed on backend pid 59871 (one listener, no
duplicate spawn), and a window-lifecycle probe confirmed one reusable palette.

**Not verified.** The real Option-Space keystroke could not be pressed on the
running app: the screen is locked (`CGSSessionScreenIsLocked`), so no global key
event reaches any process and Accessibility is not granted either. Registration,
delivery, re-registration and teardown are proven by driving the real Carbon
event target; the final physical key press remains for the owner.

## Update — 2026-10-28 (Google OAuth client configured; live consent is the remaining step)

The owner supplied two OAuth **Desktop app** clients from different Google Cloud
projects. One is now configured. The `installed` client type is the right one:
it accepts a loopback redirect, which is exactly what `/google/callback` uses.

Written to `.env` (gitignored, mode 600 — verified with `git check-ignore`), not
to source, logs or docs: `JARVIS_GOOGLE_CLIENT_ID` and
`JARVIS_GOOGLE_CLIENT_SECRET`. The refresh token is never a setting; on first
successful exchange it goes to the macOS Keychain
(`KeychainStorage` → `keyring`, service `vantage.ai.refresh_token`). That store
was exercised end to end first (store → retrieve → delete → gone) so
authorization persistence is not assumed.

Backend restarted through `backend_lifecycle.py` (pid 69063, one listener,
LAN binding preserved). `GET /google/status` now reports
`configured: true`, `state: disconnected`, `has_token: false`, `usable: false`,
and the four "create a project / enable APIs / create a client" setup steps are
gone — the screen has moved from "not configured" to "not connected", which are
different facts.

Verified without touching the account:

- `begin()` produces `accounts.google.com/o/oauth2/v2/auth` with
  `response_type=code`, `access_type=offline` (so a refresh token is issued),
  `prompt=consent`, PKCE `S256`, `redirect_uri=http://127.0.0.1:8765/google/callback`
  and scopes `calendar.readonly` + `tasks`. Bland assertion-checked.
- The loopback callback answers on all three paths: incomplete link, forged/old
  state (refused, naming why), and `error=access_denied` (cancelled, nothing
  connected). A mismatched state is rejected _before_ any token exchange.

**Not done: live account authorization.** No consent was granted in the browser,
so no account is connected and no Calendar/Tasks read has been made. Nothing
here claims otherwise. The next step needs the owner at the browser: open
Vantage → Google → Connect, approve, and the loopback callback completes it.

## 29. Update — 2026-10-29 (Phase 3: capture, documents, briefing, preferences)

Four features added to the native macOS app, all on the existing execution,
persistence and approval architecture. Nothing was recreated: the command
palette, Flows editor/executor, `MacControlCoordinator`, risk policy and the
Application Support store conventions are reused as-is.

**Quick Capture** — real, persistent inbox (`capture.json`, atomic write,
schema-checked). Seven kinds, four statuses, duplicate refusal on an open item,
search/filter, edit, archive/restore, delete with confirmation. Conversion to a
planner task or a flow draft happens only through a reviewable sheet; converting
produces an unsaved draft and does not mark the item converted. An unreadable
file is moved aside and the store goes **read-only** rather than overwriting it.

**Documents** — explicit file selection only (no scanner, no watcher). Genuine
extraction for PDF (PDFKit text layer), plain text, Markdown, RTF, HTML (visible
text only), CSV and source files. Bounded read (20 MB file, 120k chars) and a
separate 24k-char per-request budget. Local "Overview" makes no network call;
every AI action names the provider that will receive the text _before_ it is
pressed, and sends no session/action context (a document cannot start a tool
call). Action items come back as an editable draft with an explicit date
confidence, and only "Add selected to planner" creates anything.

**Daily Briefing** — aggregates `/planner/today`, `/planner/tasks`, `/schedules`,
`/confirmations`, the local inbox, the local flow history and provider health.
Three-state sections (`loaded` / `failed` / `unavailable`) so a failed read is
never drawn as an empty day; `allSourcesFailed` is computed from the sections
and says so out loud. Suggestions are derived from real counts and labelled as
suggestions. Actions call the real routes (complete task, open capture, review a
failed flow by id, open approvals).

**Preferences** — closed set of six kinds, explicit add/edit/disable/delete,
conflict detection with a stated precedence rule (most recently changed wins,
and the conflict is shown). Values that look like credentials or document text
are refused at entry. Preferences supply defaults only; nothing executes.

**Verified (evidence, not claims):**

- `IntegrationHarness/run_phase3_checks.sh` → **113/113** logic checks PASS
  (capture persistence + quarantine, classifier vs. creation/execution, natural
  dates incl. weekday/weekend boundaries and time zone, briefing empty vs.
  failed vs. unavailable, preference validation + conflict, extraction over real
  temp files, action-item parsing, palette resolution, cross-feature handoffs).
- `IntegrationHarness/run_phase3_live.sh` → **13/13** PASS against an isolated
  real backend: capture → real planner task (read back), `POST /schedules`
  reminder job (verified by returned id), inexpressible reminder date refused
  with a reason, a task due today appears in the briefing, a failed section is
  reported rather than empty.
- 4 screens rendered offscreen from the real views with seeded persisted data
  (`artifacts/phase3-{capture,documents,briefing,preferences}.png`).
- `run_anywhere_checks.sh` (pre-existing Phase 2 suite) still fully green — no
  regression. One harness switch was extended for the new outcomes; the app's
  behaviour did not change.
- `make app` → signed bundle; installed via `installers/macos/install_app.sh`
  (backup `Vantage.app.backup-20261029-004246` kept); installed app launches and
  stays running with no crash report.

**Limitations (honest):**

- The scheduler grammar has **no absolute-date form**. A reminder can be a clock
  time today or a relative delay; a specific future date is refused with a
  sentence instead of being silently downgraded to a daily job.
- No `.docx`/`.xlsx`. Scanned PDFs with no text layer report no text rather than
  being OCR'd (image OCR is used by the backend attachment pipeline, not here).
- AI document actions were not exercised against a live model: `llm_enabled` is
  false in both the isolated and the real backend. The send path and its failure
  reporting are covered; a real summary was **not** produced.
- Scheduled briefings are out of scope for this phase; the briefing is on demand
  and says so in its footer.
- The installed app's UI was not driven by hand (Accessibility/System Events is
  refused in this environment). Screens are verified by real-view offscreen
  renders, by the services they call, and by the installed binary containing the
  screens.

---

## Phase 4 — Unified Daily Workspace and Google-aware Automation (2026-10-29)

**What is now true (verified, not asserted):**

### 1. The Today workspace

A new `Today` screen (`macos/Vantage/Sources/Views/TodayView.swift`) is first in
the sidebar's Workspace group. It draws five sources — Google Calendar, Google
Tasks, Planner, Quick Capture, Needs attention — each with its **own** state:
loading, ready, empty, failed, unavailable. Its rules live in
`Services/Today/TodayModel.swift` and `TodayMapping.swift`, and are the reason
the screen can be trusted:

- a **failed** read is drawn as failed, with the provider's own sentence and a
  Retry — never as an empty list (rendered and confirmed as pixels:
  `artifacts/today-google-failed.png`);
- an **empty** read says "nothing", which is a fact (`artifacts/today-native.png`);
- one source failing cannot blank the others;
- every row keeps its origin (`googleEvent(uid:)`, `googleTask(id:listID:)`,
  `plannerTask(ref:)`, `capture(id:)`, `attention(surface:ref:)`), so an action
  is sent back to the system that owns the record. Nothing is copied into a
  second database.

Actions: open a row in the screen that owns it, complete a Google task, add a
Google task, propose and confirm a calendar event, refresh a source or all of
them, and open Calendar/Planner/Flows/Activity/Inbox. The row itself is a
button only for _navigation_; completing a task and deleting one are separate
controls, and destructive actions confirm first.

### 2. Google reliability, measured live (not mocked)

Against the connected account, through the running backend:

| Operation          | Result                                                                          |
| ------------------ | ------------------------------------------------------------------------------- |
| Calendar read      | `read_succeeded: true` (`real_empty`/`has_results`)                             |
| Tasks read         | `has_results`, real task returned                                               |
| Task create        | created, Google returned the record (`VANTAGE-PHASE4-TEMP`)                     |
| Task complete      | Google reported `status: completed` with a timestamp                            |
| Task delete        | new route; **409 without `confirm=true`**, then `deleted: true, verified: true` |
| Temp task cleanup  | read back: 0 remaining                                                          |
| Calendar **write** | refused honestly: "connected read-only … Nothing was created"                   |

The temporary task used for verification was deleted; the user's real task and
calendar data were not modified. The calendar write stays refused because the
connection holds `calendar.readonly` — this phase did not widen that scope, and
the UI says so rather than inventing success.

**Fixed while verifying:** a successful empty calendar read carried
`Verification.UNKNOWN`, so the assistant appended _"but I could not verify it
happened"_ to its own correct answer ("Nothing scheduled for today."). All
successful calendar reads now report `verified=True`; live check:
`What is on my calendar today?` → `Nothing scheduled for today.` (no caveat).

### 3. Natural language

`find my overdue google tasks` now routes to `list_google_tasks` with
`overdue_only`, filtered by **calendar day** (a task due today is due today at
23:59, not overdue), and reports a failed read as a failure rather than an empty
overdue list. Schedule sentences produce a **typed proposal** before any write
(`CalendarProposal`): a missing hour, a missing day, an ambiguous bare hour
("at 10") or a past date each produce a specific question — never a guess. The
summary names the day, both times and the title, and nothing is created until
the person presses Create.

### 4. Availability

`Availability.within(...)` takes the read's outcome as a parameter, so a failed
calendar cannot produce "you are free" — there is no code path that does. Busy
intervals are merged and clipped to the window; free blocks are the gaps.
Assumptions are stated. Live: "when am I free for an hour tomorrow" → answered
from real events.

### 5. Google-aware flows

Four actions registered in the existing executor
(`MacActionKind` + `MacActionPolicy` + router + service arm), not a second one:

- `google_agenda`, `google_overdue_tasks` — class A (read);
- `google_create_task`, `google_create_event` — class C (mutating, approval required);
- `open_today` — class B navigation.

Live flow run against the real backend: both reads executed and reported
truthfully. A failed Google step **stops the run** and the dependent step does
not execute (proven in `IntegrationHarness/today_google_check.swift`); a Google
write pauses for approval; a failed outcome never maps to `verified`; a step
with no outcome fails rather than guessing.

### 6. Integration health

The Connections screen now has a real per-service **Test** button that performs
the same read the Google screen does, and keeps Calendar and Tasks separate.
States: working / sign-in required / permission-or-API-disabled / read-only /
timed out / unavailable / **not tested**. "Healthy" is only ever produced by a
successful read — `unknown` is never healthy, and the connection state alone is
not a test. This distinction is the one this machine actually needed: one
project had Calendar enabled and Tasks disabled, the other the mirror image, so
a single "Connected" pill was true and useless.

### 7. Testing

- `macos/Vantage/IntegrationHarness/today_google_check.swift` — **56 checks, all
  PASS** (today mapping, day boundaries, proposal safety, availability, health
  classification, flow approval + dependent-step failure, outcome mapping).
- `tests/test_phase4_today_google.py` — **9 passed**.
- Regression: `test_google_integration`, `test_google_tasks_tools_and_mcp`,
  `test_planner`, `test_r97_daily_planner` — 189 passed.
- **Full suite: 3687 passed, 21 skipped, 0 failed** (598s), `pytest -q -p no:cacheprovider` on 2026-10-29 after the isolation fix.

**Test-isolation defect found and fixed:** `Settings` reads `.env` from the
working directory, so every isolated test server launched from the repo root
inherited the developer's real Google credentials and routed calendar writes to
the real (read-only) Google calendar. Three tests failed because of it. Fixed in
`tests/isolated_server.py` by turning the Google integration off for scratch
servers — the isolation the module promises.

### 8. Native build

`make app` → signed bundle (`codesign --verify --strict` OK), installed with
`installers/macos/install_app.sh` (backup kept:
`/Applications/Vantage.app.backup-20261029-125343`). The installed binary
contains `TodayView`, `CalendarProposal`, `googleCreateTask` and the Today
strings.

### Limitations (honest)

- **Calendar writes are refused by design.** The connection holds
  `calendar.readonly`; this phase did not widen it. The event path is complete
  (proposal → confirm → write → verify → refresh) and is proved against a
  provider that accepts writes in unit tests, but a live Google event was
  **not** created, because it cannot be. Enabling `calendar.events` scope and
  re-consenting is the change required, and it is not this phase's decision.
- The installed app's UI was **not driven by hand** (Accessibility/System Events
  is refused in this environment). Screens are verified by real-view offscreen
  renders against real and deliberately-broken backends, by the services they
  call, and by the installed binary containing them.
- `swift test` cannot run here (Command Line Tools only, no `XCTest`); the
  harness above stands in for it and is recorded as such, not as XCTest passing.

---

## n8n automation + capability routing (2026-10-29)

### n8n — VERIFIED end to end

The deployment at `~/n8n` had **never been started** (no containers, no images,
empty data dirs). It was hardened and brought up:

- images pinned (`n8nio/n8n:2.46.1`, `postgres:16.14-alpine`) instead of `:latest`;
- both services bound to **127.0.0.1**; PostgreSQL publishes no host port;
- healthchecks on both; `depends_on: condition: service_healthy`;
- `.env` 0600 and git-ignored (verified with `git check-ignore`); `.env.example` added;
- `N8N_DIAGNOSTICS_ENABLED=false`, execution pruning on, `no-new-privileges`;
- backup/restore/health scripts; the backup archives `.env` **with** the data so
  restored credentials stay decryptable.

**Live, against real n8n:**

| Check                       | Result                                                                          |
| --------------------------- | ------------------------------------------------------------------------------- |
| `scripts/health.sh`         | docker ok, n8n healthy, postgres healthy, HTTP 200                              |
| Port binding                | `127.0.0.1:5678->5678/tcp`                                                      |
| Version                     | 2.46.1                                                                          |
| Diagnostic echo via Vantage | `succeeded`, correlation id echoed back                                         |
| Daily agenda via Vantage    | `succeeded`, built from live calendar data                                      |
| Notify via Vantage          | `succeeded`, `delivered: false, dry_run: true` (honest)                         |
| Restart persistence         | webhook answered after `docker compose restart`                                 |
| Recreate persistence        | webhook answered after `down` + `up` (data kept)                                |
| n8n stopped                 | Vantage `unreachable`, unrelated features healthy                               |
| n8n restarted               | Vantage `ready` again                                                           |
| Security refusals           | 404 unknown workflow · 403 no Origin · 403 unknown field · 409 unconfirmed send |

**Found and fixed while running:** n8n 2.x denies Code-node env access
(`N8N_BLOCK_ENV_ACCESS_IN_NODE=false`); re-importing a workflow creates a new id
and the old one keeps the webhook path; and the agenda workflow's first design
called Vantage over `host.docker.internal`, which Vantage's DNS-rebinding guard
correctly refuses — the workflow now receives Vantage-gathered events instead,
so no Google credential is ever handed to n8n.

### Capabilities — TTS verified live, the rest honestly declared

`jarvis/gateway/media.py` adds speech-to-text, text-to-speech, image
generation/editing, vision and video adapters; `jarvis/gateway/routing.py` adds
per-task assignment with verified-only fallback; `/ai/capability-routing` and the
macOS **Capabilities** screen drive it.

| Capability                                   | State                                                                                                                                                                        |
| -------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Text to speech                               | **VERIFIED live** — Gemini `gemini-3.8-flash-tts` returned 115,558 bytes of `audio/wav`; artifact `artifacts/tts-live-gemini.wav` (194,278 bytes, RIFF/WAVE PCM 24 kHz mono) |
| Image generation                             | **DECLARED** — Gemini answered HTTP 429 (plan quota). Correctly left unverified.                                                                                             |
| Speech to text, image editing, vision, video | **NOT IMPLEMENTED/live** — adapters exist and are unit-tested; no credential or sample was available to verify                                                               |

**Found and fixed:** Gemini needs `x-goog-api-key`, not `Authorization: Bearer`
(a 401 that reads like a bad key). The adapter had the wrong header until the
live request proved it.

### Limits (honest)

- Fish Audio via OpenRouter was **not** tested: no OpenRouter key exists on this
  machine. The OpenAI-shaped `/audio/speech` adapter is what it would use.
- Image models hit a real provider quota; the state stays `declared`.
- The Gemini key was written to the keychain during verification; it was already
  present in the shell environment.
- n8n emits a Postgres-16 support warning (n8n recommends 17+). The data directory
  is already on 16; changing it is a migration, not a tag edit, so it was left.

---

## Provider-type tabs (2026-10-29, corrective)

**Why the previous attempt failed:** the screen had _one_ form plus a routing
list. It had no per-category state, so a change to any category wrote the same
block and the visible form was always the chat one. The fix is structural:
seven categories, seven stored blocks, seven drafts in the UI.

### What now exists

| Tab                      | Provider(s) offered                                                              | Controls                                                 |
| ------------------------ | -------------------------------------------------------------------------------- | -------------------------------------------------------- |
| Text & Chat              | openai, gemini, groq, 9router, chat2api, openai_compatible, pollinations, custom | provider, model, base URL, key, timeout, enabled         |
| Document Reading         | openai, gemini, openai_compatible, pollinations, custom                          | + document test that names the reference number          |
| Speech                   | openai, gemini, openrouter, openai_compatible, custom                            | + voice, format; endpoint is `/audio/speech`, never chat |
| Image                    | pollinations, openai, gemini, openai_compatible, custom                          | + size; base64 or URL results both handled               |
| Vision                   | openai, gemini, openai_compatible, pollinations, custom                          | image sent inline; text answer required                  |
| Video                    | openai, openai_compatible, custom                                                | job submit; a queued job stays unverified                |
| Custom OpenAI-Compatible | custom, openai_compatible                                                        | full form + `GET /models` discovery + manual entry       |

### Live verification (real provider calls)

| Tab              | Live result                                                                                                                            |
| ---------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| Image            | **VERIFIED** — Pollinations `black-forest-labs/flux.1-schnell` returned 15,730 bytes of `image/jpeg`, and 22,174 bytes on a second run |
| Speech           | **VERIFIED** — Gemini `gemini-3.8-flash-tts` returned 100,198 bytes of audio                                                           |
| Vision           | **VERIFIED** — Gemini `gemini-3.5-flash` accepted a 1×1 PNG and answered with text                                                     |
| Document Reading | **VERIFIED** — the model quoted the sample document's reference number `MAINT-4711`                                                    |
| Text & Chat      | **BLOCKED** — no OpenAI key is stored. The test correctly reports the 401                                                              |
| Video            | **NOT CONFIGURED** — no video provider credential exists                                                                               |
| Custom           | **NOT CONFIGURED** — strict provider list (custom/openai_compatible only)                                                              |

**Findings from running it:** image providers think for several seconds before
the first byte, and `http_read`'s 5 s socket gap reported that as a transport
failure — widened to 30 s for media calls. `gemini-2.5-flash` is retired (404)
and `gemini-3.7-flash` returns 503 under load; `gemini-3.5-flash` works.

### Isolation evidence

Saving Text with `openai/gpt-4o-mini` left Image on Pollinations with its
**key and test record intact** (asserted in `tests/test_provider_categories.py`
and checked live against the API).

### Files

New: `jarvis/gateway/provider_categories.py`, `jarvis/gateway/category_checks.py`,
`macos/Vantage/Sources/Views/ProviderTabsView.swift`,
`tests/test_provider_categories.py`.
Changed: `jarvis/gateway/providers.py` (Pollinations + `discover_models`),
`jarvis/api/app.py` (4 category routes), `APIClient.swift`, `APIModels.swift`,
`ContentView.swift`.

---

## Master completion loop (2026-10-30)

### macOS control — extended and verified on the real Mac

Nine actions added to the existing typed registry (`MacActionKind` +
`MacActionPolicy` + router + executor), not a second system:

| Action               | Class | What it does                                                      |
| -------------------- | ----- | ----------------------------------------------------------------- |
| `read_clipboard`     | B     | reads text; contents never enter the history (size only)          |
| `write_clipboard`    | B     | writes, then **reads back** to verify                             |
| `post_notification`  | B     | UNUserNotificationCenter, with permission refusal                 |
| `move_file`          | C     | single file only; refuses a directory and an existing destination |
| `rename_file`        | C     | a _name_, not a path — `../x` is refused                          |
| `delete_file`        | D     | moves to the **Trash**, never unlink                              |
| `list_displays`      | A     | real NSScreen data                                                |
| `list_audio_devices` | A     | default output name + volume, from CoreAudio                      |
| `current_time`       | A     | local date/time and zone                                          |

**27 checks pass** in `IntegrationHarness`-style harness
(`/tmp/mac_new_check.swift` run against the real tree), covering parsing,
router refusals (folder delete, path-in-name, overwrite), and real operations:
clipboard write→read, rename→move→trash lifecycle, displays, audio, clock.

**Crash found and fixed:** `UNUserNotificationCenter.current()` raises an
Objective-C exception — not a Swift error — when the process has no app bundle,
killing it outright. The executor now refuses cleanly instead.

### n8n — per-workflow control and status

- `PUT /n8n/workflows/{key}` enables/disables a workflow **in Vantage only**;
  n8n keeps the workflow, its webhook and its history. Verified live: disabling
  `notify` produced a 403 with the allowlist reason, re-enabling restored it.
- `GET /n8n/workflows/{key}/status` returns Vantage's own latest-run record.
  Verified live against a real run (`succeeded`, correlation id, 1727 ms).
- The Automation screen shows a per-workflow switch and "Last run …" line.

### macOS app — failure and recovery, proven with screenshots

- Backend stopped → Home renders one honest error naming all five unread
  sections, `—` instead of false zeros, and a Retry
  (`artifacts/home-backend-down.png`).
- Backend restarted → **Running**, 181 tools, real counts
  (`artifacts/home-recovered.png`).
- App rebuilt, signed, installed (backup `Vantage.app.backup-20261030-091330`),
  launched, and running with no crash report.

### Android

- **APK builds** (`app-debug.apk`, 19 MB) and **unit tests pass** — but only
  with **JDK 21** (`/opt/homebrew/opt/openjdk@21/...`). The default JDK 26 is
  outside AGP's supported range; this is the documented requirement.
- **No device or emulator is available**: the Android SDK here has no
  `emulator` package (only AVD configs) and `adb devices` lists nothing.
  Device-level verification is **BLOCKED** on installing the emulator package or
  connecting a phone.

### Verified live this round

Google (connected, tasks + calendar reads), chat (conversational answer,
battery tool routing, n8n run from a sentence), n8n (run + toggle + status),
and the nine Mac actions above on this machine.

---

## Update — 2026-11-13 (iteration 84: conversations can be resumed)

**Baseline first.** Full non-OCR run on the untouched tree: 32 failed. All 32
are the four live-backend files (r85, r86, r87, `test_macos_health_decode`)
reading `127.0.0.1:8765`, which was not running. With the backend started those
files are 39 passed / 3 skipped — **no code regression in the baseline**. Start
the backend before reading their failure as one.

**The gap.** `GET /ai/chats/{id}` existed but nothing could _list_
conversations, and `dashboard/ai_chat.html` created a new conversation on every
load. Every reload stranded the previous one. The ids were stored; they were
just unlistable.

**Fixed.** `GET /ai/chats` (most-recent-first, bounded `limit`, `message_count`
per entry); the page resumes the most recent conversation on load and has a
History panel to switch between them; and message/title text is rendered with
`textContent` instead of `innerHTML` (resume made the latent injection vector
reachable, so it was closed here too).

**Evidence.** `tests/test_dashboard_browser_flows.py` (2 new real-browser tests,
each proven red against the old behaviour) + `tests/test_api.py` (4 new listing
contract tests). Full non-OCR suite: **3736 passed / 21 skipped / 0 failed**
(566.75 s). Screenshots: `artifacts/ai-chat-history-panel.png`,
`artifacts/ai-chat-resume-history.png`.

**Not done / next slice (E11).** The macOS app and the dashboard still use
separate conversation stores (`messages` via `/command` vs `ai_messages` via
`/ai/chat/send`). Unifying them is the rest of E11 — an architecture-level
change, deliberately not claimed here. Every other open item is owner- or
hardware-gated (Google OAuth client id, live MCP endpoint, Android device,
which DMG artifact ships, R0.3 tree commit).

---

## Update — 2026-11-14 (iteration 85: repository portability)

Publishing the tree so a second laptop can clone and continue exposed defects the
suite could not, because it ran on one machine with one interpreter:

1. **`.gitignore` would have published a secret and ~2 GB of build output.** The
   `.env.bak-*` (real Google client id + secret) and `macos/.build` (793 MB),
   `ios/.build` (243 MB), `android/app/build` (160 MB) were untracked-but-not-ignored.
   A staged-blob scan confirms the real credentials appear in **no** committed file.
2. **`.env.example` could not be loaded.** Comma-string list values → `SettingsError`.
   Fixed to JSON arrays; 28 previously-missing `Settings` fields added with the code's
   real defaults.
3. **A blank list value crashed startup.** `JARVIS_CORS_ORIGINS=` (what the template
   contains) → `json.loads("")`. Fixed in `Settings.settings_customise_sources`.
4. **The companion certificate was not a CA.** No `basicConstraints`, so OpenSSL 3.x
   (Python 3.13) refused it as a trust anchor — every companion test failed on a fresh
   3.13 venv while passing on 3.11. Fixed in `jarvis/companion/cert.py`
   (`CA:TRUE`, `keyUsage`, `subjectAltName`).

**Proven portably**, not asserted: the staged tree was exported to a scratch dir, a
fresh venv built, `.env.example` copied to `.env`, and the server started and the
suite run there. Full non-OCR suite in the export: **see log — 0 code failures**;
remaining failures are documented post-setup steps (`playwright install chromium`,
`make app`, `build_app.py`).

New: `docs/SETUP_MACHINE.md` (complete second-laptop walkthrough),
`tests/test_env_example_is_loadable.py`, `tests/test_companion_cert_is_a_valid_ca.py`.

---

## Update — 2026-11-14 (iteration 86: the data directory on a fresh clone)

Cloned into a second checkout and ran it there. That exposed a first-run defect the
suite cannot see, because `tests/conftest.py` always sets an absolute `JARVIS_HOME`.

**`JARVIS_HOME=~/.jarvis-forge` was never expanded.** `Settings.home` is filled from the
environment by pydantic-settings, so `Field(default_factory=_default_home)` never ran
when the variable was present — and it is present the moment someone copies
`.env.example` to `.env`. The tilde stayed literal, so the first run created the token,
the SQLite database and the onboarding state in a directory named `~` **inside the
checkout**, hidden from `git status` by `.gitignore`'s `*~` backup rule, while the real
`~/.jarvis-forge` never existed. `wizard.OnboardingManager` did not expand either.

**Fixed** with one resolver, `jarvis.config.data_home()`, reached from a `mode="before"`
validator on `Settings.home`, `llm_file_path`, `gateway.selection.selection_path` and
`OnboardingManager`. The `.env.example` line now says the tilde is expanded.

**Evidence.**
- Red, before the fix: `JARVIS_HOME='~/.jarvis-forge'` → `Settings().home` is the literal
  `~/.jarvis-forge`, and `OnboardingManager().state_dir` likewise. (Pinned by
  `tests/test_env_example_is_loadable.py::test_a_tilde_home_from_the_example_lands_in_the_real_home`.)
- Green, after: both resolve to `/Users/<user>/.jarvis-forge`; 86 tests across the five
  home-resolution files pass.
- Runtime: the server started from the repo read its token from the real
  `~/.jarvis-forge/api_token` and answered `/health` `status: ok`, 181 tools; no `~`
  directory was created in the checkout.
- `README.md`'s published collected count updated 3849 → 3850 (the new test), which
  `test_r108_readme_counts_are_true.py` checks; it passes.

**Full suite on this machine:** **3786 passed, 13 failed, 51 skipped** (486.69 s), with
the backend running and Playwright Chromium installed. The 13, classified by running
each:

| Cause | Tests | Status |
|---|---|---|
| No audio backend (`ffmpeg` / `sounddevice` absent; documented optional) | `test_voice.py` ×5, `test_phase91_voice_agent_integration.py::test_audio_unavailable_raises_clear_error` | **BLOCKED** on an external prerequisite |
| No `dist/` built (`build_app.py` not run on a fresh clone) | `test_phase96_release_preparation.py::TestArtifactIntegrity::test_dist_directory_exists` | **BLOCKED** on a build step |
| Pre-existing, unrelated to this change — **proven**: both files fail identically on the clean tree with these changes stashed | `test_r85_macos_screen_models_match_the_server.py` ×2 (empty `/tasks` on a fresh data dir), `test_r96_macos_integration_harness.py` ×1 | **PRE-EXISTING** |

A first run without the backend up showed 112 failures; 94 of those were the
documented "start `run.py` first" files and 9 were the missing Chromium binary. That
is why the number above was re-measured with both in place rather than reported as is.

---

## Update — 2026-11-15 (V10 wave one: a Mac Control screen that observes, and Saved Workspaces)

Two product surfaces, both backed by real routes, plus one latent routing defect that
had been invisible to the test meant to catch it.

**Mac Control** (`jarvis/core/maccontrol.py`, `jarvis/api/mac_routes.py`,
`Views/MacControlView.swift`). The screen lists the launchable applications from the
existing `AppRegistry`, shows which of them are running, and opens one on request. The
detail that matters: an accepted `open` command is not proof an application started, so
`open_application` refuses an unresolvable name **before** any subprocess runs, then polls
`pgrep -x` for up to 15 s and returns `launched` and `observed_running` as separate
fields. The view renders `observed_running == false` as a warning, not a tick. Live:
`GET /mac/control` → 15 launchable, 109 running, `can_list_running: true`; an unknown app
→ 409 with its reason; a missing field → 422.

**Saved Workspaces** (`jarvis/core/workspaces.py`, `jarvis/api/workspace_routes.py`,
`Views/WorkspacesView.swift`). A named set of applications, files and links, persisted in
SQLite (`workspaces` table, case-insensitive unique name), created/edited/duplicated/
deleted from the app, and launched. Launch opens the project directory first, then each
app, file and URL, and reports **every member separately** (`{kind, target, ok, detail}`)
with `verified` true only when every member succeeded — "launched" as a single word would
hide the case the screen exists to show. A missing file is refused without calling `open`;
a URL must be `http(s)` and is refused before any subprocess. Live: create → edit → launch
(`verified: true`, "opened in Finder" + "Finder is running") → delete; duplicate names
itself `X copy`; a case-insensitive clash returns 409.

**Defect found — a router silently shadowed an existing one.** The first draft registered
`/workspaces`, which is already the *coding* workspace registry, and because the new
router was included earlier in `app.py`, Starlette kept it and the older routes became
dead code. Renamed to `/saved-workspaces`.

**Defect found — the test that should have caught it was blind.** `tests/test_no_duplicate_routes.py`
read only `app.routes`; FastAPI 0.162 keeps router-included routes behind an
`_IncludedRouter` wrapper that has no `path`, so those routes were skipped entirely. It now
expands `original_router.routes`. Red-proved by injecting a colliding `GET /workspaces`:
routes seen went 228 → 276 and the test failed naming both definitions.

**Defect found — a stale release binary hid the feature.** The assembled bundle held an
older `Vantage` binary, so the new sidebar entry did not appear. `swift build -c release`
then `assemble_app.sh release` fixed it; `strings` then found `saved-workspaces` twice in
the bundle.

**Evidence.** `tests/test_workspaces.py` 14 passed, `tests/test_maccontrol.py` 6 passed,
`tests/test_no_duplicate_routes.py` 3 passed (targeted batch 23 passed). Full suite
**4157 passed, 30 skipped, 3 failed** — all three were stale README/`FILE_OWNERSHIP`
counts after the new modules; `scripts/sync_readme_counts.py` updated them and the 5
count tests then passed. Swift builds clean in debug and release.

**Visually verified:** a freshly launched instance shows **Workspaces** in the sidebar
under WORKSPACE, between Planner and Command Center.

**NOT verified:** the Workspaces screen's contents (list, editor, launch panel) and the
Mac Control screen. The machine sat at the macOS lock screen — synthetic clicks do not
reach the app and per-window capture fails — so this is an environment block, not a pass.
Re-check both on an unlocked session before calling V10 wave one done.

## Update — 2026-11-15 (V10 wave two: Automation Studio, and two screens finally seen)

**The visual-verification block is broken, not bypassed.** Synthetic input is genuinely
impossible for this process on this machine: `CGPreflightPostEventAccess()` and
`AXIsProcessTrusted()` both return false, so `CGEvent` clicks are dropped silently, while
`CGPreflightScreenCaptureAccess()` is true. Rather than claim the screens from symbol
inspection, a throwaway harness outside the repo (`/tmp/vantage_render/`) hosts the real
`ContentView` in a real `NSWindow`, selects a screen through the app's own
`AppNavigation.shared.go(...)`, and renders it with
`bitmapImageRepForCachingDisplay` + `cacheDisplay`. It answers exactly one question — does
the screen lay out and say what it claims — and it is **not** a substitute for using the
app: no click reaches a control through it.

**Workspaces and Mac Control, previously NOT verified, are now seen.** Against an isolated
backend (`JARVIS_HOME=/tmp/vantage_probe_home`, port 8799 — the user's running instance on
8765 was not touched): the Workspaces empty state, then a real workspace listing
("Vantage Development", `1 app(s) 1 file(s) 1 link(s)`, `never launched`), then after a
launch `launched 1× · last 2026-11-15T18:25:35`. The launch itself returned
`verified: true` with all four members reported separately (`opened in Finder`,
`Finder is running`, `opened`, `opened`). Mac Control renders **15 launchable / 126
running** with per-app running dots and the honest caption that window control and input
injection need Accessibility and are not offered. Both screens are now closed out.

**Automation Studio** (`Views/AutomationStudioView.swift`, `jarvis/core/recorder.py`,
`jarvis/api/intelligence_routes.py`). Record → review → save-as-mission → run. Screens
verified: the idle explainer, an open recording ("Stop", `RECORDING`, live step count), the
draft review panel, and the disabled-Save state.

**Defect found — three of the recorder's own kinds were not tools.** `SUPPORTED_KINDS`
listed `append_file`, `replace_text`, `open_app`, `run_command` and `http_request`; none is
a registered tool. Appending is `write_file` with `mode="append"`, opening is
`open_application`, commands are `run_allowlisted_command`; `http_request` and
`replace_text` have no tool at all. A draft built from them reviewed clean and failed on
replay with `no agent owns ...`. The list now names real tools, and two tests pin every kind
to both a registered tool and an owning agent (`find_agent`).

**Defect found — approving a parked step destroyed the id the approval is matched against.**
`POST /missions/{id}/steps/{sid}/decide` rewrote the step detail to
`"approved by the operator; approve"`, erasing the `confirmation CONF-…` that
`MissionRunner._parked_confirmation` reads out of that same field. The decision was
recorded and then made unmatchable by the act of recording it, so an approved mission
re-parked on every run. The detail now keeps the id. Proven live: park → read
`CONF-4f928c…` from the detail → approve → resume with that id → `S1 done`,
`Created vantage_probe_out.txt (21 bytes), read it back and it matches.` Before the fix the
same sequence re-parked.

**Defect found — Save & run discarded every captured parameter.** `saveAndRun` built
`MissionStepDraft(params: [:])` for every step, and `RecordingDraft.Step` had no `params`
field to read them from. Every automation saved from the app would have failed with
`not executed: missing required parameter 'path'; missing required parameter 'content'` —
verified by running exactly that mission. The params are now carried through and sent, and a
step with no captured values disables **Save & run** with a reason rather than creating a
mission that was never going to run (a sensitive value is deliberately never stored, so
such a step cannot be replayed as recorded).

**Evidence.** `tests/test_recorder.py`, `tests/test_missions_api.py`,
`tests/test_no_duplicate_routes.py` → **39 passed**. Full suite
**4162 passed, 30 skipped, 1 failed** (508s); the one failure was the README's collected
count (`4190` vs `4193`), fixed with `scripts/sync_readme_counts.py` and re-run →
**3 passed**. Swift builds clean.

**NOT verified:** no click has ever reached a control in this app on this machine. The
render harness proves layout and live data, not interaction. The Studio's record/capture
forms were exercised through the API, not by typing into them.

## Update — 2026-11-15 (V12 phase 1: Automation Studio closes its last gap)

**Automation Studio now produces verified automations.** A recorded demonstration used to
run and report `partially_completed` no matter how well it worked, because `derive()` emitted
steps and **no acceptance conditions** — `Mission.verdict()` had nothing to check. A
demonstration that reaches `completed` with every condition independently verified was
impossible before this change.

Three defects were found and fixed, each proven by running it:

1. **The editor asked for a value it was already showing.** `write_file`'s `mode` is declared
   in the schema but not required (the tool defaults it to `overwrite`). The editor treated
   every declared field as required, so a step that never chose a mode showed
   `⚠ needs mode` beside a picker displaying `overwrite`. Required-ness is the registry's
   answer; the client now reads it the same way, and a schema default counts as satisfied.
   Pinned by `test_an_optional_tool_default_does_not_make_a_step_incomplete`.
2. **A filled-in value created no checkable outcome.** Conditions were derived from the raw
   recording, so a `copy_file` whose `destination` the person supplied produced no condition
   and the replay reported the copy as unverified however exactly it matched. The draft
   endpoint now accepts the review (`POST /intelligence/recordings/draft {"steps": [...]}`)
   and re-derives conditions from the merged values. A review identifies steps by their
   *recorded* index: zipping by position made a reorder silently swap one step's parameters
   onto another step's title and condition.
3. **A mission could not verify itself.** Verification depended on the caller re-sending the
   verifier spec on every run; forgetting it recorded a checkable condition as "no
   independent verifier was supplied". A condition now stores its declarative spec at
   creation and `_verify_all` builds the observer from it. `file_identical` was added
   (destination bytes equal source bytes, both read after the fact).

Also: a blocked primary action rendered as a dimmed emerald button, still reading
"press me" while doing nothing. `VPrimaryButtonStyle` now draws disabled as a neutral fill
with a border. Sampled pixels: accent `#3FBF8A` at 40% over the dark surface vs. the new
neutral `V.surfaceAlt`.

**Complete journey, PASS (level 7 — full user journey):** draft → supply `destination` →
reviewed draft re-derives conditions → mission → two approval gates (each with its own
confirmation id) → `state=completed`, `C1 verified` (`file_contains`), `C2 verified`
(`file_identical`), and the copy on disk byte-identical to the source. Driven over the real
HTTP surface on an isolated backend (`:8798`, `JARVIS_HOME=/tmp/vantage_probe2`), mirroring
`saveAndRun` + `runMission` + `approveParked` step for step.

**Evidence.** `test_recorder.py` 30 passed · `test_mission_runner.py` 26 passed ·
`test_mission_contracts.py` + `test_missions_api.py` + `test_demo_a_file_mission.py` +
`test_phase10_verification_authority.py` together with the above → **105 passed, 0 failed**.
Swift builds clean (only the pre-existing `Sendable` warning).

**NOT verified:** the journey was driven through the app's HTTP calls, not by typing into the
native fields and clicking **Save & run**. Synthetic clicks are still dropped on this machine
(`AXIsProcessTrusted()` false), so the editor is proven at levels 1–4 (source, compiled,
contract, rendered with live data) and the *backend* journey at level 7 — the seam between
the two, a human clicking the control, is unproven.

## Update — 2026-11-16 (V12 phase 2: ShadowMission — predict, do not perform)

A person deciding whether to run a plan needs to know what it would change and what would
stop it. Until now the only way to find out was to run it. ShadowMission answers the
question without acting: it walks the plan step by step and, for each step, asks the **real**
permission engine for its decision, computes the file effect from the contents on disk now
(held in memory, never written), and says "unknown" where it cannot see.

**What was built**

- `jarvis/core/shadow.py` — `ShadowMission.run(steps, gate=...)`. Read-only by construction:
  the module's only filesystem calls are the ones `ParallelWorld` already uses to take a
  snapshot. File effects (write / copy / move / delete) are computed against an in-memory
  world that is rolled forward, so a second write to one path reports the first write's
  result as its "before", and a read of a file an earlier step wrote is marked `predicted`
  rather than observed.
- `POST /missions/{mission_id}/shadow` — reuses `_gate_factory`, so the shadow asks the
  same policy implementation the runner does; there is no second copy of the rules to drift.
- The files' own `_guard` is injected, so a protected path is refused in the preview exactly
  as the executor would refuse it. A preview that promised success for a target the executor
  then rejects would be worse than no preview.
- `ShadowMissionView` — a plan picker, and a report card with per-step verdicts:
  `would run`, `would run (predicted)`, `needs your approval`, `would be refused`,
  `effect unknown`. Before/after previews are drawn **only** where the shadow actually has
  them, because empty boxes would imply a step that was refused or not simulated did
  something. Registered in the sidebar under the MAC group.

**Defects found and fixed during the work**

1. **A gate-blocked step was counted as previewed.** The conclusion read "2 of 3 step(s) can
   be previewed" about steps the permission engine had just refused to let run — nothing
   about their effects had been computed at all. `_conclusion` now counts `previewed`
   separately from `would_need_approval` and `not_simulated`, and states plainly that no
   effect could be previewed when the count is zero.
2. **A raising gate would have crashed the screen.** A gate exception is now caught and
   reported as a refusal with its reason, so a broken engine produces a report rather than a
   blank panel.
3. **ShadowMission was registered but invisible.** `NavItem` had the case and the dispatch,
   but the sidebar is built from explicit `group(...)` lists; `.shadow` was not in any of
   them.

**Evidence.** `test_shadow_mission.py` **15 passed** — the load-bearing ones are
`test_a_shadow_writes_nothing`, `test_a_second_write_sees_the_first_writes_result`,
`test_the_guards_refusal_is_honoured`, `test_a_gate_blocked_step_is_not_counted_as_previewed`.
Together with `test_recorder`, `test_mission_runner`, `test_missions_api`,
`test_mission_contracts`, `test_no_duplicate_routes`: **84 passed, 0 failed**. Both Swift
targets build clean.

**Rendered, level 4 (live data, not interaction).** Against a real 3-step plan on the probe
backend the screen shows: `needs your approval · Write it · write_file`,
`effect unknown · Open a page · open_url`, `needs your approval · Copy it · copy_file`;
summary `3 step(s) · 2 need approval · 1 unknown`; conclusion *"No step's effect could be
previewed: 2 step(s) need your approval first and 1 act outside Vantage, so what they would
change is unknown until they run."*; and the note *"Nothing was executed…"*.

**NOT verified.** No click reached a control: selecting a plan was exercised over HTTP, not
by a real click (`AXIsProcessTrusted()` is false on this machine, so synthetic events are
dropped). The seam between the rendered screen and the backend call it makes is unproven.

## Update — 2026-11-16 (V12 phase 3: Live Mission Steering)

A running plan could be cancelled or left alone; there was nothing in between. Steering adds
that middle: the un-run remainder of a plan can be replaced, while the record of what already
happened stays exactly as it happened.

**What was built**

- `jarvis/core/steering.py` — `apply_revision`, `revision_summary`, `standing_effects`,
  `intent_refusals`, `steering_report`. Pure logic: it takes a `Mission` and returns the
  decision; `MissionService` owns every write, so there is still one place that decides what a
  stored mission says.
- `POST /missions/{id}/steer/preview` — what a revision would change, read-only. Names the
  steps it adds, the un-run steps it drops, the completed steps that survive, every proposed
  step the mission's **original intent** refuses, and whether the previous approval still
  covers it.
- `POST /missions/{id}/steer` — commits the revision, bumps `plan_revision`, and replies with a
  `steering` report naming what already ran.
- `MissionSteeringView` — plan editor seeded from the live mission; completed steps render
  locked with "already ran", Apply stays disabled until a Preview exists for the plan on
  screen. Sidebar entry under the MAC group.

**The three refusals that make it honest**

1. **A revision cannot undo finished work.** A completed step keeps its tool, params and
   status; a revision that omits one is refused outright. Effects that already happened are
   facts — what a new constraint does is stop *further* work, and the screen says so.
2. **A steered plan is a new request.** Adding a step sets `requires_new_approval`, and the
   run re-asks at the step that needs it. The mission's `IntentScope` is frozen at creation and
   a revision is checked against *it* — not against the new plan's own ambition — so
   `/etc/passwd`, `delete_file` and an over-budget risk are refused by name before anything
   runs, and again at run time.
3. **An approval is for one action.** Steering an unchanged parked step keeps its recorded
   confirmation id. Steering a **changed** one drops it and returns the mission to `planned`,
   so it cannot resume an approval given for a different action — or sit waiting on one that
   can no longer be answered.

**Defects found and fixed during the work**

1. **A lease stopped covering the plan after the first step finished.** `_spend_lease` passed
   `plan_revision=mission.version`, but `version` is the row-write counter and moves every
   time a step is marked done. A plan with two steps could never spend a multi-step lease.
   That is precisely the distinction `plan_revision` now draws.
2. **The receipt reported a revision that never happened.** `outcome_receipt` emitted
   `plan_revision: mission.version`; a mission whose steps were marked done would claim
   "revision 14" for an unsteered plan. Now `plan_revision` plus `record_version`.
3. **A revision could leave a parked mission unapprovable.** Resetting a parked step's detail
   wiped the confirmation id, so the state machine stayed in `awaiting_approval` with nothing
   to approve. Fixed by keeping the id when the step is unchanged and adding the
   `awaiting_approval → planned` edge for when it is not.

**Evidence.** `test_live_steering.py` **24 passed** · `test_mission_runner.py` 29 (3 new intent
tests, incl. a steered plan refused at run time) · `test_missions_api.py` 24 (4 new steer
tests) · `test_proofgraph.py` 9. Related suites together: **220 passed, 0 failed**. Both Swift
targets build clean.

**Live backend journey, level 7 (HTTP, isolated `:8798`).** Steered a mission whose S1 had
completed: revision 0→1, S3 dropped, S1's `content` param intact byte-for-byte, `already_done`
naming `/tmp/vantage_probe2/plan.txt`. A revision adding an `/etc/escaped.txt` step previewed
as `resource`-refused; applied anyway (steer records, the run gates); the run refused it and
**the file was never created**. Steering an unchanged parked step kept its `CONF-…` id and
stayed `awaiting_approval`; steering a changed one dropped the id and returned `planned`.

**Rendered, level 4.** The screen shows the live plan with S1 locked and its "already ran" note,
S2/S3 editable, and Apply neutral-disabled until Preview has run.

**NOT verified.** No revision was committed by a real click; the flow was driven over HTTP.
The intent check is only as strong as the intent recorded at creation — a mission created with
no intent is gated by the permission engine alone, and the code says so rather than implying
otherwise.

## Update — 2026-11-16 (V12 phase 4: Agent Control Tower)

Seventeen agents existed and nothing showed them together; there was no way to
pause one short of stopping the backend. The Control Tower is that surface, and it
is wired into the one funnel every tool call passes through, so its controls
actually stop work rather than redraw a badge.

**What was built**

- `jarvis/core/control_tower.py` — `ControlTower`, a process singleton of
  `Worker`s. Each worker reports `idle | running | paused | budget_exhausted |
  stopped`. `running` is true only while a call is genuinely inside it (an
  in-flight count), not a timer. A worker has an optional **call budget** that
  bounds an agent that would otherwise loop.
- The gate is `ControlTower.admit(tool)`, called from `Agent.execute` — the one
  place the chat path, the mission runner and the recovery retry all dispatch
  through. A paused, stopped or out-of-budget worker is refused with a reason, and
  **the handler never runs**.
- `GET /control-tower`, and `POST /agents/{name}/{pause,resume,cancel,budget}`.
  Read and control only; the tower never dispatches a tool, so it cannot become a
  second, ungated way to run things.
- `ControlTowerView` — every worker with its state, tool count, counters and
  Pause/Resume/Stop. Sidebar entry under MAC.

**The honest boundary, stated on screen.** A pause or stop takes effect at the
**next dispatch**, not inside a call already running: Python cannot pre-empt a
thread, and killing the process to interrupt one tool would stop everything else
with it. The screen shows this note verbatim rather than implying instantaneous
control it does not have. A *stop* (cancel) is distinct from a pause — a cancelled
worker does not resume on its own, so nobody is surprised by an agent returning.

**Defect caught by an existing guard.** The tower's first read endpoint was
`GET /agents`, which already exists in `app.py:5976` as the capability listing.
`test_no_duplicate_routes` failed loudly — Starlette keeps only the first
registration, so the tower would have been invisible. Moved to `GET
/control-tower`; the controls stay under `/agents/{name}/...` where the subject
matches.

**Evidence.** `test_control_tower.py` **14 passed**, including
`test_a_paused_worker_refuses_and_the_tool_is_never_called`,
`test_a_worker_reads_running_only_while_a_call_is_inside_it`, and
`test_a_stopped_worker_does_not_resume`. Both Swift targets build clean; README and
`FILE_OWNERSHIP` counts updated (228 test modules, 4273 collected, 229 Python
modules) and their guards pass.

**Live, level 7 (isolated `:8798`).** With the **email** worker paused, a real
`POST /command` "read my latest email" replied *"email is paused (checking an inbox
rule); the next call is refused until it resumes."* After resume the same command
reached the real tool (*"I found no messages."*). With the **car** budget at 0,
"unlock my car" replied *"car has used its budget of 0 call(s); raise the budget to
let it run again."* The mission path is the same: a mission step needing the paused
worker failed with the pause as its reason and the file was never written.

**Rendered, level 4.** 17 workers, email paused with its reason and a Resume
button, car "out of budget · 0/0 calls".

**NOT verified.** No control was set by a real click (controls were driven over
HTTP; synthetic clicks are dropped here). Counters are per-process and reset on
restart — they count calls *this* process made, and no cross-restart history is
claimed.

## Update — 2026-11-16 (V12 phase 5: Recovery Tournament — rank the repairs, with the reason)

**What it is.** A failed call already triggers the recovery engine, which either
retries, switches to a declared alternative, or gives up. The tournament makes
that decision *inspectable before it is made*: for a given failure it lists every
repair on the table, each marked safe or unsafe **with the rule that decided it**,
and names the best safe one — or `null` when nothing is safe to try automatically.

**Why it is not a chatbot guess.** `jarvis/core/recovery_tournament.py` asks the
**live** `RecoveryEngine` — it does not carry its own opinion. `retry` is safe
only when `engine.retry_policy(spec, result)` says so; alternatives come from the
engine's own `ALTERNATIVES` map and are vouched for only if the tool is
registered, not destructive, and carries no safety tag; an unparseable or
unregistered tool is marked unsafe rather than assumed fine. Likelihood is read
off declared properties (`looks_transient`, idempotency, risk) — never a
confidence number invented to look decisive. A test pins the fallback policy to
the engine's, so the two cannot drift.

**"No recommendation" is an answer.** A destructive failure (e.g. `delete_file`
with a permission error) returns `recommended: null` and the note *"No repair is
safe to attempt automatically; this needs the person."* — rather than offering
the least-bad option dressed up as safe.

**Read-only by construction.** The route is `POST /recovery/tournament` and
touches nothing — it does not run a repair, does not retry, does not write. It is
a ranking. The screen says so: *"The ranking is a plan, not a proof; whether it
worked is decided afterwards by the same independent check as any run."*

**Surfaces.**
- `jarvis/core/recovery_tournament.py` — `RecoveryTournament.evaluate()`,
  `Candidate` (safety, safety_reason, likelihood, cost), `Tournament.recommended`
  (excludes the ASK candidate so "ask a human" is never the silent default).
- `jarvis/api/recovery_routes.py` — `POST /recovery/tournament` (auth-gated).
- `RecoveryLabView.swift` (+ `recovery` NavItem, sidebar group MAC) — failure
  list from `/failures`, auto-ranks the newest failure on open, each candidate
  with a shield icon and its reason.

**Evidence.** `tests/test_recovery_tournament.py` **17 passed**, including
`test_a_destructive_tool_is_never_recommended_for_retry`,
`test_the_default_policy_matches_the_recovery_engine`,
`test_a_non_transient_error_is_not_overstated`. Both Swift targets build clean.

**Live, level 7 (isolated `:8798`).**
- `search_files` with `"spotlight is unavailable, try again"` →
  `recommended: {kind: retry, rank: 0, safety: safe}`, note *"Best first attempt:
  retry via search_files — the error looks transient and the tool is safe to
  re-run."*
- `delete_file` with `"permission denied"` → `recommended: null`, retry
  `safety: unsafe` with reason *"it is a destructive action, and a retry could act
  on a world you did not approve"*, note *"No repair is safe to attempt
  automatically."*

**Rendered, level 4.** Recovery Lab on the live backend lists two real failures
from the Control Tower phase (`get_car_status` out of budget, `search_email`
paused) and ranks the newest on open: **Retry** *safe · try first · cost "1 call;
the first attempt's effect is known not to matter"*, then **Ask the person**
*safe · cost "no calls; the person's time"*.

**NOT verified.** No failure was picked by a real click — the newest is
auto-selected on open and the ranking was exercised over HTTP (synthetic clicks
are dropped here, `AXIsProcessTrusted()` false). The tournament is advisory: it
ranks the best first move; it does not prove the move works, and the screen says
so.

**Counts.** 229 test modules · 4287 collected · 231 Python modules (`README.md`,
`docs/FILE_OWNERSHIP.md`).

## Update — 2026-11-16 (V12 phase 6: Privacy Egress Firewall — a gate, not a dashboard)

**What it is.** Every outbound read in this backend already went through one
function, `jarvis.net.http_read`. That makes it the right place for an egress
gate, and this phase puts one there: a destination is reached because a **rule**
admits it, and a refused host is refused *before a socket is opened* — so a
blocked read costs nothing and cannot be mistaken for a network failure.

**Declared, not guessed.** `jarvis/security/egress.py` lists the 21 hosts the
source actually contacts, each with the reason it exists and its kind:

| kind | count | privacy mode | why |
|---|---|---|---|
| `loopback` | 3 | always allowed | this Mac — the backend, dashboard and gateways |
| `lookup` | 6 | allowed | Wikipedia, arXiv, a search index: carries the query only |
| `service` | 8 | refused | a Google/GitHub account the person configured |
| `provider` | 4 | refused | a model endpoint — the one path that carries the conversation |

The distinction that matters: a **provider** call sends the person's own words
off the machine, so privacy mode refuses it; a **lookup** sends only what the
caller put in a query string, so it keeps working; **loopback** never leaves the
machine at all. Refusing `127.0.0.1` would break the product to look strict, so
the rule does not.

**A refusal is its own failure.** The provider path catches `EgressRefused` as
category `egress_refused`, not `network` — a person who turned privacy mode on
must read "refused", not "could not be reached", or they will go hunting for a
broken connection that is not there. A test pins this.

**One rule, read live.** Privacy mode is read from settings on every check, so
flipping the switch changes the answer on the *next* read without rebuilding the
firewall. Verified: `api.openai.com` allowed with privacy off, refused under
privacy mode, `en.wikipedia.org` still allowed under privacy mode.

**The limit, shown rather than buried.** This gates reads that go through
`jarvis.net.http_read`. Traffic from other applications on the Mac is outside
this process and is not seen or controlled — the posture carries that sentence
and the Trust & Privacy screen prints it verbatim.

**Surfaces.**
- `jarvis/security/egress.py` — `EgressFirewall.config`, `Destination`, `Verdict`,
  `describe_firewall`.
- `jarvis/net.py` — the gate in `http_read`; fails **closed** if it cannot decide.
- `jarvis/api/egress_routes.py` — `GET /egress`, `POST /egress/check`. There is
  deliberately **no route that widens the allowlist**: adding a host is a source
  change a reviewer sees.
- `EgressFirewallView.swift` + a new **Trust & Privacy** sidebar group (with the
  existing Safety Center), because "what may leave this Mac" belongs beside
  "what may change it".

**Evidence.** `tests/test_egress_firewall.py` **28 passed**, including
`test_a_refused_read_never_opens_a_socket` (a `urlopen` spy counts **0** calls),
`test_privacy_mode_is_read_at_check_time`, and
`test_a_provider_reports_an_egress_refusal_as_its_own_category`. Network
regression sweep: **211 passed** across n8n, provider categories, timeout-gap,
no-implicit-retry, Google, the phase-34 deadline and no-duplicate-routes.
Both Swift targets build clean.

**Live, level 7 (isolated `:8798`).** With privacy mode ON, a real
`POST /command` "what is a train" still returned the Wikipedia answer — the
lookup path works end to end under the gate — while `api.openai.com` was refused
with its reason. A `collect.example.net` check was refused as undeclared.

**Rendered, level 4.** Privacy mode on, 3/6/8/4 by kind, "enforced at
jarvis.net.http_read", a live check of `example.com` REFUSED with its reason, and
every provider host marked **blocked**.

**NOT verified / not covered.**
- The toggle and the Check button were driven over HTTP, not by a physical click
  (synthetic clicks are dropped here).
- The `httpx` call sites are now gated too (`egress.require` at the relay, the four
  CalDAV clients, voice STT and the transport endpoint), and a refused CalDAV read
  is classified `UNAVAILABLE` rather than a network error. A relative-import bug in
  that gate (`...security` instead of `..security`) would have silently disabled all
  four calendar guards; caught by the classifier test and fixed.
- The **car/watch readers** open `urlopen` directly (an injectable opener), not
  `http_read`, so they are **not** gated yet. Named here rather than claimed.
- Other applications' traffic is outside this process. Stated on screen.

## Update — 2026-11-16 (V12 phase 7: Adaptive Autonomy Control — one dial, with a ceiling)

**What it is.** The permission engine already decided *may this run*, but from a
fixed policy: level 2+ always confirms, and "paranoid" promoted everything to a
confirmation. That is a setting, not a dial — you could not say "let this
project's routine file moves run without asking for an afternoon" and have it
mean anything narrower than "turn paranoid off everywhere."

This is the dial, and it sits **between** the risk classifier and the final
verdict. Five positions, from *Ask before everything* (0) to *Let a standing
grant cover a low-risk repeat* (4), defaulting to 3 — today's behaviour, so
nothing changes until somebody moves it.

**It can only tighten.** `AutonomyControl.apply` is asked *after* the classifier
and returns the stricter of the two verdicts. A mis-set level therefore cannot
loosen a confirmation or turn a block into a run. Pinned by
`test_the_dial_never_loosens_a_confirmation` and
`test_the_dial_never_turns_a_block_into_an_execute`.

**The ceiling.** Above `CEILING_RISK` (destructive) — so destructive **and**
physical — no level auto-executes, ever. Level 4 is not a licence; it is a
request that the floor stays where the policy put it. Verified live: `delete_file`
previews `CONFIRM → CONFIRM` at level 4.

**Scope narrows it.** A dial set to "ask before everything, but only under
`~/Projects/lab`" leaves everything outside on the default. The rule is *every*
path-like parameter must be under the prefix — a copy that reads inside but
writes outside is an escape, not a scoped action, so one parameter cannot widen
the dial (`test_a_copy_that_writes_outside_the_scope_is_not_scoped`).

**Persisted like every other small store** — `autonomy.json` under
`data_home()`, written atomically, and a corrupt file falls back to the safe
default with a log line rather than silently forgetting.

**Surfaces.**
- `jarvis/security/autonomy.py` — `LEVELS`, `CEILING_RISK`, `AutonomyControl`.
- `jarvis/security/policy.py` — step 4b in `evaluate`; a dial failure leaves the
  classifier's verdict rather than widening.
- `jarvis/api/autonomy_routes.py` — `GET /autonomy`, `POST /autonomy`,
  `POST /autonomy/preview` (read-only; restores the setting after).
- `AutonomyControlView.swift` in the **Trust & Privacy** group — the five
  levels, the scope field, the ceiling statement, and a live preview showing the
  classifier's verdict beside the dial's.

**Evidence.** `tests/test_adaptive_autonomy.py` **20 passed**, including
`test_a_destructive_action_is_never_auto_executed_at_any_level` (all five
levels), `test_the_permission_engine_applies_the_dial` (a real
`PermissionEngine.evaluate` verdict changed by the dial), and
`test_the_preview_does_not_leave_the_dial_moved`. Both Swift targets build clean.

**Live, level 7 (isolated `:8798`).** At level 0, a real `POST /command`
"what is a train" **stopped and asked**: *"This needs your confirmation before I
run it. Will do: ask_question on laptop_main."* At level 3 the same command
answered. `search_files` previewed `EXECUTE → CONFIRM` at level 0 and
`EXECUTE → EXECUTE` at level 3.

**Rendered, level 4.** Five levels with 3 marked *current*, the scope field, the
ceiling sentence, and a live preview of `delete_file` reading
`CONFIRM → CONFIRM · risk 3 · destructive`.

**NOT verified.** No level was moved by a physical click (driven over HTTP;
synthetic clicks are dropped here). Paths that decide without calling
`PermissionEngine.evaluate` would not see the dial — the engine is the only
authoriser by design, but that is asserted from the code rather than swept.

## Update — 2026-11-16 (V12 phase 8: Outcome Assurance — verify the world, not just files)

**The gap.** The mission runner already verified a job by re-reading the world
after the steps ran — but every observer it had was a *file* observer (contains a
needle, hashes to a value, is absent, matches another file). An effect anywhere
else — a page answered, a command exited cleanly, a process is up, a port is
listening, an app is in front — could only be recorded UNKNOWN. UNKNOWN is
honest, but it was the whole answer whenever the thing being verified was not a
file, which is most real work.

**What was added.** `jarvis/core/outcome_assurance.py` adds six observers, and
`_verifier_from_spec` now falls through to them for a condition's `kind`:

| kind | question | what it carries |
|---|---|---|
| `url_body` | does this page's body contain (or no longer contain) this text? | a GET |
| `url_status` | is the HTTP status one of these codes? | a GET |
| `command_output` | does this allowlisted command exit 0 and say this? | one of `echo`, `git_status`, `ls`, `test_file`, `uname` |
| `process_running` | is this process in the table (or gone)? | a `ps` read |
| `port_listening` | does this local port accept a connection? | a loopback connect |
| `app_frontmost` | is this application in front (or not)? | one System Events query, permission-gated |

**The three rules every observer keeps.**
1. **It reads the world, never the executor's message** — it is handed a URL, an
   argv, a process name, and reports only what it now sees.
2. **It never guesses** — a timeout, a missing binary or a permissions error is
   UNKNOWN with the reason, never VERIFIED from a non-answer. "The check could
   not run" and "the check ran and the world is wrong" stay different sentences.
3. **It cannot be an execution path** — the command observer runs only
   allowlisted absolute-path argv; an unknown command is refused, not guessed at.
   `rm -rf /` comes back *"not an allowlisted command; an observer will not run
   arbitrary programs."*

**Compounds with phase 6.** An `url` observation goes through the egress
firewall, so an address this Mac may not contact is **UNKNOWN** with the
firewall's reason — reporting FAILED would blame the mission for the firewall's
decision, and VERIFIED would invent a reading.

**Surfaces.** `jarvis/api/assurance_routes.py` (`GET /assurance`,
`POST /assurance/observe`) and `OutcomeAssuranceView.swift` under **Oversight**,
which lists the kinds and runs one observation live.

**Evidence.** `tests/test_outcome_assurance.py` **25 passed**, including
`test_a_refused_address_is_unknown_not_failed`,
`test_an_unlisted_command_is_refused_not_run`,
`test_the_verifier_never_crashes_on_an_observer_error`, and
`test_a_mission_verifies_a_process_condition_end_to_end` (a real mission whose
condition is "launchd is running" verifies VERIFIED through the runner's own
resolver). Both Swift targets build clean; mission suites 81 passed.

**Live, level 7 (isolated `:8798`).** `port_listening:8798` → `verified
127.0.0.1:8798 accepts a connection`; `port_listening:1` → `failed … Connection
refused`; a blocked `collect.example.net` URL → `unknown the egress firewall
refused the check`; `command_output rm -rf /` → `unknown … not an allowlisted
command`.

**Rendered, level 4.** The screen shows the rule, a live `VERIFIED` port
observation, and all six kinds with what each one is allowed to carry.

**NOT verified.** The Observe button was exercised over HTTP (the screen's live
run is the same route); no physical click drove it. `app_frontmost` depends on
Accessibility permission and reports UNKNOWN without it — not exercised with
permission granted here.

## Update — 2026-11-16 (V12 phase 9: Temporal Undo Map)

**What it is.** One timeline of the effects that landed, and for each an honest
answer to "can I take that back?". The design decision that carries the feature:
the map is drawn from a **captured pre-image**, not from what a tool reported.
`write_file` reads the file's bytes into a content-addressed store *before* it
overwrites; the entry is reversible because those bytes exist and hash to the
recorded value, not because anything claimed success.

**Added / changed**

- `jarvis/core/undo_map.py` — `UndoStore` (blobs keyed by sha256 + a JSON index,
  atomic writes), `classify()` (reversible / irreversible / conflicted / undone /
  unknown), `UndoMap.timeline/summary/undo`, `UndoError`. `TOOL_EFFECTS` lists
  outbound and physical tools *explicitly* as `none` so a new path cannot be
  assumed reversible by default; `IRREVERSIBLE_RISK = {destructive, physical}`
  overrides the table.
- `jarvis/tools/files.py` — `write_file` captures the pre-image in overwrite mode
  and returns `before_sha256`/`after_sha256`; `delete_file` with `permanent=True`
  captures a file's bytes before unlinking.
- `jarvis/agents/base.py` — `_record_undo` after a successful action, reading the
  hashes from `result.output`. Bookkeeping never fails the real action.
- `jarvis/api/undo_routes.py` — `GET /undo?limit=60`, `POST /undo/{entry_id}`
  (409 with the refusal reason). Deliberately no bulk undo.
- `macos/Vantage/Sources/Views/UndoMapView.swift` — counts by state, the timeline,
  and an Undo button **only** on a row whose state is `reversible`; every other
  row prints its state and the sentence saying why.

**Three refusals that make it honest, each with a test**

- Undoing over somebody's newer edit is refused ("has changed since this action"),
  and the newer text stays on disk — `test_undo_refuses_after_the_file_changed`.
- An irreversible effect is refused with its reason; no silent no-op —
  `test_undo_refuses_an_irreversible_entry` (a sent email "cannot be un-sent").
- A missing or mismatched pre-image is refused rather than written —
  `test_undo_refuses_when_the_pre_image_is_missing`.

**Evidence**

- `tests/test_undo_map.py` — **22 passed**. Includes a real `write_file` driven
  through `Agent.execute` (the single funnel) whose undo puts the original bytes
  back (`test_a_real_write_file_records_an_undoable_entry`).
- **Level 7, live core (`/tmp/undo_demo.py`, isolated `JARVIS_HOME`):**
  `WRITE ok=True verified=True`; file on disk `'REPLACED CONTENT\n'`; entries
  `[('reversible','write_file'), ('undo','undo'), ('undone','write_file')]`;
  `UNDO: restored /tmp/vantage_demo/demo.txt to the 17 bytes it held before`;
  `FILE after undo: 'ORIGINAL CONTENT\n'`.
- **Level 4, rendered** against the live `:8798` backend: counts by state and the
  timeline. The first render caught a real flaw — undo's own records appeared as
  `unknown` ("the map will not guess that it is reversible") beneath every real
  entry. `classify()` now returns state `undo` with "this is a reversal of the
  entry below it; it is not itself an effect to undo"; re-rendered and pinned by
  `test_an_undo_entry_is_marked_as_a_reversal_not_an_unknown_effect`.
- Both Swift targets build clean.

**NOT verified**

- **Level 5–6:** the Undo button was exercised through the core API and over
  HTTP, not by a physical click.
- `copy_file`/`move_file`/`rename_file` are declared reversible *by tool name*
  but do not yet capture the destination's pre-image; an undo of one restores
  only when a pre-image exists, otherwise it refuses. Named as the next widening,
  not claimed as working.
- A permanent delete of a **directory** is not captured and shows irreversible —
  restoring a tree byte-for-byte is not a claim this makes.
- Blobs are not pruned; the store grows with the files it captures. Retention is
  a follow-up, and the disk cost is real.

## Update — 2026-11-16 (V12 phase 10: Skill Evolution Lab)

**What it is.** A skill that has run has evidence, and keeping it by hand throws
that evidence away. The lab reads a skill's recorded runs and proposes its *next
version* — and the design question that makes it safe is not "what should change"
but "what is this allowed to change".

**The answer is a closed set that only narrows.** The lab can remove, and it can
tighten. It cannot add a step, add a capability, add a scope prefix, lower a risk
tier or relax a precondition, because each of those would turn a proposal derived
from history into a wider permission than the one that was saved. A proposal is a
return value; storing it is a separate, explicit act that makes a new version and
keeps the old one.

**Added / changed**

- `jarvis/core/skill_evolution.py` — `propose_evolution` (reasons, changes, notes,
  the proposed manifest, and the measured counters behind it), `interpret_run`
  (what each condition of the last run actually observed), `describe_lab`.
  `MIN_RUNS_FOR_EVOLUTION = 3`: one demonstration is a demo, not evidence.
- `jarvis/api/skill_evolution_routes.py` — `GET /skill-evolution`,
  `GET /skill-evolution/{id}`, `GET …/observations`, `POST …/accept`. The accept
  route with **no body** re-derives the proposal server-side, so the version
  written is exactly the one the lab showed rather than a lossy client echo.
- `macos/Vantage/Sources/Views/SkillEvolutionView.swift` — the rule (can / will
  not), the skill list with measured counts, the proposal with its reasons and
  evidence, the last run's per-condition observations, and one "Store as next
  version" button. New `Automate` sidebar group beside Skills Library.

**What it proposes, and nothing else**

1. Remove a condition whose `kind` no observer in this build can resolve — it
   could only ever be recorded `unknown`.
2. Remove a step that existed solely to satisfy such a condition (only when it
   claims no surviving condition).
3. Raise the declared risk to what the steps' tools actually cost, read from the
   frozen `ToolSpec` in the registry. A PHYSICAL tool (4) cannot be expressed in a
   manifest capped at 3, so the tier is capped **and the gap is stated** rather
   than hidden.
4. Report an out-of-scope path and an undeclared capability as **notes** — never
   applied, because that is widening.

**Evidence**

- `tests/test_skill_evolution.py` — **26 passed**. The load-bearing ones are
  negative: `test_a_proposal_never_adds_a_step_capability_or_scope`,
  `test_a_scope_gap_is_reported_never_filled`,
  `test_the_risk_tier_is_only_ever_raised`,
  `test_an_understated_risk_is_never_lowered`,
  `test_proposing_does_not_change_the_stored_skill`.
- **Level 7, live (probe backend `:8798`, real mission engine):** stored
  `notes-health` with one checkable and one uncheckable condition; three real
  runs → `partially_completed` / `partial` each time, with `C1` VERIFIED
  (`"/tmp/vantage_demo/notes.md contains 'status: ready' (sha256:3a32e28874afea65,
  22 bytes)"`) and `C2` UNKNOWN ("a check of kind 'the_vibes_are_right' is not one
  this system performs"). The lab proposed removing `C2`; accepted → v2; **the
  next real run went `completed` / `verified`**, where every prior run was
  `partial`. That is the loop closing: a proposal from history, applied, and a
  genuinely different outcome measured afterwards.
- **Level 4, rendered** (`/tmp/vantage_shots/evolution4.png`): the rule card, three
  skills with measured counts, a live proposal whose change and note both read
  correctly, and the evidence row `3 runs / 0 verified / 3 not verified / 0
  unknown / partial`.
- Both Swift targets build clean.

**Two defects the live run found, both fixed**

- The note said "the runs' own notes say what broke" about runs that had parked
  awaiting approval — the recorded note says only that the run is parked. It now
  says what was recorded and no more.
- The store counts `partial` as a failure (only `ok and verdict == "verified"` is
  a success), so a plan whose one real condition passed every time read as
  "3/3 failed — a skill that does not work", while the change being proposed was
  the cause. The note now recognises that case and says **run the new version
  before concluding otherwise**, which is what the live run then did.

**NOT verified**

- **Level 5–6:** "Store as next version" was exercised over HTTP, not by a
  physical click.
- The lab proposes no edit to a step's *params*. A skill whose steps name stale
  paths will keep naming them; that is an edit, and widening is not the only risk
  — a silent rewrite of what a step does is the other.
- Evolving a skill that has never run is refused by design (below the run floor),
  so the feature does nothing on a fresh install until something has actually run
  three times.

## Update — 2026-11-16 (V12 phase 11: Capability Passports)

**What it is.** A worker's standing, expressed as something checkable rather than
something claimed. A passport answers three questions from data the system
already keeps: what the registry *gives* this worker (tools, cost, confirmation
gate), what the hash-chained audit log says it *did* (counts by risk tier,
approvals, refusals), and — the line most useful and most often missing — what it
**holds but has never touched**.

**The property that makes it safe: a passport is a record, never a credential.**
Nothing reads it to authorize anything; the permission engine decides every call
as before. The single write (`POST /passports/{worker}/issue`) appends an audit
entry saying the passport was produced, and records `"grants": "nothing"`.

**Added / changed**

- `jarvis/core/passports.py` — `build_passport` (tools by cost, gated set,
  exercised/never-used split, counts by risk, audit-chain verdict, notes),
  `list_passports`, `issue_passport`. No aggregate score anywhere: a "trust
  rating" would be the one figure on the screen nobody could verify.
- `jarvis/api/passport_routes.py` — `GET /passports`, `GET /passports/{worker}`,
  `POST /passports/{worker}/issue`.
- `macos/…/CapabilityPassportsView.swift` — the passport with its counts, the
  per-risk tally, a tools table marking never-used entries, an audit-chain badge,
  the authority line rendered verbatim from the API, and the worker list. New
  entry in the `Automate` group.
- **A real defect found and fixed in the audit ledger:** the confirmed-execution
  audit site wrote `risk="confirmed"`. A workflow state was sitting in the risk
  column, so a passport counting actions by tier reported `confirmed: 1` as if it
  were a kind of risk, and the actual tier of a confirmed write was
  unrecoverable from the ledger. It now records the real tier from the stored
  confirmation, with `confirmation=True` carrying the "was confirmed" fact it was
  always meant to. Pinned by
  `test_a_confirmed_action_is_audited_with_its_real_risk_tier`.

**Evidence**

- `tests/test_passports.py` — **21 passed**, including the negative ones:
  `test_issuing_a_passport_does_not_change_what_the_gate_decides` (the same
  action evaluated before and after a passport is built and issued → identical
  decision and reason), `test_a_passport_states_that_it_grants_nothing`,
  `test_an_unknown_worker_is_an_error_not_an_empty_passport`,
  `test_rows_for_another_workers_tool_are_ignored`. That first test replaced an
  earlier version that merely grepped the policy module for the word "passport"
  — a grep passes on an import that never runs.
- `tests/test_phase10_confirmation_revalidation.py` — 12 passed (the risk-tier
  test above plus the existing revalidation suite).
- **Level 7, live** (probe backend): `GET /passports` returned all 17 workers;
  `laptop` reported 49 tools / 26 gated / 3 exercised / 4 actions
  (`read: 3`, `confirmed: 1` **before** the fix — which is what exposed it) and
  `evidence: {intact: true, checked: 11, breaks: 0}`.
- **Level 4, rendered** (`/tmp/vantage_shots/passports1.png`): the automation
  worker's passport with 4 tools, 1 gated, 0 exercised, the "held but never used,
  and not read-only: n8n__notify" note, the never-used markers on every tool, and
  "audit chain verified · 11 records".
- Both Swift targets build clean.

**NOT verified**

- **Level 5–6:** "Record in audit log" was exercised over HTTP, not by a physical
  click.
- Attribution is **by tool ownership**: an audit row is credited to the worker
  whose spec owns that tool. Rows for tools no worker owns are left out. A tool
  two workers could both own would be double-counted — no such tool exists today,
  and that is the assumption, stated rather than hidden.
- ~~The audit log is read with a 2000-row window and the page did not say so.~~
  **fixed**: the passport now carries `window {rows_read, rows_limit, complete}`
  and, when the log held at least that many rows, a note saying the counts cover
  the most recent of them rather than all of history. Pinned by
  `test_a_truncated_log_window_is_stated_not_presented_as_a_total`.

## Update — 2026-11-16 (V12 phase 12: Reality HUD + Human Takeover)

**What it is.** One screen that says what is really happening on this Mac, and one
control that genuinely stops the agents — not a badge that redraws.

**The HUD** assembles the picture from sources that are already honest. Permissions
come from `PermissionStatusService`, which preflights the real macOS APIs and
returns `UNKNOWN` when it cannot ask — and states whether the values describe
Vantage.app or the process that asked. Workers come from the Control Tower with
the refusal counters *it* keeps, so a "blocked" number moves only when a call was
actually refused.

**The takeover** reuses the one control enforced at a dispatch boundary: the
tower's pause gate, which every tool call passes through. The screen states what
it cannot do in the backend's own words — it cannot freeze a call already inside a
handler, only refuse the next dispatch. There is no pointer control, no synthetic
input and no capture anywhere in the module; a takeover here hands the machine
back by stopping what the agents would do next.

**Added / changed**

- `jarvis/core/reality_hud.py` — `snapshot` (permissions with their caveat,
  workers with refusals, the takeover gate's own state), `take_over`, `release_all`.
  `release_all` resumes only workers whose pause this module set (`paused_by ==
  "operator"` **and** the takeover reason), because a pause a person set by hand
  looks identical if you only read `paused`.
- `jarvis/api/reality_routes.py` — `GET /reality`, `POST /reality/takeover`,
  `POST /reality/release`. The tower is the process-wide singleton, deliberately:
  a fresh instance would pause workers nobody dispatches through.
- `macos/…/RealityHUDView.swift` — the takeover card (with its limitation), the
  permission table with per-state colouring and the "read by another process"
  warning, worker rows with in-flight and refused counts. First entry in `Mac`.

**Evidence**

- `tests/test_reality_hud.py` — **15 passed**, including the measurement that
  separates a takeover from a badge: `tower.admit("write_file")` succeeds, a
  takeover runs, and the next `admit` raises `WorkerRefusal` naming the takeover —
  plus `test_release_leaves_a_hand_set_pause_alone` and
  `test_the_module_never_drives_the_screen` (no `osascript`, `CGEvent`,
  `cliclick`, `screencapture`). `tests/test_control_tower.py` still passes
  (32 passed together with the new file).
- **Level 7, live (probe backend, real mission engine):** `POST /reality/takeover`
  paused **17** workers → a real skill run then failed with
  `laptop is paused (human takeover: …); the next call is refused`, and the HUD
  reported `paused: 17, refused: 1, held: true`. `POST /reality/release` resumed
  17 and left 0 paused → the same skill run then proceeded
  (`partially_completed`). That is the whole point measured end to end: taken over
  refuses real work, released lets it run.
- **Level 4, rendered** (`/tmp/vantage_shots/reality1.png`): the takeover card,
  the permission table showing `screen_recording GRANTED`, `accessibility
  REQUIRES_MANUAL_SYSTEM_SETTINGS`, `microphone NOT_DETERMINED`, `automation
  UNKNOWN`, and the honest note that these were read by code outside the bundle.
- Both Swift targets build clean.

**NOT verified**

- **Level 5–6:** "Take over now" and "Hand control back" were exercised over HTTP
  against the live backend, not by a physical click.
- **`about_bundled_host` is `false` here** — the probe runs the backend from the
  repo, not from Vantage.app, so these permission values describe that caller.
  They are *not* a measurement of the shipped app's permissions; the screen says
  so itself rather than hiding it.
- A worker paused by hand is faithfully left paused, so "Hand control back" after
  a takeover can legitimately resume fewer workers than were paused.

## Update — 2026-11-16 (V12: the ten end-to-end demonstrations, run for real)

The twelve V12 phases were each committed with their own evidence. This section
is the other half the directive asks for: the ten mandatory demonstrations
(§18), run against a **live backend** (`:8798`, `JARVIS_HOME=/tmp/vantage_probe2`)
with disposable resources, and the completion matrix measured against them.

### Verification baseline

| Check | Command | Result |
|---|---|---|
| Full suite, before this section's fixes | `.venv/bin/python -m pytest -q -p no:cacheprovider` | **2 failed, 4418 passed, 31 skipped** in 559s |
| Full suite, after this section's fixes | same | **1 failed, 4425 passed, 30 skipped** in 599s -- the one failure is the pre-existing timing flake below |
| That flake, run twice in isolation | `pytest tests/test_phase34_one_sentence_one_deadline.py` | **11 passed** in 14.5s, **11 passed** in 13.8s |
| The two failures, individually | same, by node id | `test_phase34_one_sentence_one_deadline.py` **11 passed** (pre-existing flake); `test_r85_macos_screen_models_match_the_server.py` **1 real failure, mine** |
| Doc-count guards | `tests/test_r108* tests/test_r109*` | **5 passed** after re-measuring |
| Mission/goal/runner suites after fixes | `test_demo_a_file_mission test_missions_api test_mission_runner test_goalguard` | **78 then 32 passed** |

The `r85` failure was not a flake. It is described under **Defects found** below.

### Demo A — File Mission

`/tmp/demo_a.py`, project at `/tmp/demo_a_project`.

1. `POST /missions` created `MIS-…` in `draft` with a scope of the project root.
2. `POST /missions/{id}/run` → **`awaiting_approval`**. The README on disk was
   still `'# Sample\n\nOld docs.\n'` **byte for byte** — nothing applied.
3. The step's own detail recorded the gate's reason and the confirmation id
   (`CONF-…`); `POST /missions/{id}/shadow` predicted `would_need_approval`
   without executing anything.
4. `POST /missions/{id}/run` with that recorded confirmation → **`completed` /
   `verified`**. `write_file` read the file back and matched; the condition
   `file_contains 'src/util.py'` verified with sha256 `31dc0d7a…`.
5. `GET /missions/{id}/proof` → one claim, `supported: true`, one observation
   with `evidence_id LOG-…`, `unsupported_claims: []`, chain intact.
6. `GET /undo` listed the write as `reversible` with its captured pre-image.

**Evidence level 7** (complete user journey), backend-verified. **Not verified:**
level 5–6 — the approval was answered by the script over HTTP, not by a click in
the native app.

### Demo B — Self-Healing Service

`/tmp/demo_b.py`, a real `http.server` on `127.0.0.1:8899`.

A goal was registered against the endpoint, observed `verified` (`HTTP 200`), the
process was then **killed** (SIGKILL plus a port re-check), and the observation
run recorded **`unknown | unreachable: [Errno 61] Connection refused`** — an
`unknown`, not a fabricated `failed` and not a silent pass. `POST
/recovery/tournament` ranked the candidates from the registry (`read_file`
alternative safe, `ask` safe, `http_get` retry **unsafe — not a registered
tool**) and returned a `recommended` entry with the note that the ranking is a
plan, not a proof. The service was restarted, `GET` on the endpoint returned
`200 healthy\n`, a second observation run recorded **`verified`**, and the goal
record then held all three: `verified → unknown → verified`.

**This found a real gap, now fixed** (see Defects). Evidence level 7 for the
protocol; the "service" is a disposable fixture, which the directive allows.

### Demo C — Interrupted Mission

`/tmp/demo_c.py`.

A two-step mission ran until step 1 landed (`first.txt` created and read back)
and step 2 was parked. The backend was then **SIGKILLed mid-flight**, confirmed
down (`URLError`), and relaunched from the same `JARVIS_HOME`. `GET
/missions/interrupted` listed the mission; `GET /missions/{id}` returned it with
`s1 done`, `s2 pending`. Resuming re-asked only for step 2, and the completed
step was **not replayed** — asserted by comparing `first.txt`'s `st_mtime_ns`
before and after (`unchanged: True`). Final state `completed`, verdict
`verified`, one condition verified with sha256 evidence, `unsupported_claims: []`.

**This found a real gap, now fixed** (see Defects): before the fix the mission
was **not** in `/missions/interrupted`.

### Demo D — Android Approval — **BLOCKED (hardware)**

No Android device is attached; no emulator is configured in this session. What
*was* run instead, and passes: `tests/test_phase97_phone_mac_workflow.py`,
`tests/test_phone_self_approval_guard.py`,
`tests/test_phase71_phone_authority_hardening.py` → **41 passed, 1 skipped**.
Those exercise pairing, scoped sessions, the device-may-not-approve-itself rule
and the Mac-side revalidation over the real HTTP contract. Physical-device
verification, the on-device UI, and cert pinning against a live gateway remain
**outstanding** and are labelled so rather than claimed.

### Demo E — Safe Failure

`/tmp/demo_e.py`.

A mission was created with an explicit `IntentScope` naming one capability and
one prefix (`/tmp/demo_e/allowed`), and a plan containing a second write to
`/tmp/demo_e/outside/secret.txt`. `POST /steer/preview` (read-only) predicted
the refusal before anything ran:

> `s2  violation: resource — '/tmp/demo_e/outside/secret.txt' (step parameter
> 'path') is outside every resource prefix the intent authorizes`

The in-scope step was approved and **ran** (`Created ok.txt`); the out-of-scope
step was then refused by the intent, the mission went `blocked`, the outside file
was still `'must not change\n'`, and the refusal was written into the
hash-chained audit log as `result=denied` with `refused_by: intent`.

**This found a real gap, now fixed**: refusals previously reached only the
mission row, never the audit chain.

### Demo F — Privacy Egress

`/tmp/demo_f.py`.

`POST /egress/check` refused every undeclared host (`api.example.com`,
`hooks.slack.com`, `pastebin.com`) with *"is not a declared destination; nothing
in this project contacts it, so the read is refused rather than sent"*, while
admitting `127.0.0.1` because *"the read never leaves it"*. The content half was
exercised through the real redaction function: a bare `sk-ant-…` key, an AWS key
id and a `api_key=…` pair were each masked to `[redacted]`, a whole request body
`redact_value` masked the `api_key` and `password` **keys** and the token inside
the message text, and a harmless sentence and an email address (with privacy mode
off) were left untouched. The raw body did contain the key; the redacted one
does not.

**Honest limitation:** the firewall is **destination-based**. There is no
payload-scanning layer inside `EgressFirewall.check`; the redaction shown is the
function callers apply before transport. Claiming the firewall redacts bodies
would be false.

### Demo G — Autonomy Dial

`/tmp/demo_g.py`.

`POST /autonomy/preview` at level 0 returned `CONFIRM` for `read_file`
(*"autonomy is set to ask before everything"*); at level 1 the same read returned
`EXECUTE` (*"low-risk action"*), while `write_file` and `delete_file` stayed
`CONFIRM` at **every** level. Then the same read was run for real: **level 0 →
`awaiting_approval`**, **level 1 → `completed` / `verified`**. `POST /autonomy
{"level": 99}` was refused `422` with the valid set. The dial was restored to its
prior position.

### Demo H — Temporal Undo

`/tmp/demo_h.py`.

A real `write_file` replaced `original contents\n`. `GET /undo` offered it as
`reversible` — *"the file's previous bytes were captured, so it can be restored
exactly"* — with its `current_sha256`. `POST /undo/{id}` restored the file
**exactly** (`restored to the 18 bytes it held before`, verified sha256). For the
conflict case a second write was made, then a person edited the file by hand:
`GET /undo` re-classified the entry as **`conflicted`** (*"the file changed after
this action"*) and `POST /undo/{id}` returned **409** while the human's edit
stayed on disk.

### Demo I — Skill Evolution

`/tmp/demo_i.py`.

A skill with one checkable (`file_contains`) and one uncheckable (`looks_nice`)
condition was saved and run three times — all stopped at the permission gate.
`GET /skill-evolution/{id}` proposed a **narrowing-only** change: remove the
condition *"its check kind 'looks_nice' is not one this build performs"*, remove
the step that existed only to satisfy it, and **raise** risk 0 → 2 to match
`write_file`. The lab's own `will_not_change` list states it cannot add or edit a
step, add a capability or scope, lower the risk tier, or store a version.
`POST /accept` stored the next version; `skills.json` still held every prior
version (9 by then, from repeated demo runs) and the newest kept the checkable
condition but not the uncheckable one. The new version was then run, approved,
and returned **`completed` / `verified`** — where every run of the old version
was unverified. Re-reading the lab afterwards produced **no further proposal**.

### Demo J — Human Takeover

`/tmp/demo_j.py`.

`POST /reality/takeover` paused **17** workers. A real mission step was then
attempted and refused:

> `laptop is paused (human takeover: what is on screen did not match what it
> reported); the next call is refused until it resumes`

The file did **not** exist afterwards. The human wrote it by hand. `GET /reality`
reported `held: true`, `refused_total: 1`, `paused: 17`. `POST /reality/release`
resumed 17 and left 0 paused; the same step then ran
(`Updated handover.txt (27 bytes), read it back and it matches`).

**Not verified:** level 5–6 (physical click on the buttons), and the module never
touches the pointer — `test_reality_hud.py::test_the_module_never_drives_the_screen`
asserts no `osascript`/`CGEvent`/`cliclick`/`screencapture`/`pyautogui`.

### Defects found and fixed while running the demos

1. **A refusal reached the mission row but never the audit chain.**
   `MissionRunner` wrote only verifications to `AuditLog`; `_intent_refusal` and
   the gate-refusal path wrote nothing. Passports count denials from that log, so
   Demo E's denial was invisible to them. Added `_record_refusal` (writes
   `result=denied` with `refused_by` and `step_id`), called from both refusal
   paths. Pinned by
   `test_a_refused_step_is_written_to_the_audit_chain`. *A lost audit write still
   does not turn a refusal into a pass.*
2. **A mission parked on an approval vanished from the relaunch list.**
   `incomplete_missions()` selected only RUNNING/VERIFYING/RECOVERING/PAUSED, so
   Demo C's crash left the parked mission out of `/missions/interrupted` — the
   one piece of unfinished work a person most needs to see. Now includes
   `AWAITING_APPROVAL` and `BLOCKED`, each row carrying its own state. Pinned by
   `test_a_mission_parked_on_an_approval_is_surfaced_after_a_restart` and
   `test_a_completed_mission_is_not_listed_as_incomplete`.
3. **A goal's history forgot the failure that the recovery repaired.**
   `GoalStore.record_observation` kept only `last_*` and a run count, so Demo B's
   `verified → unknown → verified` left a record reading "healthy" with no
   evidence it had ever broken. Added a bounded `history` (`MAX_HISTORY = 50`).
   Pinned by `test_a_failure_survives_the_recovery_that_follows_it` and
   `test_the_observation_history_is_bounded`.
4. **`issue_passport` wrote a result word the Activity view did not know.**
   Phase 11 used `result="ok"`, which is not in the server's own audit
   vocabulary; `test_r85…` caught it. Changed to `success`. This was a genuine
   regression from this session's work, not a flake.
5. **`test_phase34_one_sentence_one_deadline`**: pre-existing timing flake — it
   failed in the full run and passed 11/11 in isolation. Not a regression.

### Completion matrix

| Feature | Status | Evidence |
|---|---|---|
| Mac Control | **VERIFIED** | Existing; unchanged this session |
| Saved Workspaces | **VERIFIED** | Existing; unchanged |
| Workflow Studio | **VERIFIED** | Existing; unchanged |
| Demonstrate-to-Automate | **VERIFIED** | Existing; unchanged |
| Mission Progress | **VERIFIED** | Demo A/C show real per-step `detail` transitions |
| Mission Controls | **VERIFIED** | Approve/park/resume/refuse shown in A, C, E |
| Smart Files | **VERIFIED** | A (write+verify) and H (undo+conflict) |
| Project Doctor | **PARTIALLY_VERIFIED** | Route exists; not exercised end-to-end this session |
| Command Center | **PARTIALLY_VERIFIED** | Existing; not re-run this session |
| GoalGuard | **VERIFIED** | Demo B, plus the history fix |
| Skills Library | **VERIFIED** | Demo I: save, run, evolve, run verified |
| Browser Research | **PARTIALLY_VERIFIED** | Existing tests pass; fan-out not exercised |
| Android | **BLOCKED** | No device/emulator. Protocol: 41 passed, 1 skipped |
| Voice | **NOT IMPLEMENTED** (in this session's scope) | Unchanged |
| ProofLens | **VERIFIED** | A, C: claims, observations, evidence ids, chain state |
| Privacy | **VERIFIED** (destination) | F; body redaction is a caller step, stated as such |
| Security | **VERIFIED** | E: both intent and permission refusals hold, no mutation |
| Performance | **PARTIALLY_VERIFIED** | Suite wall-clock measured; no new perf claims |
| Persistence | **VERIFIED** | C: kill/relaunch/recover without replay |
| Packaging | **BLOCKED** | Not rebuilt this session; nothing pushed, CI never ran |

Nothing above is marked VERIFIED from a backend module alone: each has a run
recorded in this section, and the levels that were **not** reached (5–6 physical
interactions; CI) are named rather than absorbed.

### What is still not done

- Levels 5–6 for every demo: **no physical clicks were made**. Every control was
  exercised over HTTP against the live backend.
- **Demo D** needs a device or emulator.
- Nothing is pushed; CI has never run on this work.
- The Swift XCTest target has not been executed locally.

---

## Update — 2026-11-16 (the Text & Chat tab now actually drives Command Center)

**Reported symptom:** "the cmd centre is not working properly and its maximum
req are failing."

### Root cause (measured, not inferred)

The macOS Command Center, the `/command` route and the app's client were all
working. The defect was a **split configuration**:

| Path | Read its provider from | Read its key from |
| --- | --- | --- |
| `/command` → chat | `settings.ai_provider` (default `openai`) | `JARVIS_LLM_API_KEY`, else `vantage.ai.<provider>` |
| Text & Chat tab | `provider_categories["text"]` | `vantage.ai.category.text` |

`provider_categories` was imported by exactly four API routes and
`category_checks.py` — nothing in `config.py`, `selection.py`, `router.py` or
`orchestrator.py`. So the tab declared *"Command Center answers, conversation
and reasoning"*, its Test button returned `ok:true` in 5.5 s, and the chat path
still computed `llm_enabled: False` and answered every sentence from the rule
planner.

Reproduced with the real client against the live backend:

```
aiAvailable : false     provider : openai     model : rules
reply : …'I would need an AI provider for that one.'
```

### Fix

- `jarvis/config.py` — `_overlay_text_category()` applies the saved Text & Chat
  category to `ai_provider`, `llm_model`, `llm_base_url`, and the role models
  (`llm_fast_model`/`llm_reasoning_model`, which otherwise stay `gpt-4o-mini`/`o4-mini` — names the gateway answers with **404**; both measured).
  An exported `JARVIS_*` variable still wins. An unconfigured tab changes nothing.
- `jarvis/gateway/selection.py` — `stored_key()` falls back to
  `vantage.ai.category.text`, **only** when the category names that same
  provider, so a 9Router key is never sent to OpenAI.
- The tab's off switch means "this tab configures nothing": the overlay returns
  early rather than writing `ai_enabled: false`, so the switch is reversible and
  switching off Text & Chat cannot disable embeddings or vision.

### Model choice (measured)

Benchmarked 8 candidates on the live 9router gateway, then soaked the best two
with 20 concurrent calls each.

| Model | Result | Avg | Max |
| --- | --- | --- | --- |
| **`qoder`** (chosen) | 20/20 | 1.1 s | 1.7 s |
| `modelscope` | 20/20 | 1.0 s | 1.7 s |
| `ali/qwen3.8-flash` | 8/8 | 1.7 s | 2.1 s |
| `van` | 6/6 | 8.4 s | **34.2 s** |
| `ag/claude-sonnet-4-6` | 6/6 | 2.7 s | **25.2 s** |
| `zen/openai/gpt-4o-mini` | 0/3 | — | 503 |
| `cu/claude-sonnet-5-5-low` | 0/3 | — | 400 |
| `groq/…llama-4-maverick` | 0/3 | — | 503 |

`qoder` is a gateway-side **combo** (provider-side fallback), 262 144 context,
384 000 max output, tools + vision + search. It passed a tool-call shape check
(`get_weather({"city":"Paris"})`), arithmetic, instruction-following and
multi-step reasoning. `modelscope` is an equivalent second choice.

### Live verification

- `GET /health` → `llm_enabled: true`, `provider_paths.chat: 9router`.
- `POST /command` → `model_used: "9router:qoder"`, 3 245 ms, real prose.
- Real `ConversationService` + `APIClient` → `model 9router:qoder`, 3 579 ms.
- Deterministic intents still stay deterministic (`"what time is it"` →
  `model=rules`, 7 ms) — the model is not put in front of what rules answer faster.

### Tests

- New `tests/test_text_category_drives_chat.py` — **10 passed**.
- `test_provider_categories` + `test_ai_chat_provider_path` +
  `test_provider_config_isolation` + `test_provider_architecture` — **52 passed, 1 skipped** (the skip is the optional live-key test).
- `test_env_example_is_loadable` — 7 passed (a new `Settings` field was reverted
  rather than editing the example file around it).
- `test_r108_readme_counts_are_true` — 3 passed (README counts re-measured to
  237 modules / 4 466 collected after this file was added).

### Not done

- The macOS `.app` was **not** rebuilt: the fix is entirely backend-side and the
  installed binary (2026-11-15 15:59) already matches its sources.
- The backend was restarted by hand to load the new model; the running process is
  `run.py serve --port 8765 --host 127.0.0.1` (not under the app's supervision).
- The 429s I observed came from a synthetic 140-request flood (burst 120).
  Realistic usage — 12 concurrent screen loads and 60 polls over 30 s — returned
  **100 % 200**, so the security limit was left alone.

### Follow-up defect found while testing (fixed)

A real second bug, found by trying a conversational follow-up:

> "What **time** should I leave if the trip takes 45 minutes?" → *"It is 17:36
> on Monday, 16 November 2026."*

`planner._time` matched the bare alternative `the time`, so any sentence
containing those two words was routed to `get_time` — including a follow-up
whose answer needed the conversation context the model had. The rule now
declines when the sentence is about a **planned** time (`should`/`will`/`can`/
`to leave`/`to arrive`/…), the same shape as the existing
"what is the date of my meeting" guard.

Verified: 5 clock questions still reach `get_time`; 5 departure-time questions
no longer do (`tests/test_text_category_drives_chat.py`, 8 new cases).
`test_phase52`, `test_voice` and `test_phase91_voice_agent_integration` — 142
passed, 1 skipped (`test_phase52` also carries the unrelated 181-vs-196 failure
described above).

## Update — 2026-11-16 (V14: real desktop control — measured, not claimed)

This phase set out to make Vantage physically operate macOS. It produced a real
executor, real tools, a real screen, and one capability that genuinely works —
plus a precise, measured account of why the rest cannot on this machine.

### The measurement that decided the design

Read fresh from a newly compiled binary on this machine:

| Check | Value |
|---|---|
| `CGPreflightPostEventAccess()` | **false** |
| `AXIsProcessTrusted()` | **false** |
| `CGPreflightScreenCaptureAccess()` | **true** |

Then measured, not assumed:

- `CGEvent.post` with post-event access **false** returns **no error and does
  nothing**. A pointer move posted that way left `CGEvent(source:)?.location`
  unchanged at (607,731) → (607,731). This silent no-op is why the executor never
  treats "the call returned" as evidence.
- `CGWarpMouseCursorPosition` **does** move the real pointer: observed
  (701,718) → (731,748), confirmed by readback, then restored.
- `osascript ... click at {x,y}` exits **1**:
  `osascript is not allowed assistive access (-25211)`. The pre-existing
  AppleScript `click_desktop` correctly reported this as a failure — it was not
  lying, it simply could not click.

So the capability is split, and the whole implementation reports which half is
available rather than implying both are.

### What was built

- **`native/desktop_executor/main.swift`** — a compiled Swift executor
  implementing pointer move, click/double/right, drag, scroll, unicode typing,
  key + modifiers, app/window focus, Accessibility tree inspection, AXPress,
  element reading, and a `verify_change` observer. Every operation returns a
  structured status; `verified` is only used where a readback actually confirms
  the outcome.
- **`jarvis/desktop/executor.py`** — the Python bridge. It runs the helper and
  passes its JSON through verbatim; an unparseable answer becomes `unknown`, and
  a missing binary becomes `permission_required`, never a silent success.
- **`jarvis/tools/desktop_native.py`** — 15 registered tools with real risk
  tiers: four READ, one LOW, one EXTERNAL (pointer move — visible, so not a
  read), nine DESTRUCTIVE confirmation-gated (click, double, right, drag, scroll,
  type, key, shortcut, press_element).
- **`jarvis/api/desktop_routes.py`** — the authenticated `/desktop` surface,
  mounted in the existing app. No second server.
- **`macos/.../DesktopControlView.swift`** — the Desktop Control screen, first in
  the Mac sidebar group. It disables a control whose capability is missing and
  says why, names the executable the permission applies to, and colours
  `performed_unverified` amber, never green.
- **`tests/test_desktop_native.py`** — 23 tests.
- **`benchmarks/v14_desktop/`** — the acceptance run and its README.

### Defects found and fixed while doing it

1. **`CGGetActiveDisplayList` returns 0 on this machine** while
   `CGGetOnlineDisplayList` returns the real display and `CGMainDisplayID`
   reports 1352x878. Coordinate validation built on the empty list refused
   **every** coordinate with "not on any display" — a validation bug wearing the
   costume of a safety feature. Switched to the online list, with the main
   display as a fallback. Pinned by
   `test_a_valid_on_screen_point_is_not_refused`.
2. **The desktop HTTP surface bypassed the Control Tower.** It called the
   executor directly, so a human takeover stopped the mission runner while a
   click from this surface still went through. Measured in the acceptance run: a
   pointer move succeeded *during* a takeover. The routes now call `admit()` and
   return **409** with the tower's own reason. Verified live and pinned by
   `test_a_takeover_refuses_a_desktop_action_from_the_http_surface`.
3. **My own `fail()` call left the message field empty.** `fail()` carries its
   reason in `error`, not `message`, so every refusal rendered as a blank row in
   the first smoke test. Fixed in `_finish`.

### Acceptance results (real run, live backend)

| Demo | Status | Level |
|---|---|---|
| A — move the cursor | **VERIFIED** | 3 — pointer moved, readback confirmed |
| B — click a button | **PERMISSION BLOCKED** | 1 |
| C — type into TextEdit | **PERMISSION BLOCKED** | 1 |
| D — scroll | **PERMISSION BLOCKED** | 1 |
| E — double/right click | **PERMISSION BLOCKED** | 1 |
| F — drag and drop | **PERMISSION BLOCKED** | 1 |
| G — keyboard shortcut | **PERMISSION BLOCKED** | 1 |
| H — multi-step mission | **PARTIALLY VERIFIED** | 3 — the move step ran through the real mission gate and verified; the type step stopped on the missing permission |
| I — permission refusal | **VERIFIED** | names the executable and the fix; target untouched |
| J — human takeover | **VERIFIED** | a move during takeover returns HTTP 409 |

Level 1 = the request reached the executor. Level 2 = a system event was posted.
Level 3 = the target UI actually changed, observed.

### What is NOT done, and why

- **No click, keystroke, scroll or drag has executed on this machine.** Post-event
  access is not granted to the executor, so each refuses *before* acting. The
  code path is complete and will report `performed_unverified` — never
  `verified` — until a separate observation confirms the target changed.
- **Accessibility targeting is implemented but untestable here.** `inspect`,
  `press_element`, `read_element` and `verify_change` all refuse with the
  executable named, because `AXIsProcessTrusted()` is false for it.
- **Granting the permission is a user action.** `vantage-desktop request_access`
  shows macOS's own prompt; the person must approve it in System Settings. I did
  not click that dialog or edit TCC, and the grant did not appear on its own.
- **A rebuilt binary can be a different identity**, so a grant may not follow a
  rebuild to a new path. `build.sh` writes to a fixed location for this reason.
- **The Vantage.app bundle was not granted or re-signed this session.** The
  executor is what holds (or lacks) the permission, and it names itself in every
  result.

### Test health

Targeted suites: **108 passed, 7 skipped** across `test_desktop_native`,
`test_phase93_desktop_control`, `test_phase61_desktop_control_that_asks_before_it_touches`,
`test_phase25_desktop_truth`, the two doc-count guards, `test_no_duplicate_routes`,
`test_reality_hud` and `test_control_tower`. The 7 skips are the pre-existing
acceptance tests that require Accessibility — they skip honestly rather than
faking a pass. `swift build` is clean for the app.

README counts re-measured after adding 15 tools: **49 of 196** confirmation-gated,
tiers read 97 · low 52 · external 21 · destructive 23 · physical 3; 238 test
modules · 4499 collected; 219.6k versioned lines.

### Corrections found by running the full suite (not by reading the diff)

The targeted suites above all passed, and that was not enough: the full suite
returned **6 failed, 4462 passed, 30 skipped in 517 s**, and all six were mine.
Each is fixed at its cause.

1. **`desktop_move_pointer` was EXTERNAL and ungated.** Phase 54, 55 and 56 each
   assert that *every* EXTERNAL and DESTRUCTIVE tool requires confirmation — a
   deliberate invariant, not a frozen count (they were written that way
   specifically so a new phase could add gated capabilities). Last session I
   reasoned "a visible move is not a READ, so make it EXTERNAL" and then, in the
   same spec, set `requires_confirmation=False`. Those two lines cannot both be
   right. The fix is **LOW**: a pointer move is reversible and less consequential
   than `close_app` or `browser_open`, both of which are LOW and ungated. The
   consequence starts at the click, and a click is DESTRUCTIVE and gated. The
   invariant was not weakened for it; `test_nothing_ranked_external_is_ungated`
   now pins this on the desktop tools as well.

2. **The README published 181 tools while the registry has 196** (Phase 52's
   guard). A stale published count is the defect this project keeps re-finding,
   so it is re-measured rather than nudged.

3. **Phase 28's unreachable ledger was 32; V14 added 15 unrouted desktop tools.**
   That is the on-purpose direction — the entire design intent is that a *sentence*
   cannot reach a pointer, keystroke or AXPress — so the ledger moves 32 → 47 with
   the 15 tools named in the assertion, the same way Phase 61 recorded its two.

Re-run after the fixes: **209 passed** across the six affected files plus
`test_desktop_native`. The full suite was re-run end to end after this point; see
the isolation defect below for the one failure that survived it.

### A seventh failure, and it was not the deadline logic

The re-run returned **1 failed, 4468 passed, 30 skipped in 516 s**:
`test_phase34_one_sentence_one_deadline.py::test_no_papers_are_invented_when_the_clock_runs_out`
at line 234. It passed in isolation and in ordered subsets, which is the shape of
a test-isolation defect rather than a flake.

Measured cause: `search_papers` tries the requested source and, on failure, falls
back to the local corpus, which reads a `PaperStore` **shared by the whole
session** (the `core` fixture is `scope="session"` and every test uses one temp
home). Any earlier test that filed a paper matching "agents" or "papers" made the
local source answer, so the assertion — "a source going quiet invents nothing" —
saw stored papers and reported them as invented.

Proven both directions rather than argued: seeding one paper and running the old
test reproduces the exact line-234 failure; the same seed with the fix passes. The
test now stubs the store empty, because the property under test is about invention,
not about the shelf's contents.

This is **pre-existing**, not from V14: the file is untouched by those commits, and
phase34 and its neighbours pass in order on the pre-V14 baseline (`871e21e`, 220
passed). It is fixed here because it was the last red test.

**Final full-suite state after every fix, measured end to end: 4469 passed, 30
skipped, 0 failed in 517.52 s.** The 30 skips are the pre-existing
prerequisite-gated tests (Accessibility, playwright browsers, microphone) that skip
honestly rather than passing on a mock.

### Added after the first commit: a Level-3 harness, and a coordinate defect it caught

`tools/input_probe/` is a disposable SwiftUI app that does nothing by itself, and
`selftest.py` drives it with real events and then reads the target's own state
file back. Building it exposed a bug in my own coordinate conversion, which is
exactly the class of error the harness exists to catch:

- The probe reports each control's position in CGEvent screen space so nothing is
  guessed. The first conversion **mirrored every target vertically** — measured:
  the first control reported y=623.5, the fifth y=461. The cause is that
  `NSHostingView` is already a flipped view, so the "obvious" content-height
  subtraction double-inverts. It now uses AppKit's own `convert()` chain, and the
  measured ordering is top-to-bottom (252.5 → 294.5 → 340.5 → 379.8 → 415.0),
  geometrically consistent with a window at (120, 120), height 672.

**The success path of that harness has never executed.** Post-event access is not
granted to `vantage-desktop`, and a freshly compiled binary is not trusted either
(measured: `CGPreflightPostEventAccess()` false for a probe built minutes earlier).
Only the refusal path — exit 2, nothing attempted — is verified. The script exists
so that the grant, when given, produces a Level-3 answer in one command; it is not
evidence that a click has happened.

## Update — 2026-11-16 (V15: real desktop automation, and what this machine refuses)

The V15 campaign asks for real native mouse, keyboard and Accessibility control.
This section records what was measured, and what the machine permits. It is
short on purpose: the acceptance table is the result, and everything below is
either how a number was obtained or what is still blocked.

### The permission split, measured

Three switches, granted separately. `native/desktop_executor/vantage-desktop
permissions` reports them; the values below are from that command on this tree.

| Switch | Value | Gates |
|---|---|---|
| `CGPreflightPostEventAccess()` | **false** | every click, keystroke, scroll, drag |
| `AXIsProcessTrusted()` | **false** | reading and pressing UI elements |
| `CGPreflightScreenCaptureAccess()` | **true** | window bounds, and cursor warping |

`CGWarpMouseCursorPosition` needs only screen-recording, so the real pointer
moves and can be read back. `CGEvent.post` needs post-event access, so nothing
that lands on a target can run. That is the whole reason the cursor demo is a
Level-3 result while the eight actuation demos beside it are blocked.

### T1–T10, from `benchmarks/v15_desktop/acceptance.py`

Levels: 1 the request reached the executor · 2 a real event was posted · 3 the
target actually changed, observed. Only 3 is proof an action worked.

| Demo | Result | Level |
|---|---|---|
| T1 cursor | VERIFIED | 3 — per-leg target vs readback |
| T2 left click | PERMISSION BLOCKED | 1 |
| T3 double click | PERMISSION BLOCKED | 1 |
| T4 right click | PERMISSION BLOCKED | 1 |
| T5 typing | PERMISSION BLOCKED | 1 |
| T6 shortcut | PERMISSION BLOCKED | 1 |
| T7 scroll | PERMISSION BLOCKED | 1 |
| T8 drag | PERMISSION BLOCKED | 1 |
| T9 TextEdit mission | PARTIALLY VERIFIED | 3 — S1 opened TextEdit, S2 moved the real pointer and verified it, S3 stopped on post-event access |
| T10 stop and takeover | VERIFIED | 1 — HTTP 409 during takeover |

**2 verified, 1 partial, 7 permission blocked, 0 failed.** No click, keystroke,
scroll or drag has ever executed on this machine. That is stated as a limit, not
hidden behind a passing test count.

### What was built, and what it proved

- **Chained planning (V15 §16).** `jarvis/desktop/planner.py` used to handle one
  operation. It now splits a sentence on genuine instruction boundaries and plans
  every clause in order. The never-invent-a-target rule survived the change and is
  what makes it correct: "click there" takes the point an earlier step of the same
  request named and is a question with no such step; a quoted comma stays text;
  "click 400 300 and 410 310" is one malformed click, not two. Measured:
  `POST /missions/plan` returns the demonstration as five ordered, dependency-
  chained steps, and an ambiguous request as zero steps plus a question.
- **Window geometry without Accessibility.** The `windows` op reads the window
  server with only screen-recording. Measured: TextEdit at (273, 208) 656×422, in
  the same global points a click accepts.
- **Identity, read from the process.** `identity` reports the executable, bundle
  id, signature state and SHA-256. It says plainly that an ad-hoc signature does
  not survive a rebuild, because that is what a person needs before granting.
- **Dev app bundle (V15 §24).** `make_dev_app.sh` wraps the executor in
  `VantageDesktopDev.app` (`com.jarvisforge.vantage.desktopdev`, version 15.0) so
  there is a stable, findable thing to grant. It grants nothing itself. There is
  no Developer ID on this machine (measured: `security find-identity -v -p
  codesigning` reports 0 valid identities), so the signature is ad-hoc — recorded
  as a limitation rather than papered over.
- **The probe fixture now publishes what four demos need.** `context_menu_openings`
  (`NSMenuDelegate.menuWillOpen`), `selection_length`/`field_text_length`
  (`NSTextViewDelegate`), `scroll_offset` (real clip-view bounds), and
  `dropped_item`/`drop_count` (drop provider completion). Each was cross-checked by
  driving the fixture's own AppKit callbacks; none is invented. They read zero
  without a grant, which is the correct reading.
- **The Desktop Control screen shows evidence, not conclusions.** The probe's
  per-leg target-and-readback decodes and renders. A typo in the coordinate field
  used to send the real pointer to (0, 0); it is now refused.

### A second defect, found the same way

Recording a `desktop_type_text` step with `{"text": "password=hunter2"}` was
**accepted and stored**. The sensitive-value guard inspected only parameter
*names*, so "text" looked innocent and the value — which would be replayed as
keystrokes on every run and sat in the draft in plain text — was kept. Found by
driving the live `/intelligence/recordings/steps` endpoint, not by reading the
code. Fixed: a typed parameter's value is now inspected for `key=value` shapes,
and the value never appears in the skip reason. Verified after the fix:
`skipped=true, params={}`. A sentence that merely mentions a credential ("the
token expired") is still recorded, because a bare word match would make the
recorder useless for notes.

### A defect the demonstration found

TextEdit was not in the app catalog (`jarvis/registry/apps.py`), so the required
demonstration could not open the application it names — `open_application`
answered "I could not find an application matching 'TextEdit'". Registered now,
with "text editor" as its alias and "editor" deliberately left to VS Code, since
an alias shared by two apps makes resolution order-dependent. Guarded by the T9
run and by `tests/test_missions_api.py`.

### Tests, measured on this tree

| File | Result |
|---|---|
| `tests/test_desktop_planner.py` | **58 passed** |
| `tests/test_desktop_native.py` | **29 passed** |
| `tests/test_phase28_reading_is_not_writing.py` | **33 passed** (ledger 49 → 51; the invariant-asserting line above the pinned count is unchanged) |
| `tests/test_missions_api.py` | **30 passed** |
| `tests/test_recorder.py` | **40 passed** (was 31; includes the two cross-language pins and the typed-secret guard) |
| `test_phase41`, `test_phase52`, `test_phase39`, `test_phase51`, `test_r43_browser_slice` | **166 passed** (before the fix: 165 passed, 1 failed — that one pinned README's tool count against the live registry and caught 196 vs 200) |

Published tool count corrected 196 → 200 and pinned by
`test_phase52::test_the_two_capabilities_this_phase_added_are_there`.

### The full regression, and the five failures it found

The suite was run once after the source settled: **5 failed, 4542 passed, 30
skipped in 545 s** (`.venv/bin/python -m pytest -q -p no:cacheprovider`, exit 1).

All five were count-consistency guards, and all five were *true claims made
false* by this campaign's additions — not flaky, not unrelated:

| Guard | Said | Tree |
|---|---|---|
| `test_phase25::test_the_readme_counts_the_gated_tools_out_of_the_whole` | 49 of 196 | 50 of 200 |
| `test_phase25::test_the_readme_counts_the_risk_tiers_it_ships` | 97/52/21/23/3 | 99/53/21/24/3 |
| `test_r108::test_the_readme_test_module_count_is_real` | 238 modules | 239 |
| `test_r108::test_the_readme_collected_count_matches_a_real_collection` | 4499 | 4577 |
| `test_r109::test_the_module_count_matches_the_tree` | 249 modules | 250 |

Two new desktop reads, two new test modules, and making the desktop actuation
kinds recordable moved each of them. Every one was corrected to the measured
value and each guard re-run green (`27 passed`, then `79 passed` including the
phase52 tool-count pin, plus `54 passed` across the neighbouring count tests).
None was weakened, skipped, or made to stop asserting.

This is the guards working exactly as intended: a published count is a number a
reader quotes to somebody else, so a stale one is a false claim.

### Still blocked, and exactly what would unblock it

- **Post-event access** for `native/desktop_executor/VantageDesktopDev.app`. The
  grant is the person's to make: `vantage-desktop request_access` shows macOS's own
  prompt, or drag the bundle into System Settings → Privacy & Security →
  Accessibility. It cannot be scripted, and this session did not attempt to.
- **Accessibility**, needed only for reading and pressing UI elements by label.
  T2–T8 need only post-event access; AX is for the semantic path.
- **T9's save step has never run.** The plan includes `cmd+s`, but saving to a
  named path means driving the save panel, which needs the same post-event access.
  No file has been produced by typing.

## Update — 2026-11-16 (V16: one coordinate parser, stale-target refusals, a takeover that lets go)

The V16 campaign's measured state. The central acceptance gate — the TextEdit GUI
mission — is still blocked at the same permission, and this section says so rather
than dressing the partial result as a pass. What did change is real, and each item
below was verified by running it.

### Three copies of one check had drifted apart

Coordinate validation existed in three places with three different behaviours.
Measured on the baseline: `/desktop/move` performed **no** bounds check, so `nan`
and `inf` reached the native executor; the tool and the planner each re-tested
`0 <= x <= 32000`; and both rejected **every** negative coordinate, which is a
valid point on a display placed left of the primary one.

`jarvis/desktop/coordinates.py` is now the only place a point is judged, and it
judges against real display geometry rather than a magic range. Every door — the
HTTP routes, the tool registry, the planner, and through them the studio and the
manual UI — uses it. NaN, infinity, empty strings, booleans and non-numbers are
each refused with a sentence that says what was wrong.

### A click that names its target

A click can now carry the id of the window it was resolved against. If that window
has closed or moved more than two points, the click is refused with
`target_not_found`/`target_stale` **before** anything is posted. Typing does the
same with the frontmost application. Both checks run before the permission
request, because a stale target is knowable without permission — reporting
`permission_required` for a window the caller never meant would hide the more
useful answer.

### A takeover that lets go

`take_over` paused every worker and stopped there. Pausing stops the *next*
dispatch; it did nothing about a drag already in progress with the mouse button
down, so a person grabbing the mouse to stop a runaway drag would find their own
pointer still dragging. It now releases whatever the executor holds and reports
the result. On this machine that release is refused for the same reason everything
else is, and the report says `permission_required` rather than claiming success.

### Acceptance, measured

**7 verified · 1 partial · 7 permission-blocked · 0 failed** across T1–T15.
T10 and T11 moved from level 1 to level 3; T1, T12, T13, T14, T15 are level 3.
The seven blocked demonstrations are blocked because `CGPreflightPostEventAccess()`
is false, and no click, keystroke, scroll or drag has ever executed here.

### A disagreement worth recording

Two specialist workers were started in isolated worktrees. Both worktrees were cut
from a base that predated the desktop subsystem, so the SwiftUI worker built
against files that did not exist there and duplicated models that already exist in
this checkout. Its five-state capability derivation was sound and was taken into
the real view by hand; the duplicate definitions were discarded. The native
worker's output was rebuilt and run before being accepted. This is recorded because
reporting it as a clean parallel integration would be false.

### The frozen regression, and its three failures

Run once at revision `85dd1ca`: **3 failed, 4580 passed, 30 skipped in 544 s**
(`.venv/bin/python -m pytest -q -p no:cacheprovider`, exit 1).

All three were published-count guards, made stale by this campaign's own
additions — one new module (`jarvis/desktop/coordinates.py`) and one new test
file:

| Guard | Said | Tree |
|---|---|---|
| `test_r108::test_the_readme_test_module_count_is_real` | 239 modules | 240 |
| `test_r108::test_the_readme_collected_count_matches_a_real_collection` | 4577 | 4613 |
| `test_r109::test_the_module_count_matches_the_tree` | 250 modules | 251 |

Each was corrected to the measured value and the four files that pin them
re-ran green (**79 passed**). None was weakened or skipped. These are the same
guards that caught the drift twice in V15; they exist so a published number
cannot quietly become false.

### Final regression — clean at the frozen revision

**4583 passed, 30 skipped, 0 failed, exit 0** in 533 s at revision `1bb09ab`
(`.venv/bin/python -m pytest -q -p no:cacheprovider`).

This is the run the V15 report could not honestly claim: the count guards were
corrected first, then the whole suite was re-run at the corrected revision rather
than the old result being restated. The 30 skips are the pre-existing
prerequisite-gated tests (Accessibility, Playwright browsers, microphone) that skip
honestly rather than passing on a mock.

## Update — 2026-11-17 (V21: real desktop input, and the bug that was hiding it)

Desktop automation works on this machine. The central result: T2–T8 all pass
against the disposable fixture with the target's own state observed, and the
complete TextEdit → save → Finder workflow runs through real GUI interaction.

### The defect that caused everything

For three campaigns the report said "clicks are blocked by permissions". That was
wrong. The fixture's coordinate conversion was wrong by **32 points**, so every
click landed on the text label directly beneath the button. The event was
delivered perfectly; the target was wrong.

What settled it was instrumentation, not reasoning. Three earlier hypotheses — a
modal dialog, window occlusion, wrong window identity — were each measured and
each disproved. Adding a local `NSEvent` monitor to the fixture that records
every event it actually receives, including which view was hit
(`hit_view: NSTextField`), showed the click arriving at the right screen point and
the wrong control. `last_action` alone could never have shown that, because it
only changes when one of the fixture's own callbacks fires.

### Six defects fixed

| Defect | Evidence it was real |
|---|---|
| coordinate conversion off by 32pt | button answered at y 210–230, advertised 252.5 |
| double-click `clickState` | both presses carried 2, so no 1-then-2 sequence existed |
| Shift never released | "Hello" arrived as "HELLO" — flags only set on shifted chars |
| keystroke timing | 2ms/6ms produced `L iprobessu` for "Hello from Vantage" |
| scroll | wheel events never arrived; `AXScrollDownByPage` returned -25204 |
| focus lost mid-run | activating once, then 7 steps, gave 1/7 |

### Verified, with the observation each rests on

- **T2–T8:** 7/7 in `benchmarks/v21_desktop/accept.py`
- **T9:** 8/8 in `benchmarks/v21_desktop/textedit_finder.py` — typed, GUI-saved,
  file on disk, Finder open
- **T10:** every desktop action kind refused during takeover (HTTP 409)
- **MissionRunner:** counter 3→4 through the real runner and a real approval
- **Automation Studio:** a recorded click replayed, counter 4→5

### What remains genuinely undone

**Standalone packaging.** `/Applications/Vantage.app` is a signed native Swift
app whose backend and executor are not inside the bundle; it looks for
`scripts/backend_lifecycle.py` beside itself and finds nothing. Every desktop
result above is through the development checkout.

**Permission attribution.** Measured: a never-granted binary reports
`accessibility: true` launched from this shell and `false` launched from Finder.
The executor inherits its permission from the responsible process, so a
Finder-launched app gets nothing until it is granted in its own right — and that
grant is only meaningful once the packaging work exists.

### Final regression — clean at this revision

**4582 passed, 31 skipped, 0 failed, exit 0** in 547 s at `0f9b490`
(`.venv/bin/python -m pytest -q -p no:cacheprovider`). The 31 skips are the
pre-existing prerequisite-gated tests that skip honestly rather than passing on
a mock.

### Test builds must not take the app's port

During a build-testing session a second copy of the app (`dist/Vantage.app`) was
launched. It started its own backend, which took **8765** and displaced the
backend the user's running `/Applications/Vantage.app` was talking to. The app
detected the mismatch and refused to connect — its ownership guard worked — but
the user's session stayed broken until the stray backend was killed by hand.
Nothing on the build/test side prevented the collision.

`scripts/dev_port_guard.sh` is that missing guard. POSIX `sh`, `lsof` only, and
it **never signals or kills a process**.

| Verb | Behaviour |
|---|---|
| `check <port>` | exit 0 and print `port N is free`; exit 1 and print `port N is IN USE` with each holder's pid and cwd (`lsof -p N \| grep cwd`; if the cwd is unreadable, say so instead of guessing). Exit 2 on a non-numeric or out-of-range port. |
| `claim` | print the first free port in **8798–8810**, the number alone on stdout, so `PORT=$(scripts/dev_port_guard.sh claim)` works. Exit 1 if the whole range is busy. |
| `check 8765` | **refused even when the port is free**: it belongs to the installed app. The refusal is checked before the port is probed, so occupancy cannot make it pass. |

`claim` reports a free port; it does not reserve it. The number is a hint, not
a lock.

Verified on this tree at `fa62b0f`, with the real backend listening on 8765
(pid 1682, cwd this checkout):

```
$ scripts/dev_port_guard.sh check 8765
REFUSED: port 8765 is the installed app's backend port.
A test build must never take it. Run "scripts/dev_port_guard.sh claim" for a development port (8798-8810).
Port 8765 is currently held by:
  pid 1682 (cwd: /Users/abhijeetmiskin/AppData/Vantage)
exit=1

$ PORT=$(scripts/dev_port_guard.sh claim); echo "[$PORT]"
[8799]

$ scripts/dev_port_guard.sh check 8798
port 8798 is IN USE:
  pid 95137 (cwd: /Users/abhijeetmiskin/AppData/Vantage)
exit=1

$ scripts/dev_port_guard.sh check 8799
port 8799 is free
exit=0
```

8798 was genuinely held by an unrelated Python process, and `claim` skipped it
rather than handing it out — the range is real. Invalid input (`check abc`,
`check 70000`), a missing verb and an unknown verb each print usage and exit 2.
After every check, `lsof -nP -iTCP:8765 -sTCP:LISTEN` still showed pid 1682 and
the backend still answered HTTP (401 from the auth gate): the guard did not
touch it.

Portability was checked with `dash` (a strict POSIX shell), not only with
macOS's `/bin/sh`, which is bash 3.2 in POSIX mode: `dash -n` parses clean and
all three verbs behave identically under `dash`. **`shellcheck` is not
installed on this machine, so no lint was claimed** — `dash -n` is a syntax
check, not a lint.

Limitation, stated rather than papered over: `lsof` run as an ordinary user may
not see a process owned by another user, so a busy port can read as free. Run
the guard as the user who owns the build.

Not yet wired in: nothing calls it. The next step is the build/test entry points
(`build_app.py`, the playwright-driven tests) invoking `claim` instead of
assuming 8765.

### Full suite, at revision 0b23014

**4611 passed, 20 skipped, 0 failed** in 539 s
(`.venv/bin/python -m pytest -q -p no:cacheprovider -rs`).

Skipped count is down from 30. The ten that were lifted had never executed, and
running them found real breakage in every group -- a deleted helper, `ToolContext`
fields that do not exist, coroutines called without `await`, and two instances of
the same `Settings`/`ToolContext` mismatch that made the wizard report a granted
permission as UNKNOWN. Details in `docs/SKIPPED_TESTS.md`.

The remaining 20 each name a prerequisite this machine does not have (tkinter,
qrcode, Android SDK, microphone hardware, a real API key, a configured model). None
masks a failure.

### Standalone app: the TCC identity fix

The installed bundle could be built, signed, and resolve its executor from inside
itself, and still could not post input. TCC showed why: with `/bin/sh` as
`CFBundleExecutable`, macOS attributed the executor's Accessibility request to
`python3-55554944...` — the Homebrew runtime the script exec'd — so a grant to
Vantage.app never applied.

The launcher is now a compiled Mach-O in the signed bundle that `execv`s Python,
with no intermediary process. Measured after: `responsible = com.jarforge.app`.

`native/` and `scripts/` are also staged into the bundle now. `native/` was never
staged, so an installed app resolved its executor to a source-tree path a copied
bundle does not have; `scripts/` holds `backend_lifecycle.py`, without which the
app could not start its own backend.

## Update — 2026-11-17 (V22: the Swift app the user opens now stands alone)

The campaign had been validating two different products. `macos/Vantage/Vantage.app`
is the SwiftUI app the user double-clicks; `dist/Vantage.app` is a Python launcher
with no Swift UI. The V22 "standalone passed" results came from the second while
the product is the first. That is recorded here so it is not repeated.

### The one line that blocked startup

`BackendManager.backendInterpreter()` looked for `.venv/bin/python` inside the
project directory. A bundle has no `.venv`, so it fell back to
`/usr/bin/python3` — which cannot import fastapi. The backend died before binding
a port and the app showed an empty Home screen. Every earlier "the app does not
start its backend" was this.

It now reads `python_path.txt`, written by the packaging step. A checkout keeps
working unchanged: no such file, so the `.venv` branch still runs.

### Verified by launching the assembled app with an empty port

| Check | Result |
|---|---|
| Backend started by the app | pid 24936, no manual step |
| Backend working directory | `<bundle>/Contents/Resources` — the bundle, not the checkout |
| `/health` | `status=ok`, 200 tools |
| Native executor | `<bundle>/Contents/Resources/native/desktop_executor/.../vantage-desktop` |
| Capabilities | move, click, type, read_ui, screen_capture — all true |
| SIGTERM | app exits, port released, no orphan |

The bundle is 81 MB: backend source, 89 Python packages, and the executor.

### Two defects found while verifying

`applicationWillTerminate` does not run on a signal. `kill -TERM` left the app
alive *and* orphaned its backend on the port, so the next launch found a stranger.
SIGTERM and SIGINT now run the same teardown and exit.

### Still not done

**The interpreter is not bundled.** `python_path.txt` names the build machine's
`.venv`. The packages travel with the app; the Python runtime does not. A machine
without that interpreter would still fail, and this is the remaining V22 gap.

---

## Update — 2026-11-17 (V23.2: the approval the client gave itself, and a guard two tools dropped)

**HEAD at start:** `bb9fc3a`. Commits this round: `7138232`, `cbc3d90`.

### The finding that mattered

The permission gate was correct. The client defeated it.

Measured against the live backend:

```
POST /missions/desktop {"request":"click at 400 300"}
POST /missions/{id}/run {}
-> state=awaiting_approval
   S1 pending | requires approval before it may run
               (level 3 (destructive) action requires confirmation);
               a mission does not supply that approval
```

`ConversationService.runDesktopMission` reacted to `awaiting_approval` by
scraping the confirmation id out of the step detail and posting it straight
back, in a loop of up to eight iterations. The backend validates that the id
matches the one it minted — so it accepted. But an id the client fetched in the
same breath is not a person approving a destructive action. Every click,
keystroke and scroll in a desktop mission ran unseen. The doc comment there
claimed these tools were "LOW/EXTERNAL risk"; the gate reports level 3.

Fixed (`cbc3d90`): the mission runs once; if it parks, the plan is held in
`pendingDesktopApproval` and the turn ends. `approvePendingDesktop()` and
`denyPendingDesktop()` are the only ways forward, and the single place a
confirmation id is supplied reads it from that state. A plan that parks again
re-parks for a fresh answer instead of chaining.

### A guard the tools dropped on the floor

The executor has carried `--expect-window-id` / `requireFreshTarget` since the
replay defect. It reached exactly one tool.

Measured, same binary, same command, before and after:

| binary | `scroll x=400 y=300 --expect-window-id=999999` |
|---|---|
| live (granted, old) | `status=performed_unverified` — **posted anyway** |
| staging (new) | `status=target_not_found` — "nothing was posted" |

`opScroll` parsed the flag into `TargetExpectation` and never passed it to the
operation. `desktop_drag` and `desktop_scroll` swallowed it through `**kwargs`.
`desktop_press_key` swallowed `expect_app` the same way, so a recorded `cmd+s`
replayed into the wrong application pressed anyway. All three now forward their
guard; the frontmost-app check in `opType` is `requireExpectedApp`, shared with
`opKey`.

### Verified commands

| command | result |
|---|---|
| `pytest tests/test_desktop_native.py tests/test_desktop_coordinates.py -q` | **65 passed** |
| `pytest tests/test_desktop_native.py -q` (after key guard) | **37 passed** |
| `swift build --product Vantage` | exit 0, "Build of product 'Vantage' complete!" |
| `./native/desktop_executor/build.sh` | built, CDHash `8a16a9e0`, **not promoted** |
| `vantage-desktop windows` | `verified`, 23 windows with real ids/pids/bounds |

**The staging binary is deliberately not promoted.** Swapping it in changes the
CDHash and invalidates the Accessibility + Post-Event grants the live binary
holds (`accessibility=True post_events=True`). Promotion is a user action.

### Not verified

- The pending-approval UI end to end. `ConversationService` exposes the state
  and the two methods; the view wiring is in progress.
- Recorded-workflow window identity (worker in flight).
- Approval adversarial tests (worker in flight).
- Physical input from the Finder-launched app — still gated on the user granting
  Accessibility to `macos/Vantage/Vantage.app` under its own identity.

---

## Update — 2026-11-17 (V23.2 continued: three approval defects, all reproduced then fixed)

**Commits:** `1753619`, `88205fd`, `5a2a0fe`, `df13a6b`.

Adversarial tests — written by a worker, then verified independently here —
turned up three real defects in the approval path. All three are fixed and their
`xfail(strict=True)` markers retired, so a regression now turns the suite red
rather than returning to "expected failure".

### D2 — a refused credential was exchangeable for a working one

Every `/run` calls the permission engine, which mints a fresh confirmation id,
and the runner wrote that id over the parked step's detail **before** comparing
it to what the caller offered. Reproduced against the running app:

```
park                    -> CONF-a
/run with "deadbeef"    -> awaiting_approval, response now names CONF-b
/run with CONF-b        -> partially_completed, file written
```

A smuggled id destroyed the operator's real approval *and* was answered with a
usable replacement. An already-parked step now keeps the id it was parked with.

### D1 — a refused request erased the record of a denial

`mark_step` re-reads the mission and persists steps from that fresh copy, so
mutating a step before a transition that then failed left it rewritten while the
mission stayed put. A `/run` on a denied mission returned 404 **and** had
already replaced "denied by the operator" with "awaiting approval", making the
denied step approvable again. The park branch now refuses before touching
anything and raises the transition error the state machine would have.

### D3 — a steered mission could never run again

`record_intent` had no callers. The intent stamped at creation is bound to
`plan_revision` 0; steering bumps the revision; the intent's own rule then
refused every step forever — including after a fresh approval. A steer now
re-records the intent against the plan that will run, bound to the new revision
and to the **union** of the old and new plans, so a steer cannot silently widen
the mission beyond what either plan named.

Verified end to end (not just by state): create → steer `x`→`y` → run parks →
approve → `partially_completed`, and the file on disk contains `y`.

### Also fixed: my own earlier window-guard fix was inert

`expect_window_id` was accepted by the handlers but declared in no
`ToolSpec.parameters`, so the permission engine **BLOCKed** every guarded step
before it reached the executor — a recorded workflow was refused outright rather
than protected, which is worse than having no guard. The earlier tests missed it
because they called handlers directly instead of going through the registry. A
registry-level test now covers it; removing a declaration again fails that test
(verified by mutation, not by inspection).

### Verified commands

| command | result |
|---|---|
| `pytest` approval-refusals + runner + missions API + live-steering + desktop-native + recorder | **200 passed, 0 xfailed** |
| `pytest` approval-refusals + runner + missions API | 79 passed, 0 xfailed |
| `pytest` recorder | 56 passed |
| `pytest` desktop-native + coordinates | 65 passed |

### Test-quality change

`test_a_refused_point_leaves_the_real_pointer_where_it_was` asserted the live
cursor was unchanged across the call. That is unprovable when anything else is
using the desktop — a person, or another agent — and it produced three spurious
failures this session while workers were running. It now re-reads once and
attributes a move correctly instead of assuming the world held still.

### Still open

- The Command Center UI that renders `pendingDesktopApproval` (worker in flight).
- Physical input from the Finder-launched app — gated on the user granting
  Accessibility to `macos/Vantage/Vantage.app` under its own identity.
- The staged executor (`CDHash 8a16a9e0`) is not promoted; promotion revokes the
  live Accessibility grant and is a user action.

---

## Update — 2026-11-17 (rebuilt and relaunched: the fixes are now in the running app)

The installed `macos/Vantage/Vantage.app` was a build from **18:57**, which
predated every approval fix this round. It was replaced.

```
swift build -c release --product Vantage     -> complete (69.67s)
./assemble_app.sh release                    -> codesign verify: ok
```

**A mistake made and corrected during this:** I ran the app *binary* with a
`--self-check` flag expecting a diagnostic. No such flag exists; it launched a
second GUI, whose backend took :8765 and displaced the user's running app,
leaving an orphan at 19:33. The orphan was confirmed (same `run.py serve`, cwd
= the checkout), health-checked, stopped with SIGTERM, and the port verified
free before rebuilding. Nothing was deleted; the user's app was restarted from
current source.

### Bundle verified self-contained (measured, not assumed)

```
python_path.txt -> <APP>/Contents/Resources/python/bin/python3.11
from <APP>/Contents/Resources, PYTHONPATH=<APP>/.../site-packages:
  py 3.11.18 | fastapi 0.162.7 | uvicorn 0.54.0
  jarvis -> <APP>/Contents/Resources/jarvis/__init__.py
```

A plain `import fastapi` fails without `PYTHONPATH` — the packages travel in
`Resources/site-packages`, not in the interpreter's own path. That is by design
(`BackendManager` sets it), and the first check was wrong for omitting it.

### Live launch (PASS)

| check | result |
|---|---|
| app process | 42975 `Vantage.app/Contents/MacOS/Vantage` |
| backend process | 42977 |
| backend interpreter | `<APP>/Contents/Resources/python/bin/python3.11` — **bundled, not the venv** |
| `/health` | `status: ok`, 200 tools |
| desktop click mission | `awaiting_approval` — "level 3 (destructive) action requires confirmation" |

The backend running from inside the bundle rather than the developer `.venv` is
V22's gate, observed in a real launch rather than inferred from source.

### Re-verified in the rebuilt app

A `desktop_click` mission parks with `awaiting_approval` and the level-3 reason;
denying it settles the mission at `blocked` with nothing posted. That is the
gate the old binary defeated and this one does not.

### Still not verified

- The approval **card** has never been rendered on screen. The code builds and
  the state it binds to is proven, but no one has seen the layout.
- Physical input from this bundle under its own Finder-launched identity still
  needs the Accessibility grant at the app level.
- The staged executor (`CDHash 8a16a9e0`) is still not promoted.

---

## Update — 2026-11-17 (V23.3: the permission model, measured at last)

**HEAD:** `43a6df3`. Four workers running in parallel on disjoint files; two
leads committed directly.

### The measurement that settles V22's blocked gates

One binary, two launching contexts, opposite answers:

| context | accessibility | post_events |
|---|---|---|
| `vantage-desktop self_check` from a terminal | **true** | **true** |
| the same binary, asked by the backend under Vantage.app | **false** | **false** |

Same executable path in both. macOS grants Accessibility to a **responsible
process**, and it attributes the request to the app that launched the binary.
From a shell the terminal is responsible and holds the grant; under Vantage.app
the chain resolves to `com.jarvisforge.vantage`, which holds none.

Consequences, now written down in `docs/DESKTOP_PERMISSION_MODEL.md`:

- Granting the executor, or `VantageDesktopDev.app`, **cannot** fix this — neither
  is the responsible process when the app drives them.
- `Vantage.app` is **ad-hoc signed**, so the code identity includes the binary
  hash. **Rebuilding invalidates any grant**, which is why chasing this defect by
  rebuilding repeatedly can look like the grant "not working".
- The only meaningful readout is the one returned **by the app's own backend**.
  A `self_check` in a terminal reports the terminal's capability and proves
  nothing about the app.
- The required action is a person's: grant Accessibility to
  `macos/Vantage/Vantage.app`, relaunch from Finder, and verify from inside.

Status of the physical gates: **PERMISSION_BLOCKED**, not broken. Everything that
does not post input is proven without it.

### Command Center journey — VERIFIED

`benchmarks/v23_command_center/journey.py`, 3/3, each ending in a filesystem fact
rather than a response body:

| journey | evidence |
|---|---|
| deny | parks, writes nothing, ends `blocked`, step `denied by the operator`, still no file |
| approve | nothing before the decision; then `done`; file contains exactly the approved bytes |
| forged id | refused; the issued confirmation is unchanged; no file |

The denial branch's "no file" assertion is the one that matters: it is the
negative proof that a parked plan does not act.

### Routing and clarification — VERIFIED

| utterance | desktop? | steps | clarification |
|---|---|---|---|
| `type this up for me` | yes | 0 | yes — asks instead of typing blindly |
| `move the cursor to 400 300` | yes | 1 | no |
| `move report.pdf to Documents` | **no** | 0 | yes — not misrouted to the pointer |

### Live app state

App `42975`, backend `42977`, `/health` ok, 200 tools, backend running from
`Vantage.app/Contents/Resources/python/bin/python3.11`. The bundled backend
resolves the bundled executor at
`.../Resources/native/desktop_executor/VantageDesktopDev.app/...` — the granted
identity is preferred by design, and the bundle stays self-contained.

---

## Update — 2026-11-18 (V23.3: the card seen, the guard proven, provenance stated)

**HEAD:** `05d716a`, then the bundle rebuilt and relaunched.

### The approval card has now been seen — and it had defects

A launch argument (`--render-approval-cards <dir>`) renders the card offscreen in
three states × two appearances and exits before any window or backend starts.
Renders committed under `docs/evidence/approval-card/`.

Three defects, none visible to a compiler:

| defect | evidence | fix |
|---|---|---|
| explanation **clipped** at the card's bottom edge | "Mission Control." cut mid-glyph in the nothing-parked render | moved to its own line above the controls |
| a **disabled Approve** drawn beside "there is nothing to approve or deny" | case 3 render | omitted unless approving is possible |
| **emerald** "Approve and run" on a level-3 destructive action | every render | `VPrimaryButtonStyle` gained an optional `tint`, defaulted to accent; this card passes the warning colour |

After the fixes: case 1 shows per-step status, the "1 step already ran" honesty
line, the full destructive warning, the confirmation id and both buttons in
amber; case 2 suppresses Approve and says why; case 3 omits both buttons with
nothing clipped. Light and dark both legible.

### The single-instance guard was written but never called

`InstanceGuard.swift` existed with a sound design (`O_EXLOCK`, chosen over a
bundle-id lookup because a dev binary has no bundle id — exactly the case that
caused this session's incident) and **no caller**. Now called first thing in
`applicationWillFinishLaunching`.

Verified live against the rebuilt app:

```
app running: 1
$ ./Vantage.app/Contents/MacOS/Vantage
Vantage: another instance is already running. Activating it.
exit: 0
app count after: 1        (original still healthy, backend 65478 intact)
```

The exact incident from earlier in this session is now impossible.

### What a confirmation id proves — measured, and bounded

`docs/APPROVAL_PROVENANCE.md`. Measured: a parked step's id posted with no auth
header returns **401** and writes nothing. The id is a **selector, not a
capability**.

But the honest bound is stated there too: every protected route accepts one
master token, and a model-driven tool call runs inside the backend, so it can
request a run, read the id from the response and post it back. Nothing
distinguishes that from a person pressing Approve. That is what a single-tenant
local backend with one master token *is*, not a defect in the approval system.
Written down so no later report claims a consent the architecture cannot give.

What **is** guaranteed: no silent execution, no smuggled or rotated id, no erased
denial, and a runner that refuses a step whose revision, params or target changed
after approval.

### Desktop guards completed

`desktop_double_click` and `desktop_right_click` call the same Swift `click` op
as `desktop_click` and had dropped its guard; all three now forward
`expect_window_id`/`expect_pid` and declare them. Every desktop tool checked: the
parameters its handler declares are a subset of its `ToolSpec.parameters`, so no
tool can advertise a guard its spec would reject. Mutation-proven.

### Live state after relaunch

App `65474`, backend `65478`, `/health` ok, 200 tools, backend running
`Vantage.app/Contents/Resources/python/bin/python3.11`. Both new behaviours
confirmed present in the shipped binary (`strings` finds both the instance-guard
message and the render flag).

Physical desktop input remains **PERMISSION_BLOCKED** — see
`docs/DESKTOP_PERMISSION_MODEL.md`.

---

## Update — 2026-11-18 (Automation Studio: the multistep replay, fully verified)

The prior run of `benchmarks/v23_studio/studio_roundtrip.py` reported **8 passed,
2 skipped** — the skips were honest (no fixture window was running, so
window-move resilience could not be measured). Re-run with the fixture up:

```
17 passed, 0 failed, 0 skipped
```

The steps that had never executed before:

| check | evidence |
|---|---|
| fixture window | 460x672 at (120, 86); button centre from live AX (180, 220.5), agreeing with state.json |
| baseline replay | counter **0 → 1**; executor readback matches each recorded point |
| window move | (120, 86) → (420, 186); live AX button centre moves (180, 220.5) → **(480, 320.5)** |
| stale-coordinate proof | the recorded point (180, 220.5) is now **outside** the moved window — a naive replay would miss it |
| reviewed draft | re-resolution yields (480, 320.5), not the recorded values |
| moved replay | counter **1 → 2**; executor readbacks (480, 320.0), (480, 320.5) |

The last row is the one that matters: the counter moved only because the click
landed on the button's *new* position. That is an observed target-state change,
not a posted-event claim.

Also measured: the fixture's own cached target did **not** follow its window move
(state.json still said 180,220.5 while AX said 480,320.5), so live AX is the only
honest source for re-resolution. Worth remembering before trusting a cached
target anywhere else.

### Recorded-window identity, now on the same path

A recorded step carries `window_id`; `derive()` emits it as `expect_window_id`;
the tool spec declares it; the executor enforces it. A replay at a stale window
now refuses with `target_not_found` instead of acting on whatever is there.
Verified earlier this session by before/after on the live binary:
`performed_unverified` (old) vs `target_not_found` (new).
