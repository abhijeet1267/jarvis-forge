# Session checkpoint — 2026-10-16 (first autonomous run)

This checkpoint tells a future session exactly where things stand. Read
`CLAUDE.md` → `docs/PROJECT_STATUS.md` → `.autodev/tasks.json` for the live
state; this file explains the _why_ and the traps.

## What was verified (baseline, before any change)

- Full suite: **2665 passed, 9 failed, 41 skipped** (745s). All 9 failures root-caused.
- Collection was broken by `playwright` top-level import in
  `tests/test_phase92_browser_automation.py` (fixed: `importorskip`; file skips).
- `keyring` was undeclared/uninstalled → 3 `llm_enabled` failures + silent no-op
  key store. Fixed: declared in `requirements.txt` + `pyproject.toml`, installed.
- Phone allowlist: WIP had added `/projects` to the backend `PHONE_ALLOWED`,
  contradicting Phase 67's _pinned_ decision and the module's own
  "every path phone.html actually calls" rule. **Removed that one line from
  the user's WIP** — flagged in PROJECT_STATUS §4. The gateway's
  `PHONE_ALLOWED` is a _strict subset_ of the backend's by design (remote <
  local surface) — do NOT "fix" the divergence by widening the gateway.
- `phase34` ratchet: re-recorded to the verified 7-site silence-only set
  (incl. the new gateway proxy). `phase39c`: dashboard is _dark-first_ (base
  `:root` is dark; `prefers-color-scheme: light` overrides) — 2 tests assumed
  light-first and were corrected to check the same concern the other way; 5
  stray hex colors tokenized + a 430px breakpoint added (real CSS fixes).

## What was built (all tested, suite green)

- **R1.1/R1.2** — 31 gateway security tests (`tests/test_companion_gateway.py`);
  found+fixed a real bug: `issue_pairing_code` didn't supersede old codes
  (docstring promised it). One-shot codes now enforced.
- **R1.3** — `jarvis/companion/server.py`: backend owns the gateway subprocess
  (own process group, TLS chain-verified readiness — self-signed cert can't
  match the host, so we verify the _chain_ against our own cert; the Android
  equivalent is fingerprint pinning, R1.4). Started in the FastAPI lifespan
  when `lan_mode` on, stopped in `finally`. `GET /companion/status` +
  `/health.companion` report real state. 7 tests in
  `tests/test_companion_server.py`. The macOS app's own gateway spawn was
  neutralized (single-owner; it would double-bind :8766) —
  `BackendManager.startCompanionGateway` is now a documented no-op.
  `swift build -C macos/Vantage` passes.
- **R2.1** — multi-turn memory on the model surface: `AIProvider.chat`/
  `_chat_request` gained optional bounded `history` (OpenAI list; Gemini
  user/model alternation with same-role merge); `send_ai_message` loads the
  last 12 turns from `ai_messages` by `conversation_id` and drops the
  in-flight user turn (the client pre-persists it). 7 tests in
  `tests/test_conversation_history.py`. `/command` fallback history is R2.2
  (it has no conversation id).
- **R5.1/R5.2** — fail-loud codesign in `build_app.py` (committed) +
  `installers/macos/build_dmg.sh`; `scripts/release_verify.sh` now records
  commit/timestamp/sha256 in the manifest and self-checks it (untracked —
  see commits).
- **Infrastructure** — CLAUDE.md, docs (roadmap/status/architecture/runbook),
  `.autodev/` queue + supervisor (`scripts/autodev/autodev`, stdlib) with
  real headless `claude -p` workers, persisted deadline, backoff,
  checkpoints, login-agent scripts. Deadline-persistence verified (expired
  run refuses to extend). Committed.

## Traps / things a next session will trip on

1. **The tree is heavily dirty with the owner's WIP** (128 modified + 69
   untracked at start, incl. the entire `macos/`, `android/`,
   `jarvis/companion/` trees and `dashboard/index.html`). My edits are
   layered on that WIP in: `jarvis/api/app.py`, `jarvis/gateway/providers.py`,
   `dashboard/index.html`, `tests/test_phase34/39c/ai_chat_provider_path`,
   `jarvis/companion/gateway.py`. **Do not commit those files without the
   owner** — they'd carry the owner's unreviewed WIP. What IS committed:
   the infrastructure (commit `0c37d4d`) and `build_app.py` (`aa1b66a`).
2. **The native `android/` app does not compile** — ~26 Kotlin errors (missing
   types: `PairingCodeResponse`, `ApprovalsViewModel`, `ApprovalsScreen`,
   `appointments` nav; duplicate `DeviceHealthInfo`; missing imports). It's
   half-written WIP. I fixed the _build config_ (version catalog was missing
   4 aliases the script referenced + wrong `compose-compiler` plugin alias;
   needs `ANDROID_HOME=$HOME/Library/Android/sdk`) but the _app code_ is the
   owner's to finish. R1.4/R1.5 are blocked on that.
3. **`android/` and `macos/` are untracked by git** — a `git clean` would
   destroy the owner's work. Never run destructive git here.
4. The **formatter hook** auto-reformats `docs/*.md` after edits — re-Read
   before a second edit to the same region.
5. `pyproject.toml` `[project] deps` and `requirements.txt` were out of sync;
   both now include `keyring` + `python-multipart`. The venv is uv-managed
   (`uv pip install --python .venv/bin/python`, no pip).
6. No `timeout` command on this macOS — use the Bash tool's timeout param.

## Remaining work (ranked, in .autodev/tasks.json)

- R2.2 unify chat surfaces (next; inherits /command history)
- R2.3 model-planned actions; R2.4 default-unverified verification
- R3.1–R3.5 macOS app (lifecycle orphaning, destructive-action confirmations,
  silent error states, version from Info.plist, URL encoding)
- R0.3/R1.4/R1.5 — blocked on the owner (git decision + native app compile)
- R4.x workflows (calendar/email/browser slices)
- R5.3 CI for the Android build (once it compiles)

## Run state

- Interactive session did the work directly (not via the autodev supervisor).
- No supervisor run is active; `.autodev/state.json` may not exist. To run
  unattended: `scripts/autodev/autodev start --hours 12` (see the runbook).
- **Final full suite (through R3.2): 2721 passed, 39 skipped, 0 failed** (407s).
  Green. Every skip is a named external prerequisite.

## Completed this run (updated)

R0.1, R0.2, R1.1, R1.2, R1.3, R2.1, R3.2, R5.1, R5.2.
R3.2 also FIXED a broken button: "Delete All Data" was POSTing an empty body
to `/privacy/delete` which requires `{scope, confirm}` — it 422'd and the empty
`catch {}` swallowed it, so the button did nothing. Now sends
`scope:"everything", confirm:true` and surfaces errors.
Committed: infrastructure (`0c37d4d`) + `build_app.py` signing fix (`aa1b66a`).
Everything else sits on the owner's uncommitted WIP (see Traps #1).
