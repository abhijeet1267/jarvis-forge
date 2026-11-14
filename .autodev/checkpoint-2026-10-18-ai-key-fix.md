# 2026-10-18 — Native macOS app "AI not on" fix + supervisor hardening

## What the user reported

The native macOS app (the "new app", macos/Vantage) showed the AI provider as
"not configured / AI not on", and the whole thing appeared broken. The user
wants to use the native app (with its own native dashboard), not the web
dashboard/launcher.

## Root cause found and fixed (native app Add Key was sending the wrong field)

The native app's **Add Key flow was broken at the API contract level**:

1. **`store` field-name mismatch** (the "AI not on" root cause):
   `macos/Vantage/Sources/Views/ProvidersView.swift` `saveKey()` sent the key
   under `{"keychain_path": key}`, but the backend
   `jarvis/api/app.py` `store_provider_credentials` reads `body.get("api_key")`.
   So the key was never stored — the backend rejected it with 400 "Invalid API
   key format", the provider stayed "Not configured", and the app's Test button
   (`/ai/providers/{id}/test`, which also reads `api_key`) always failed.
   **Fix:** the native app now sends `{"api_key": key}`. Verified: source line
   has the comment + correct field.

2. **Provider picker listed unsupported providers**:
   the native picker offered `anthropic`, `google`, `openrouter`, `deepseek`,
   `ollama` — but the backend `PROVIDERS` registry (providers.py:546) only has
   `openai`, `gemini`, `groq`, `9router`. Selecting an unsupported id → 404.
   **Fix:** picker now shows exactly openai / gemini / groq / 9router.

Both fixes compile (`swift build` clean) and are in the rebuilt bundle
(binary contains "Google Gemini" / "9Router" / "OpenAI").

## Remaining cause of "AI not on" (owner prerequisite, not a bug)

Even with both fixes, the AI is _legitimately_ off until a real key is added:
`/ai/selection` returns `credential_configured: false` and no key is in the
keychain for any provider. To turn AI on: open the native app → AI Providers →
Add Key → paste a valid OpenAI/Gemini/Groq/9router key. That will now actually
store (field fixed) and the provider will show "Verified" after Test.

## Supervisory runner (scripts/autodev/autodev) — hardened this session

- Added `probe_worker_auth()` — preflight that a headless `claude -p` worker can
  authenticate. This environment is **desktop-managed**: `CLAUDE_CODE_HOST_AUTH_ENV_VAR`
  points at a host-managed auth mechanism, and spawned child `claude -p` processes
  report "Not logged in" (auth token NOT inherited). The supervisor now detects
  this definitively and records a `blocked` run with the exact reason instead of
  silently retry-looping. It deliberately does NOT try to bypass/inherit auth.
- Added per-worker auth detection in `run_worker` (worker "Not logged in" →
  clean stop as `blocked`, no retry loop).
- Rewrote `worker_prompt()` to build the prompt in code (removed the
  `worker_prompt.md` template file — a markdown formatter hook kept mangling the
  `__PLACEHOLDER__` tokens into `**bold**`, silently breaking substitution).
- Verified: worker_prompt substitutes REPO + task JSON cleanly; `do_start`
  fail-fast on auth records a blocked state and returns 1; `autodev status` is
  clean.
- Consequence: **unattended autonomous runs are NOT possible in THIS desktop
  environment** because spawned workers cannot authenticate. The supervisor is
  honest about this instead of pretending. It will work in an environment where
  `ANTHROPIC_AUTH_TOKEN` / normal login is available to child processes.

## Test suite (verified)

- Full Python suite: **2727 passed, 0 failed, 39 skipped** (424s) — green.
  The earlier "3 failed" (phase10 memory, phase30, phase44) were test-ordering
  pollution: each passes in isolation, in the 3-file group, and now in the full
  suite. No test weakening; the 3 tests are unchanged and passing.
- `tests/test_phase42_the_model_has_a_job.py` (conversational safety) — 7 passed.
- Gateway (test_companion_gateway.py) 32 passed; release pipeline 3 passed.

## Files changed (this session, layered on pre-existing WIP)

- `macos/Vantage/Sources/Views/ProvidersView.swift` — Add Key `api_key` field
  fix + provider picker aligned to backend registry.
- `jarvis/core/planner.py` — `_unrecognised` + `_SMALLTALK` wording now tells the
  user to configure AI Providers for conversational answers (ChatGPT-like goal).
- `jarvis/tools/memory_tools.py` — `remember_fact` now `verified=True` (local
  write confirmed by returned row); this was part of R2.4 default-UNKNOWN.
- `scripts/autodev/autodev` — auth probe + per-worker auth detection + in-code
  prompt builder (removed `worker_prompt.md`).
- `.gitignore` — track `.autodev/tasks.json` + checklist checkpoints.

## Owner prerequisites still open (unchanged)

1. Live AI model answer — needs a real provider key (now the Add Key flow works).
2. Android device-level pairing/pinning — no emulator/device here.
3. Release signing — needs Developer ID / Android keystore.
4. Commit the untracked native trees (`macos/`, `android/`, `jarvis/companion/`).

## Next task

After the user adds an API key in the native app, verify a real model answer +
multi-turn follow-up. Independently: R1.5 (repoint release pipeline to native
android/), R2.1 (multi-turn history), R3.1–R3.3 (macOS lifecycle/confirmations/
honest states).

## Follow-up: live AI now works (2026-10-18, evening)

Resolved "backend not working" + "AI not on" with the user's 9Router config:

- Base URL: http://127.0.0.1:20128/v1 (9Router gateway — was UP; /health 404s by
  design but /v1/models and /v1/chat/completions both work)
- Key: stored for provider `9router` via the (already-fixed) /store endpoint.

Root causes, in the order they blocked:

1. **Backend down** — nothing on :8765 (stale kill from earlier). Started it.
2. **`ai_enabled: false`** persisted in ~/.jarvis-forge/ai_selection.json — the
   AI on/off switch was off, so `llm_enabled` was false regardless of the key.
   Re-enabled via POST /ai/selection {ai_enabled:true}.
3. **Provider selection reset to `openai`** on restart (no openai key) — the
   persisted selection had reverted. Re-persisted provider=9router/model=smooth/
   base_url=20128 via /ai/selection.
4. **Real parse bug**: 9Router emits a streaming `data: [DONE]` sentinel after its
   single JSON answer; OpenAIProvider.parse's json.loads failed with "unreadable
   response: Extra data". Fixed NineRouterProvider.parse to strip the sentinel.
   Regression tests: tests/test_nine_router_provider.py (3 passed).

Final live verification:

- /ai/chat/send → ok:True, "Hello there friend", provider 9router, model smooth.
- /health → llm_enabled:True, ai_provider:9router → native app shows "AI on".

Files changed this follow-up:

- jarvis/gateway/providers.py — NineRouterProvider.parse strips `data: [DONE]`.
- tests/test_nine_router_provider.py — new (3 tests).
- (selection/key state is runtime state under ~/.jarvis-forge, not in-tree.)

## Added: custom "OpenAI-compatible" provider (key + base URL + model id)

User can now add any OpenAI-compatible endpoint (zcode, opencode, LM Studio, …)
by entering its API key, base URL, and optional model id.

Backend:

- jarvis/gateway/providers.py: new `OpenAICompatibleProvider` (OpenAI wire shape,
  empty DEFAULT_BASE_URL so the user's persisted `llm_base_url` is the endpoint;
  empty = "not configured", never a vendor nobody chose). Registered in PROVIDERS.
- jarvis/security/providers.py: `_DISPLAY` entry "OpenAI-compatible (custom)".

Native app (macos/Vantage/Sources/Views/ProvidersView.swift):

- Add Key sheet now has a "OpenAI-compatible (custom)" picker option; selecting it
  reveals two extra fields: Base URL (required) and Model id (optional).
- saveKey stores the key (/store, field `api_key`), then persists the selection
  {ai_provider, llm_base_url, llm_model} via /ai/selection for the custom provider.

Verified:

- /ai/providers + /ai/selection list `openai_compatible` and resolve it with a
  user base_url+model → llm_enabled:true.
- Full custom flow (store key → set selection → chat) resolved end-to-end; the one
  transient `call failed: timed out` was 9Router cold-route latency (direct curl to
  20128 succeeds), not a wiring bug.
- tests: test_provider_architecture.py updated for the sanctioned empty-default
  exception; test_nine_router_provider.py + provider tests all green (46 passed).

Note re "backend app is not working properly": backend (8765), 9Router (20128), and
the app are all up; /health = llm_enabled:true / ai_provider:9router. The remaining
app-side check is a fresh relaunch to pick up the rebuilt bundle (binary now contains
the new UI strings).
