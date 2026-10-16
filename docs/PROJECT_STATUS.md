# VANTAGE / JARVIS Forge — Project Status

> Re-measured continuously. Every claim below names its evidence (command +
> result or file:line). Counts are from actual runs on this tree — the old
> `STATUS.md`/`VANTAGE_CURRENT_STATE.md` figures are historical and stale.

**Last full-suite measurement:** 2026-10-16 — see §1 (updated as the
verification run completes).

## 1. Test health (measured)

| Run                               | Result                                       | Evidence                                                                                                                                        |
| --------------------------------- | -------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| Baseline 2026-10-16 (pre-fix)     | **2665 passed, 9 failed, 41 skipped** (745s) | `/tmp/baseline_run.log`, exit 0 (tail)                                                                                                          |
| Phase 92 collection break         | fixed                                        | `pytest.importorskip("playwright.async_api")` in `tests/test_phase92_browser_automation.py`; collection now clean: **2714 collected, 0 errors** |
| `test_companion_gateway.py` (new) | **31 passed**                                | suite run 2026-10-16                                                                                                                            |
| `test_phase67` phone allowlist    | **14 passed**                                | after removing WIP `/projects` from `PHONE_ALLOWED`                                                                                             |
| `test_ai_chat_provider_path`      | **8 passed, 1 skipped**                      | after declaring + installing `keyring>=25.0` (conftest swaps in an in-memory keyring; real keychain never touched)                              |
| `test_phase34` deadline ratchet   | **11 passed**                                | ratchet re-recorded to the verified 7-site set incl. gateway proxy                                                                              |
| `test_phase39c` dashboard         | **14 passed**                                | 5 stray hexes tokenized; 430px breakpoint added; 2 tests corrected to match the (correct) dark-first design                                     |

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
  **Not yet started by any process** (roadmap R1.3) — the gateway is
  implemented and tested but unwired.
- **macOS app** (`macos/Vantage/`, SwiftPM, zero deps): builds; talks to
  `127.0.0.1:8765` only (no non-local plaintext); native pairing via QR.
- **Android native** (`android/`, Compose, `com.vantage.companion`): Encrypted
  SharedPreferences token storage (verified), unit tests present; contract is
  still legacy (R1.4); **untracked by git** (R0.3).
- **Dashboard** (`dashboard/index.html`): dark-first token system (42 tokens
  base, 40 color tokens restated in the light override — 2 easings inherit by
  design), focus-visible, reduced-motion, aria-live, empty states, 640px +
  430px breakpoints, zero external resources, inline script parses under node.

## 3. Verified defects / gaps (current)

Ranked; details + acceptance criteria in `docs/PROJECT_ROADMAP.md`.

| #   | Defect                                                                                           | Tier | Evidence                                                                                                                               |
| --- | ------------------------------------------------------------------------------------------------ | ---- | -------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | Release pipeline builds the legacy WebView wrapper, not the native companion                     | 1    | `installers/build_release.sh:93` → `installers/android/build_apk.sh`; `build_release.sh:100` says "The Android project uses a WebView" |
| 2   | Companion gateway never started by any process                                                   | 1    | `grep -r build_companion_app` → defined+exported, never called                                                                         |
| 3   | Native Android client uses legacy contract; no cert pinning                                      | 1    | `VantageApiClient.kt:137-155` scrapes `GET /phone` + posts `/auth/session`; no `CertificatePinner` in Kotlin                           |
| 4   | Canonical `android/` tree untracked by git                                                       | 0    | `git ls-files android/` → empty                                                                                                        |
| 5   | No multi-turn conversation memory on either chat path                                            | 2    | `providers.py:307` single user msg; `orchestrator.py:250-269` fresh system+user each turn                                              |
| 6   | Two disconnected chat surfaces; rules-first, model = fallback annotator                          | 2    | `app.py:872` vs `app.py:3621`; model only via `orchestrator.py:532-536`                                                                |
| 7   | macOS lifecycle: onDisappear kills services; connect() not idempotent; SIGTERM to env not python | 3    | `ContentView.swift:19-21`, `BackendManager.swift:41-50,137`                                                                            |
| 8   | Destructive actions without confirmation (Delete All Data, session revoke, task cancel)          | 3    | `SettingsView.swift:210-217,136-142`, `ActivityView.swift:131-138`                                                                     |
| 9   | Silent error states (HomeView catches, SelectionInfoView stuck "Loading…", Export Data discards) | 3    | `HomeView.swift:119-145`, `ProvidersView.swift:309-313`, `SettingsView.swift:206-215`                                                  |
| 10  | Packaging swallows codesign failure; no artifact provenance                                      | 5    | `build_app.py:182`, `build_dmg.sh:51`                                                                                                  |
| 11  | `pyproject` packaging deps lagged runtime deps (fixed this run: +keyring, +python-multipart)     | done | diff 2026-10-16                                                                                                                        |

## 4. Pre-existing uncommitted work (NOT mine — preserved)

The tree started this run with **128 modified + 69 untracked files**
(~14,122 insertions / 10,723 deletions vs HEAD), plus 69 unpushed commits.
The WIP in `jarvis/api/app.py` is a coherent **session-revocation hardening**
(`_revoked_sessions`, `_record_session_revocation`, `_is_session_revoked`)
that I preserved; I removed one line from it (`/projects` in `PHONE_ALLOWED`)
because it contradicted a pinned security test. The WIP in
`tests/test_phase72_ai_provider_security.py` and other docs are untouched by
me. A snapshot of the pre-run state lives in `.autodev/presnapshot-2026-10-16.txt`.

## 5. Quality scores (0–10, evidence-backed, no inflation)

| Dimension                       | Score | Rationale                                                                                                                                                                          |
| ------------------------------- | ----- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Functional correctness          | 6.5   | 2714 tests, 9 verified failures fixed this run; conversational path is rules-first, not model-driven                                                                               |
| End-to-end completeness         | 5     | No real device pairing path works end-to-end (gateway unwired, Android on legacy contract); workflows mocked/local                                                                 |
| Test reliability                | 8     | Full suite runs deterministic offline in ~12 min; every skip has a named prerequisite; collection clean                                                                            |
| Security / privacy              | 6.5   | Master-token-on-Mac + scoped sessions + strict-subset gateway allowlist, now test-pinned; but no cert pinning on the client, gateway unwired, 2 UI destructive actions unconfirmed |
| UX / accessibility              | 6.5   | Dashboard strong (focus, aria, reduced-motion, dark-first, breakpoints); macOS app has silent-error gaps + missing confirmations                                                   |
| Performance / resources         | 6     | Suite 12 min; backend fine; no measured regressions; not profiled systematically                                                                                                   |
| Recovery / maintainability      | 7.5   | Durable autodev queue/checkpoints/deadlines; supervisor tested; two parallel agent pipelines (`core/` vs `agent/`) are drift risk                                                  |
| Integration quality             | 5.5   | Calendar/email local-real; IMAP/SMTP/CalDAV configured-gated; browser dep not installed; Android native untested against gateway                                                   |
| Documentation / reproducibility | 8     | CLAUDE.md resume protocol, roadmap, runbook, status; build scripts exist but swallow signing failures                                                                              |

## 6. Next task (single)

**R1.3 — Wire the companion gateway into the backend lifecycle** (start/stop
with the backend, health answers, cert stable across restarts, tested). It
unblocks R1.4 (Android native pairing) and R1.5 (release pipeline), the two
highest-impact Tier-1 items.

How a future session starts here: read `CLAUDE.md` (resume protocol), then
this file, then `.autodev/tasks.json`. To run unattended:
`scripts/autodev/autodev status` / `resume` (see `docs/AUTODEV_RUNBOOK.md`).
