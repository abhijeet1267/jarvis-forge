# 2026-10-18 — P0 provider test + status + selection repair

## Root causes (all confirmed, all fixed)

1. **Test button → 422 "Field required"**: the native app's Test button POSTs
   `/ai/providers/{id}/test` with NO body, but the route declared
   `body: dict = Body(...)` (required). FastAPI rejected the empty body.

2. **"Not tested" / connection test couldn't succeed**: `verify_connection` hit
   `GET /models` and required a matching hardcoded `SUPPORTED_MODELS` entry —
   impossible for 9Router (empty list) and any custom endpoint. A connection
   test must be a minimal chat completion.

3. **9Router/custom "unreadable response: empty"**: the OpenAI-shape adapter is
   synchronous (parses one JSON body), but did NOT send `stream: false` — so
   9Router (SSE by default) returned `data: {chunk}` lines json.loads cannot
   decode. Fixed with `"stream": false` in `OpenAIProvider._chat_request`.

4. **"Current Selection: Unknown"**: the native `ProviderSelection` model
   decoded `provider_id`/`provider_name`/`mode`/`reasoning`, but the real
   `/ai/selection` response sends `ai_provider`/`llm_model`/`llm_base_url`/
   `ai_enabled`/`llm_enabled`/`credential_configured`/`known_providers` — so
   every field decoded nil → "Unknown". Model + SelectionInfoView rewritten
   to the real schema.

5. **Home card "Loading…" next to a red dot**: `HomeView.backendDetail` returned
   "Loading…" whenever `health` was nil, even when `state` was `.error` (red).
   Now reflects the state (Connecting / error reason / Stopped).

6. **Bottom-left bare "Error"**: `ContentView.statusText` discarded the
   `.error(message)` payload. Now shows the message.

7. **Provider picker listed unsupported providers** (anthropic/google/openrouter/
   deepseek/ollama) → 404. Aligned to the real registry + added a generic
   "OpenAI-compatible (custom)" option with base URL + model fields.

## Provider / test fixes (backend)

- `jarvis/gateway/providers.py`:
  - `OpenAIProvider._chat_request` now sends `"stream": false`.
  - `verify_connection` rewritten to a minimal chat completion (reuses
    `_chat_request` + `_post`; no GET /models, no static-model-list match).
  - `test_credentials(key, model=...)` threaded the model through.
  - New `OpenAICompatibleProvider` (empty DEFAULT_BASE_URL = user's base URL
    wins; registered in PROVIDERS).
- `jarvis/security/providers.py`: `_DISPLAY` entry "OpenAI-compatible (custom)".
- `jarvis/api/app.py` `/ai/providers/{id}/test`: body now OPTIONAL; tests the
  _stored_ key (KeychainStorage.retrieve) + effective base URL + selected model;
  custom provider without base URL → honest "base URL required"; default timeout
  30s (matches the chat path).

## Native app fixes (macos/Vantage)

- `ProvidersView.swift`: picker aligned + "custom" option; Add Key sheet shows
  base URL + model id for custom; `testProvider`/`saveKey` now use `api_key`.
- `APIModels.swift`: `ProviderSelection` rewritten to the real `/ai/selection`
  schema.
- `ContentView.swift`: error status shows the reason, not "Error".
- `HomeView.swift`: backend card detail reflects real state, no false "Loading…".

## Tests

- `tests/test_nine_router_provider.py` (5): 9Router `data:[DONE]` strip, plain
  parse, error body, `stream:false` in payload, verify-is-a-chat-completion.
- `tests/test_provider_test_endpoint.py` (4): no-body Test not a 422; no-key is
  honest; custom requires base URL; Test does not mutate selection.
- Full Python suite: **2732 passed, 0 failed, 39 skipped** (361s).
- Live: backend 200 / llm_enabled True / provider 9router; Test → True
  "Connection verified successfully"; chat → "Hello, how are you?".

## Remaining blockers (unchanged)

- Live OpenAI/Gemini/Groq/Qwen: not tested — no keys for those providers (the
  user's key is 9Router); the generic OpenAI-compatible path is verified via
  9Router. Add those keys and the same Test flow applies.
- Swift unit tests still need full Xcode (CLT has no XCTest) — same as before.
- Commit/push NOT done (untracked native trees + prior WIP preserved).

## How the user adds + tests a provider (native app)

AI Providers → Add Key → pick provider → (custom: base URL + model id) → Save →
click Test → "Connection verified successfully". Command Center then answers via
the selected provider/model.

## Git state

Working tree: provider.py, security/providers.py modified; test_nine_router_provider.py,
test_provider_test_endpoint.py new; native app files (untracked tree) modified.
No commits, no push. Pre-existing WIP preserved.
