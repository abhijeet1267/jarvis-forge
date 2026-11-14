# VANTAGE — Autonomous Development State

> The checkpoint. Read this **first**, then `VANTAGE_AUTODEV_PROMPT.md` for
> the standing requirements, then `docs/PROJECT_STATUS.md` for the long
> record. This file is short on purpose: it must point the next iteration at
> the next action, not re-describe the project.

**Last updated:** 2026-11-14, after Vantage X P0 + P1/P2 vertical slice (iteration 90).

## Iteration 90 — Vantage X: foundation repair + mission contracts + ProofGraph (2026-11-14)

### P0 — foundation

- **Companion certificate failed on any long hostname.** `CN=vantage-companion-<hostname>`
  exceeded OpenSSL's 64-byte subject limit on a CI runner, so *every* companion test
  errored at certificate generation ("string too long: maxsize=64"). Fixed in
  `jarvis/companion/cert.py` with a bounded CN that keeps a digest of the full name so
  trimmed hosts stay unique. This was a real bug, not a CI quirk.
- **CI never installed Chromium**, so every browser test failed with "Executable
  doesn't exist" and read as a code failure. Added
  `python -m playwright install --with-deps chromium`.
- **The Android job blanked its own SDK path.** `env: ANDROID_HOME: ${{ env.ANDROID_HOME }}`
  reads the *workflow* env context, which never defines it, so it resolved to "" and
  overwrote the runner's SDK. Removed the override; the aapt2 step now probes
  `ANDROID_HOME`/`ANDROID_SDK_ROOT`/the macOS default and picks the newest build-tools.
- **The Python job never started a backend**, so the wire-contract tests failed with
  `FileNotFoundError: ~/.jarvis-forge/api_token`. Those tests pass against a live
  backend (verified locally); CI now starts `run.py`, waits for `/health`, and stops it.
- **Swift test-target compile errors**, each fixed at the cause: `FlowAuthoringTests`
  referenced Carbon's `controlKey`/`optionKey`/… which a test target cannot see through
  `@testable import Vantage` — the masks are now declared on `ShortcutSpec` and both
  sides use them; `MacControlCoordinator.combine` was main-actor-isolated although it is
  a pure function (now `nonisolated`); `BackendManagerStateTests` used the non-existent
  `XCTAssertNotSame`, compared optional tasks with `===`, and called a memberwise
  `HealthResponse` initializer that does not exist (it now decodes the real payload).
- **`test_no_duplicate_routes.py`** (new) fails if any `(path, method)` is registered
  twice, and separately if two routes claim one OpenAPI operation id.

### P1 — Mission contracts (`jarvis/core/missions.py`)

A mission is a durable objective with explicit success conditions and a validated
state machine (`draft → planned → awaiting_approval → running → verifying →
completed`, plus paused/blocked/recovering/failed/cancelled/partially_completed).

What the contract refuses to do is the feature:

- `completed` is refused unless **every** condition is `verified`. A mission with no
  conditions can never complete — "done" would be an assertion.
- Illegal transitions raise `MissionTransitionError`; they are never silently ignored.
- `verdict()` returns `verified` / `partial` / `failed` / `unknown`, so a mission whose
  steps all "succeeded" but whose conditions were never checked reports `unknown`.

`jarvis/core/mission_runner.py` runs the slice: gate each step through the real
`PermissionEngine`, execute through the owning agent, then verify with a *separate*
observer (`Verifiers.file_contains` / `file_hash` / `file_absent`). A runner with no
permission engine refuses to run (absence is not permission), a refused step blocks the
mission without touching anything, and a completed step is not replayed on resume.

### P2 — ProofGraph / ProofLens (`jarvis/core/proofgraph.py`)

Evidence is the **existing hash-chained audit log**, not a second store. Each
verification writes a record whose `detail` carries `mission_id` and `condition_id`;
ProofLens reads those back and answers claim by claim: what was observed, what
contradicted it, and which claims have **no** observation at all. The report states in
words that the chain detects an edit inside itself and is *not* signed or externally
anchored.

Found while wiring it: `AuditLog.record()` generated the log id inline and discarded it,
so evidence could be written but never cited. `AuditRecord` now carries `log_id` and the
log returns it.

### Wiring

- `jarvis/api/missions_routes.py` — a router (not more lines in the 7,500-line
  `app.py`) for list/get/create/transition/cancel/run/proof. Auth is attached at
  construction, and the runner refuses rather than guessing if no gate is attached.
- `macos/Vantage/Sources/Views/MissionControlView.swift` — Mission Control plus the
  ProofLens panel, wired into `NavItem`. It states verdicts in words as well as colour,
  and shows "No missions yet" only when the read actually succeeded.

### Evidence

- `tests/test_mission_contracts.py` (16), `test_mission_runner.py` (11),
  `test_missions_api.py` (7), `test_proofgraph.py` (6),
  `test_demo_a_file_mission.py` (3), `test_no_duplicate_routes.py` (3) — all passing.
- **Demo A, both halves**: a real write to a temp file completes the mission because a
  verifier re-read the file and found the change; the identical run with the write
  removed reports `partially_completed` / `failed`.
- **End-to-end with real evidence**: a mission run in-process produced verdict
  `verified`, the condition carried `LOG-a6119014c7be`, and ProofLens resolved that
  record through `verify_chain`.
- `swift build` succeeds with the new view and models.

### Live verification (over HTTP, real gate, real tool)

Run against the restarted backend, not a test client:

```
POST /missions  {request, conditions[C1], steps[S1 read_file]}
POST /missions/{id}/run  {verifiers:{C1:{kind:file_contains, path:/tmp/demo_note.txt, needle:"--fast"}}}
-> state: completed | verdict: verified
   step S1 done
   cond C1 -> verified | /tmp/demo_note.txt contains '--fast' (sha256:47092bac8f373b5d, 31 bytes)
```

**Three bugs this live run found that the unit seams could not**, each pinned by a
test now:

1. **The gate compared the wrong vocabulary.** `PolicyDecision`'s values are
   `EXECUTE`/`CONFIRM`/`BLOCK`; the adapter compared against `"execute"`, so every
   *allowed* step read as refused and the mission reported `blocked` with the reason
   "low-risk action" — a policy-looking message for a string mismatch. `CONFIRM` is
   also now treated as needing approval rather than as allowed: a mission is not an
   approval.
2. **A failure reported the word "failed".** `ActionResult` carries its reason in
   `error` when it fails; the runner read only `message`, so "File is too large to read
   inline (349.8 KB)" surfaced as a bare "failed".
3. **`get_core` was imported from the wrong module** (`core.orchestrator`, where it does
   not exist); it lives in `api/app.py`. The route 500'd until fixed.

A fourth, found by the gate working correctly: the real `PermissionEngine` refused a
step whose plan omitted a required `path` parameter. That is the system behaving as
designed — it declined to invent a parameter rather than guessing one.

### P3 — ParallelWorld (`jarvis/core/parallelworld.py`, `jarvis/api/files_routes.py`)

Preview-before-apply for local file operations, scoped honestly to **create,
replace, append, delete** — the operations that are genuinely reversible. The note
in every preview says applying is not atomic across a crash and that no external
service action is reversible, so the capability cannot be mistaken for more.

- **A preview never writes.** The proposed text is computed and diffed against the
  snapshot; the target is untouched. Pinned by a test that hashes the file before and
  after a preview.
- **Apply needs explicit approval** (`approved: true`, nothing inferred), and it
  **re-hashes the source immediately before each write**. A file edited after the
  preview is refused with "changed after the preview, so applying this would overwrite
  work nobody saw" rather than silently overwritten.
- **The tools' own path guard is reused** (`tools/files._guard`), so a preview and an
  apply both refuse protected system locations — enforced at both points, not just
  where the plan is built.
- A failed apply restores that file from the bytes read moments earlier.

**Live, over HTTP:** a preview reported `modified: 1` and left the file unchanged; an
apply without approval returned **409** with the file unchanged; an approved apply
returned `applied: true` and the file held the new text; `delete /etc/hosts` was
refused at preview time with "/private is a protected system location."

**Test helper bug found here:** `flat_routes()` returned zero routes because FastAPI
0.162 keeps an included router's children on `original_router`; the helper now reads
that, and the two tests that iterate routes were updated to use it (they had been
sniffing `[r.path for r in app.routes]`, which raises on any app with an included
router).

### P4 — Self-healing missions (`jarvis/core/selfhealing.py`)

Observers (`observe_http`, `observe_file`, `observe_owned_process`) report
VERIFIED / FAILED / UNKNOWN from what they actually see, and a `RecoveryController`
proposes at most one bounded repair. The refusals carry most of the value:

- **An unowned resource is never restarted** — "the failing resource is not one
  Vantage owns, so it will not be restarted".
- **An UNKNOWN observation escalates rather than acting** — acting on silence is how
  a broken observer becomes a restart loop.
- **Attempts are bounded** (3 by default) with a minimum interval between them, and
  the tenth call says how many remain.
- Process identity is a durable `{pid, started_at, command}` record checked against
  the OS start time, so a recycled pid never reads as "our process is alive".

**Live, over HTTP (`POST /missions/observe`):** a healthy service → `verified`,
`needed: false`; after the port closed → `unknown`, escalated, **not** restarted even
with `can_restart: true`; a reachable service returning the wrong body → `failed`,
`allowed: true`, action `restart`; the same failure with `can_restart: false` →
refused as unowned.

### Phase 1 fix — CONFIRM is an approval, not a permission

The runner treated a `CONFIRM` decision as a refusal. It now parks the mission in
`AWAITING_APPROVAL` with the gate's confirmation id, and resuming requires an
approval whose confirmation id **matches** the one the gate issued. A mission parked
there stays parked across a restart and never resumes itself. `RUNNING →
AWAITING_APPROVAL` was added to the transition table, since an approval discovered
mid-execution is legitimate and must not unwind completed steps.

### Phase 3 — ParallelWorld native UI (`macos/.../Views/ParallelWorldView.swift`)

Plan editor, preview summary, change browser, before/after diff, conflict inspector
and result panel, wired into `NavItem`. Apply is disabled while the plan conflicts,
while nothing is previewed, and while busy — and the reason is always shown in words.
Nothing in the view invents progress or success: every value comes from the backend.

### V2 evidence

- `test_mission_runner.py` now 19 (audit-write failure ⇒ no verified claim; CONFIRM
  parks; resume without approval stays parked; matching approval resumes).
- `test_parallelworld.py` 17 (partial failure identifies applied vs refused; stale
  preview refused; a directory is a conflict, not a crash).
- `test_selfhealing.py` 12, `test_demo_a_file_mission.py` 5 (restart-and-resume,
  interrupted missions listed).
- `swift build` succeeds with the new screen; the UI's exact API payloads verified
  live (preview reports before/after without writing; apply verified per file;
  `/etc/hosts` refused as protected).

**Found by exercising the installed app, not the tests:** `/files/preview` returned
**500** for a path that is a directory — `read_text` raised `IsADirectoryError` straight
out of the route. A directory is now reported as a conflict ("this preview and apply
cover files only") in both preview and apply, and the route answers 200 with
`applicable: false`. Two tests pin it. This is the second bug this session found only
by running the real surface: the unit seams never pass a directory.

**The installed app was rebuilt, signed and reinstalled** with Mission Control and
ParallelWorld in the sidebar; it connects to the backend (114 requests to `:8765`, only
the deliberate port probes rejected).

## Iteration 89 — 9router configured per section (2026-11-14)

9Router is running on this machine (`node`, `127.0.0.1:20128`) and its catalog
reports **1033 models**. Configuration is driven from that live catalog, not from a
guessed list: `GET /api/v1/models` carries per-model capabilities, and the counts are
what decide which tab can use the gateway.

**Configured (from real capability data):**

| section | model | chosen for |
|---|---|---|
| text | `kc/google/gemini-2.5-pro` | tools + reasoning, 1M context |
| documents | `kc/google/gemini-2.5-pro` | pdf + vision, 1M context |
| vision | `kc/google/gemini-2.5-pro` | vision, 1M context |

9router was added to those three tabs' provider lists in
`jarvis/gateway/provider_categories.py` (it was previously offered for `text` only).

**Deliberately not configured — the gateway cannot do these jobs:**

- **image** — 0 models with `imageOutput`.
- **speech** — 0 models with `audioOutput` (no TTS).
- **video** — the tab submits a generation job to `POST {base}/videos`; 9router
  answers that with its HTML shell (404). Its 176 `videoInput` models *read* a video,
  which is a different job. This was added first and reverted after the live test
  returned the HTML 404 — reading the capability counts alone was not enough.

**Blocked, not done: every completion returns `401 Missing API key`.** Verified by
calling `/v1/chat/completions` for seven models across five families, and again
through the app's own route (`POST /ai/provider-categories/{c}/test`): text and
vision answer `HTTP 401 Missing API key`. The catalog lists models; the route will
not serve them without a key, and no key exists on this machine (`9router show`
reports `(none)` in every tool). `test_ok` therefore stays unset — the state is
*configured, awaiting a key*, never *working*.

**The unblock is one owner action:** `9router login` (it prompts for the dashboard
password and creates/prints a key). Then the key goes into the keychain through
`provider_categories.store_key(category, secret)` and each tab's real test proves it.

Suite after the change: **3826 passed, 30 skipped, 1 warning**.

## Iteration 88b — duplicate route registrations removed (2026-11-14)

FastAPI warned at startup about duplicate operation ids. Four paths were registered
twice for the same method — `/meetings/prepare` (POST), `/calls/availability` (GET),
`/contacts/{name}/call` (POST) and `/contacts/{name}/call-notes` (POST) — plus two
earlier duplicates (`/contacts/{name}/outreach`, `/meetings/{uid}/follow-up`) that
were shadowing each other. Starlette matches the **first** registration, so the later
copies were dead code that still ran at import time.

- **Two were byte-identical** (`/calls/availability`, `/contacts/{name}/call`,
  `/contacts/{name}/call-notes`): deleted outright.
- **The divergent ones had to be checked, not guessed.** For `/meetings/prepare` the
  later copy appeared richer (it referenced `_join_url` and `_meeting_conflict`), which
  suggested deleting it would lose `join_url`/`conflict`. That was wrong: the
  **serving** copy at the first registration uses both helpers too. A first probe
  returned the early "no meeting" shape and looked like evidence to the contrary; a
  read of the serving body settled it. The lesson is in the order of checks — read the
  serving handler, do not infer it from a diff of line ranges.

**Evidence.** No `(path, method)` duplicates remain (checked against the live route
table, not by eye). No duplicate-operation warnings at import. After the change:
`GET /calls/availability` 200, `POST /contacts/x/call-notes` 200,
`POST /meetings/prepare` 200, and `test_api_contract.py` +
`test_r85_macos_screen_models_match_the_server.py` → **29 passed, 4 skipped**.

The README publishes the route count and a test guards it: removing the 6 duplicate
registrations moved it 228 → 222, and `test_phase25_desktop_truth.py` failed until the
line was corrected. That drop matching the deletion count exactly is the cross-check
that the removal was scoped to the duplicates and nothing else.

## Iteration 88 — every remaining suite failure, classified and fixed (2026-11-14)

The suite had 9–13 failures depending on what was installed. Each was traced to a
cause and closed:

- **6 voice tests** — no audio backend. Installed `ffmpeg`; `test_voice.py` +
  `test_phase91_voice_agent_integration.py` now **91 passed, 1 skipped**.
- **`dist/` missing** — `build_app.py` had never run on this clone. Built it;
  `test_phase96_release_preparation.py` **6 passed, 1 skipped**.
- **README published a stale count** — now 3856 (was 3850). `test_r108` **3 passed**.
- **`test_r85` task-steps** — asserted a non-empty `/tasks`, but a fresh data dir has
  no workflow job. It now skips on empty data (as every sibling check in that file
  already did) and still runs the real check when a job exists: with a job created
  through `/coding/tasks`, the file is **19 passed, 4 skipped**.
- **`test_r96` harness (9 checks failing)** — two hardcoded copies of the old path
  (`~/AppData/vantage/jarvis-forge`, which does not exist here). `findProjectDir` is
  now `static` and exposed as `BackendManager.resolvedProjectDir()`, and the harness
  asks it instead of naming a path by hand. **9 passed.**

**A false lead worth recording.** The harness also reported "concurrent action →
exactly one runs" as failing with `busy=0 ran=2`. The busy guard was correct: tested
in isolation against the real actor, two overlapping `perform()` calls return
`busy` and `ran`. The real cause was the missing path — both calls threw
`scriptMissing`, and the harness's `catch { return true }` counted that as "ran".
A test that treats "any error" as "it ran" cannot distinguish a broken fixture from
a broken guard; fixing the fixture is what made the check meaningful.

## Iteration 87 — E11: the app's conversations are listable and resume (2026-11-14)

**The gap.** `GET /ai/chats` read `ai_chats` only. The macOS app speaks `/command`,
whose turns live in `messages` keyed by `session_id` — and nothing listed those
sessions. So a conversation held in the app existed on the server and was
unreachable from any client that lists conversations, including the dashboard's
History panel. The earlier note that "the id was never lost, only unlistable" was
true for the dashboard; for the app no id was ever created.

**Fixed.**
- `ConversationService.list_conversations()` merges both stores on the id, names a
  pipeline session by its opening line, and reports `surface` (`chat` / `pipeline`
  / `both`) plus a `message_count` that counts both stores' turns.
- `_log_message` registers each pipeline session as a chat row, so it has an
  identity a listing route can return.
- `GET /ai/chats/{id}/messages` reads through the shared service, so a listed
  pipeline conversation opens with its real turns instead of empty.
- `DELETE /ai/chats/{id}` also deletes the `messages` rows. Without that the
  conversation reappeared on the next read — a false success.
- The dashboard sends the conversation id as `session_id` too, so continuing a
  pipeline conversation does not fork it.

**Evidence.** 6 new tests in `tests/test_r22_shared_conversation.py` (25 pass in the
file). Red-proven: with the source changes stashed, 4 of them fail; with the fix,
all pass. Live against the running server, a `/command` turn appeared in
`/ai/chats` as `surface=pipeline, msgs=2, title="what is the date"`, resumed with
both turns, and deleted cleanly (`listed before: 1`, `listed after: 0`).

**Found while testing.** The `messages` table has `created_at`, not `timestamp`;
the first listing query used the wrong column and the swallowed exception made
every pipeline conversation report `msgs=0`. The `surface` field also needed to be
derived from whether turns exist, not from whether an `ai_chats` row exists —
registering a session creates that row, so its presence proves nothing.

## Iteration 86 — data directory on a fresh clone (2026-11-14)

Cloning into a second location exposed a first-run defect the suite could not see,
because the suite always sets an absolute `JARVIS_HOME` (`tests/conftest.py`).

- **`JARVIS_HOME=~/.jarvis-forge` was not expanded.** `Settings.home` is populated
  from the environment by pydantic-settings, so `Field(default_factory=_default_home)`
  never ran and the tilde stayed literal; `wizard.OnboardingManager` did not expand
  either. On a real first run the token, SQLite database and onboarding state were
  created in a directory named `~` **inside the checkout**, hidden by `.gitignore`'s
  `*~` backup rule, while the real `~/.jarvis-forge` never existed.
- **Fixed** with one expanding resolver, `jarvis.config.data_home()`, used by
  `Settings` (a `mode="before"` validator on `home`), `llm_file_path`,
  `gateway.selection.selection_path` and `OnboardingManager`.
- **Evidence.** Red-proven behaviourally before the fix (`Settings.home` →
  `~/.jarvis-forge` literal; `OnboardingManager().state_dir` likewise), green after.
  Pinned by `tests/test_env_example_is_loadable.py::test_a_tilde_home_from_the_example_lands_in_the_real_home`.
  86 tests across the five home-resolution files pass.
- **Runtime-verified on the fixed tree.** The server, started from the repo, read its
  token from the real `~/.jarvis-forge/api_token` and answered `/health` with
  `status: ok`, 181 tools — no `~` directory was created in the checkout.

## Next action

1. **E11 continuity, remaining slice:** unify the macOS app's conversation store
   (`messages`, via `/command`) with the dashboard's (`ai_messages`, via
   `/ai/chat/send`). The dashboard slice is done (2026-11-13: `GET /ai/chats`
   listing, resume-on-load, History panel, text-safe rendering). Unifying the two
   stores is an architecture change, deliberately left as the next slice.
2. **Google live OAuth** needs `JARVIS_GOOGLE_CLIENT_ID` (Google Cloud → OAuth client of type
   "Desktop app"). Until it is set the backend says `not_configured` and prints the exact
   steps; the connect/exchange code path is mock-tested but not live-tested.
3. **Live MCP** needs a real hosted MCP endpoint; the fake-server path (save → discover →
   allowlist refusal-before-network → allowed call) is verified.
4. **Android device runtime** needs an emulator or phone; the build and unit tests pass here.
5. Owner decision, unchanged: whether to commit the untracked `macos/`, `android/`,
   `jarvis/companion/` trees (R0.3).

Everything else the 2026-10-27 run left open is closed — see the 2026-10-28 update at the
end of `docs/PROJECT_STATUS.md` (the stale :8765 backend was migrated to a current instance,
one listener, LAN binding preserved).

## Where the project actually is

- **Backend**: FastAPI on `127.0.0.1:8765` (LAN mode on → binds `0.0.0.0`).
  154 tools, 15 agents. The conversational path works with the configured
  provider (9router → `ag/gemini-2.5-flash`).
- **macOS app**: `macos/Vantage/`. Builds clean in release, bundles and
  signs. The Command Center is a real conversation.
- **Dashboard**: five pages, all driven in a real browser, all clean. The
  provider page is now built from the server's registry. The browser tests
  now run against an **isolated server on a scratch home** — they used to
  drive the developer's live backend, and wrote into the user's calendar.
- **Phone companion**: the web page and the Android app both work; the
  self-approval guard is enforced on both paths.
- **Email workflow**: find/search by term, read by name, summarise, and
  **conversation continuity** work end to end through the conversational
  pipeline (iterations 78–79). A follow-up ("read that one", "the second
  one", "what did they say in that message?", "reply to that one") resolves
  against a per-session selection — isolated, honest when empty, never a
  guessed read. A mail account (IMAP/SMTP) is not configured on this
  machine, so the verified backend is the local maildir; the IMAP/SMTP
  provider is the same code path behind the credential boundary. The mail
  providers implement **no** archive/label/move operations, so there is no
  "organize" capability to advertise — only draft + gated send. As of
  iteration 81 the file routes **refuse to treat an email as a file**:
  "archive that email" / "move my invoice email to the trash" /
  "delete my invoice email" now get an honest "the mail backend has no such
  operation" instead of a file mutation (was `compress_path("that email")`).
- **Suite** (reconciled from `--collect-only`, 2026-10-23 loop): **3320
  collected** total. Non-OCR subset = **3239 collected** (3320 − 81); those 5
  OCR/browser files = **81 collected**. Uncontended results: non-OCR →
  **3217 passed / 22 skipped / 0 failed** (272 s); the 5 OCR/browser files →
  **81 passed / 0 failed in true isolation** (29 s). So the full 3320-test
  suite passes when the OCR cluster runs uncontended. A _combined_ full run is
  environmentally unstable here (contention trips OCR timeouts; one run was
  SIGKILL'd/137 at 93%) — an environment limitation, not failing tests. (Earlier
  headlines here quoted a "3286 collected" total and mixed full-run totals from
  different test counts; those were wrong. This is the corrected, collection-
  verified reconciliation. Run the non-OCR and OCR subsets separately,
  uncontended — never a combined full run with other load.)
- **Capability registry** (R6.4): the safe "give it a real source of truth"
  half is done. `CapabilityRegistry.capability_report` reports each capability
  as **verified** (a real completion recorded in `route_health`), **declared**
  (a static profile names it, unproven), or **unknown**, surfaced in
  `GET /ai/providers/{id}/health` and rendered in the dashboard provider page.
  It is a **report, not a request gate** — `can_handle_request` is unchanged
  and `check_ai_capability`/`find_compatible_providers` are still uncalled
  (r88 pins the gate stays dead; r94 pins the report is live). Wiring it as a
  gate, or deleting the module / the owner's `.multimodal` copy, remains an
  owner decision.
- **Android**: build + 48 unit tests verified (JDK 17, Gradle 8.11.1, AGP
  8.7.3, Kotlin 2.1.0, SDK 34). **Device-level runtime is blocked** — no
  emulator, no system-images, `adb devices` empty. APK builds and is
  verified (`com.vantage.companion`); startup/API behaviour not yet observed.
- **Git**: 283 files uncommitted, most of them the owner's. **R0.3 — whether
  to commit the untracked `macos/`, `android/`, `jarvis/companion/` trees —
  is an owner decision. Do not take it.**

## Completed this iteration (82)

**Test counts reconciled (my earlier arithmetic was wrong).** `--collect-only`
gives the real numbers: full suite **3320 collected**; non-OCR subset (5 files
excluded) **3239**; those 5 OCR/browser files **81** (3239 + 81 = 3320).
Uncontended: non-OCR **3222 passed / 22 skipped / 0 failed** (301 s, including
the 3 new r96 tests + 2 calendar-title tests); the 5 OCR/browser files **81/81 pass as whole
files in true isolation** (29 s). The old "3286 collected" claims were a guess
from a killed run's progress — corrected here and in the checkpoint. A
_combined_ full run is environmentally unstable (contention → OCR timeouts) —
run the two subsets separately.

**macOS connection lifecycle proven end to end (XCTest cannot run here).** Added
a production-safe seam: `BackendManager.backendBaseURL` and `APIClient.baseURL`
honour an internal `JARVIS_BACKEND_PORT` env override — production default stays
**8765**, no user-facing port setting. The health fetcher now derives its URL
from `backendURL` (removing a hard-coded-8765 duplication).
`macos/Vantage/IntegrationHarness/run_lifecycle_check.sh` compiles the REAL
BackendManager/APIClient/APIModels and drives them against a real local backend:
**12/12 checks** — connect→running, down→truthful error, recovery→running+refresh,
repeat-connect does NOT multiply the poll loop, 401→authorization error (not
"unreachable"), shutdown clears the poll task.

**Command Center proven end to end.** `run_command_center_check.sh` drives the
REAL `ConversationService` + `APIClient` (what CommandCenterView uses) against a
stub backend: **17/17 checks** — model answer shown/attributed, session adopted
for follow-ups, provider-failure/auth-failure/malformed each become an honest
visible error (no fake success), recovery works, failures stay visible. Also
live-verified: `get_battery_status` executes with `verification: success`; a
`create_event` returns `confirm` with no actions, and denying it created
nothing. Both harnesses are gated by `tests/test_r96_macos_integration_harness.py`.
Bundle rebuilt with both seams; codesign VALID.

**Noted, not fixed (pre-existing, separate):** "schedule a meeting called X"
keeps "called" in the proposed title ("called X") — harmless in the deny test
but a real calendar-title quirk. Recorded for owner prioritization.

## Completed this iteration (81)

**macOS connection lifecycle — verified in the running app.** Rebuilt from
current source (`make -C macos/Vantage app`, 11.99 s; codesign VALID; bundle
holds the current executable). Launched via `open -a` against the live 8765
backend (the app is hardcoded to 8765, so it **attaches** — the live,
user-owned backend was never touched). Observed in the real app: attach with
**no duplicate spawn** (exactly one 8765 listener), authenticated connect + a
**single** 30 s poll loop (access log: `checkBackend` 401 → `/health` 200s),
data refresh (fetched `/ai/providers/*/health`), and **clean shutdown that
left the live backend up and healthy**. The failure→recovery state transitions
are unit-pinned in `BackendManagerStateTests.swift` but **XCTest cannot run
here** (CLT only), and the live backend was not taken down — so those
transitions are source-verified + unit-pinned, not runtime-observed. No
defect found in the state machine. Window existence could not be
re-confirmed (no accessibility permission — the known environment limit).

**Command Center is a real conversational assistant — verified live.** General
questions get a real model answer (`intent=unknown`, not misrouted to a tool);
a same-`session_id` follow-up resolved "that" to the prior subject (multi-turn
history works); malformed JSON → clean 422. The macOS client adopts the
returned `session_id` (the mechanism that made the follow-up retain context)
and distinguishes model answers from lookups, with timeout → error and Retry.

**Defect found and fixed: oversized `/command` input returned HTTP 500, not 422.** The `MAX_COMMAND_CHARS` (20 000) cap lived only on `CommandRequest`,
which the route builds _after_ FastAPI validates the body — so a 50k-char
paste passed the body model and raised inside the route (500). Enforced on
`CommandBody` (`jarvis/api/schemas.py`), the HTTP boundary that owns the
contract → clean 422 with the same message. 2 regression tests in
`tests/test_api.py`. (The live backend still runs pre-fix code — it is the
user's process and was not restarted; verified against current source.)

**Defect found and fixed: an email request was a file operation.**
"archive that email" → `compress_path("that email")`, "move my invoice email
to the trash" → `move_file`, "copy that email to notes" → `copy_file`,
"delete my invoice email" → `delete_file` — a person approving them would
have moved/deleted a file. This is the same cross-domain misroute the
calendar deferral guards (iteration 77). `jarvis/core/planner.py` now defers
the file routes when the subject names an email message (and is not itself a
file or a container like "the emails folder"), answering honestly: "I can't
archive a message — this mail backend has no such operation. I can search,
read, summarise, or draft a reply." This **delivers R6.4c option (b)** (the
honest answer) rather than a mutation. 12 regression tests in
`tests/test_r95_an_email_is_not_a_file.py`; real files (`archive that folder`,
`move notes.txt`, `move the emails folder`, `delete old.txt`) still work.

**Full suite: 3281 passed / 22 skipped / 0 failed (348.21 s, exit 0)** — the
3267 baseline + 14 new tests. (An earlier 605 s run showed 7 OCR/attachment
failures — resource contention while the app was running for lifecycle
verification; re-run in isolation they pass in 13.36 s. Not a regression.)

## Completed this iteration (80)

**R6.4 — the capability registry got a real source of truth (report, not gate).**
Iteration 78 pinned the registry as _dead and dangerous_: as a request gate it
would refuse a working provider (`can_handle_request("9router", …, [TEXT]) →
False`). The owner-gated choice was "give it a real source of truth, or
delete it." This iteration took the **give-it-a-source** half — the safe one —
without turning it into a gate.

Findings (evidence): the registry only knows a capability if told, and nothing
in the live path tells it, so it judges the working 9Router model incapable —
which is _correct and pinned_ (r88), and exactly why a gate on it would refuse
real requests. Two real sources of truth exist: the adapters' `SUPPORTED_MODELS`
(declared) and the `route_health` ledger (verified — a real `chat()` success per
provider/model, recorded on every completion).

Implemented:

- `jarvis/security/providers.py` — `CapabilityRegistry.capability_report(provider,
model)`: each capability is **verified** (a completion succeeded, read from
  `route_health`; only `text` is derivable from a completion — nothing else is
  promoted), **declared** (a static profile names it, incl. the
  `supports_tools`/`supports_streaming`/`context_window` fields, unproven), or
  **unknown**; plus `last_success_at`. `can_handle_request` is **unchanged** —
  still the conservative declared-only check, so nothing is tempted to wire it
  back up as a request decision.
- `jarvis/api/app.py` — `GET /ai/providers/{id}/health` (already polled by the
  dashboard provider page) returns `capability` (the tier map) and
  `capability_verified` (the verified list). A report, not a gate: the GET never
  blocks or refuses a request based on it.
- `dashboard/ai_connections.html` — the card health box now shows
  `verified: <caps>` when a capability is verified, so the page displays real
  evidence instead of only the route state; empty until the model has actually
  answered, so it never shows a claim with nothing behind it.

Verified at three levels: unit (r94, 8 report/gate tests), runtime (a recorded
completion → `/health` returns `capability_verified == ["text"]`, TestClient),
and browser (a Playwright render test against the **isolated server** confirms
the page shows "verified: text"). r88's gate-stays-dead assertions and r94's
report-is-live assertions hold together (r88's docstring now cross-references
r94). **Full suite: 3267 passed / 22 skipped / 0 failed (304.29 s).**

**Android — everything possible without hardware, done and verified.**
Toolchain confirmed present/consistent: Gradle 8.11.1 (wrapper), AGP 8.7.3,
Kotlin 2.1.0, JDK 17, SDK platform 34 + build-tools 34.0.0 (documented in
`docs/TESTING.md`).

- **Unit tests: 48 passed / 0 failures / 0 errors / 0 skipped** — measured from
  the JUnit XML, not Gradle's exit code. `./gradlew testDebugUnitTest --rerun-tasks` exit 0.
- **APK builds**: `./gradlew :app:assembleDebug` exit 0; aapt2 confirms
  `com.vantage.companion`, launcher `MainActivity`, compileSdk 34 (18.9 MB).
- **Device-level runtime: BLOCKED (external)** — `adb devices` empty, no
  emulator binary, no system-images. Reported as a real dependency, not fabricated.
- Client error handling inspected and already sound (OkHttp 15 s connect /
  30 s read+write timeouts, SHA-256 cert pinning, `Call.await` exception path);
  no clearly-required gap, no fake fix invented.

## Completed this iteration (79)

**A follow-up could not point at the message the conversation just showed.**
_"summarize that one"_, _"read the second one"_, _"what did they say in that
message?"_ — measured in the running orchestrator, each answered "I
understood the words but not the goal." The pipeline had no memory of the
previous turn, and _"reply to that one"_ had no path at all.

Fixed with a **per-session selection** in a new `email_selections` table
(keyed by the same `session_id` the messages, confirmations and audit rows
use — so one conversation's selection is never reachable from another, and a
fresh session starts with nothing). Only a uid, subject and sender are
stored; the body is always re-read through the authorized read path.
`jarvis/core/email_context.py` is the thin access layer (no module global).

The planner carries a demonstrative as a `referent` (the read branch, and a
**reply-to-selected** branch that drafts to the sender — draft only, the
send gate is untouched). The orchestrator resolves it against the session's
own row _before_ anything else: a unique pick reads that uid; an ordinal
picks from the list the reply showed, in reply order; a several-match with
no pick asks which (numbered); an out-of-range ordinal, a missing selection,
a provider switch, and a deleted message are each reported honestly — never
a guessed read and never another session's message. The chat surface
(`propose_and_execute`) resolves referents the same way instead of asking
the model to guess.

The read reply now carries a 300-char body excerpt, so "what did they say in
that message?" returns the actual words, not just the subject line.

**The mail providers implement no archive/label/move/flag operations**, so
there is no "organize" capability to add without inventing an endpoint — the
supported write surface (draft + gated send) is now fully conversational.

Measured in the running orchestrator against a seeded local maildir:

```
find my email about the Q3 report  -> search_email(query='Q3 report')
read that one / summarize it /
what did they say in that message? -> read_email(uid=<selected>)
   reply: 'Q3 report due — from boss@example.com. "Please send the Q3 …"'
find my emails about update (2)    -> list kept, nothing selected
read the second one                -> read_email(uid=<2nd, in reply order>)
read the fifth one                 -> "That list had 2 message(s); …"
summarize that one (fresh session) -> "no email in this conversation…"
A/B interleaved "read that one"    -> each resolves its own message
reply to that one                  -> draft_email(to=<sender>), not sent
reply to that one saying thanks…   -> the body is drafted verbatim
read that one (message deleted)    -> "I could not find message …"
```

**Two pre-existing r85 `SessionInfo` failures surfaced** and were fixed the
way the file already fixes `/audit`: that test pins the Swift model against a
**live** `GET /auth/sessions`, which lists only scoped (phone) sessions with
an 8-hour TTL. No phone was connected → `200 []` → the assert-on-nonempty
fired. Not a regression (verified the endpoint answers 200 with a legitimate
empty list; the diff touches none of auth/sessions). It now skips when there
are no rows to decode-check — the model stays pinned by `_declared_fields`.

## Completed this iteration (78)

**The email find step did not search — it listed.** _"find emails about the
invoice"_ was planned as `search_email(query="")`, and an empty query means
_recent messages_ to the provider, so the first step of the email workflow
answered with the last few messages whatever they were about. Only
end-anchored `from <name>` and `unread` survived. The route now extracts the
term (`RulePlanner._email_search_term`): the words that say _what_ are the
query, and a request that names no thing still lists the mailbox.

**A named message could not be read.** `read_email` needs a `uid` and no
sentence could reach it (it was on the Phase 28 unreachable ledger).
`"read the invoice email"` now carries the name into the iter-77 identifier
mechanism (`search_email`'s `lookup`): exactly one match is read, several
matches are asked about with both named, a miss says "there is nothing of
that kind right now", and nothing is ever read on a guess. `read_email`
left the unreachable ledger 35 → 34 — the on-purpose move that test's
docstring anticipates (a _read_ is the category it keeps reachable).

**The reply now names what it found.** _"I found 1 message(s)"_ did not let
the person identify the message. Search and summarise replies now carry
subject + sender (first 3, `+N more`), and the read-side tools report
`verified=True` — the read is the confirmation, per `ok()`'s own rule and
the calendar-side `search_events`. The `…., but I could not verify`
double-period in replies is gone.

Measured in the running orchestrator against a seeded maildir:

```
"find emails about the invoice"      -> search_email(query='invoice')
  "I found 1 message(s) matching 'invoice'. Invoice 2026-09 from billing@…"
"read the invoice email"             -> read_email(uid=<resolved>)
  "Invoice 2026-09 — from billing@example.com"
"read the update email" (two match)  -> clarification naming both, no read
"read the zebra email" (none match)  -> "…nothing of that kind right now."
"check my email"                     -> search_email(query='')  (unchanged)
"what new emails did I get"          -> search_email(query='', unread_only=True)
```

**Two pre-existing failures surfaced on the first full run of 2026-10-23**,
and were fixed deliberately: a date-rollover test in `test_r89` (it pinned
`2026-10-23` as "tomorrow" — true only on 2026-10-22; now computed from
`now + 1 day`) and the Phase 28 ledger (35 → 34, because of the new read
route above, documented in the test). Full suite after the fixes:
**3227 passed / 20 skipped / 0 failed**.

**A calendar request was planned as a file rename.** _"move my Design Review
event to tomorrow at 3pm"_ produced

```
move_file(source="my Design Review event", destination="tomorrow at 3pm")
```

The model planner was shown all 137 proposable tools — about 17,000
characters of one-per-line definitions — and asked to pick. It picked the
wrong _domain_, and the person would have spent their approval renaming a
file. Three fixes, in order:

1. `jarvis/core/domain.py` — a keyword domain detector. The planner is now
   shown only the tools tagged with the domain the sentence names
   (`ModelPlanner._catalogue(domain)`). A sentence that names no domain, or
   more than one, gets the full catalogue: no narrowing only means a big
   prompt, while a wrong narrowing hides the right tool. With only the
   calendar tools visible, the same sentence came back as `search_events` —
   the right first step.
2. `jarvis/core/planner.py` — the file-move rule now **defers** when the
   subject names a calendar thing, and it defers only for a subject that is
   not itself a file, so `event-log.txt` still moves.
3. `jarvis/core/planner.py` — a calendar sentence the rules cannot act on
   declines with `reply_kind="not_understood"` instead of a flat
   "declined", so the orchestrator routes it to the model.

**The time the person said was not the time that was used.** The calendar
tool read `2026-10-23 9:00 AM` as **10:00** — the parser took the `10` out
of the _date_ as the hour. A new `jarvis/integrations/natural_time.py`
parses ordinary English ("tomorrow at 3pm", "next Friday", "in two hours",
"tonight at 8", "noon") and `calendar_provider.parse_datetime` falls
through to it **last**, so an explicit format always wins. Measured: 1 of 12
natural phrases parsed before, 12 of 12 after. The planner's system prompt
now states the format the tools read (`YYYY-MM-DD HH:MM`, 24-hour) and
carries the current date and time, so a relative time is resolved by the
planner instead of being sent as a phrase the tool cannot read.

**The name in the sentence did not find the thing it named.** _"cancel my
Design Review event"_ asked which event was meant and then answered its
own question with _"I checked, and there is nothing of that kind right
now"_ — because the identifier source (`list_events`) was run with **no
arguments**, and no arguments means _today_, and the event was six days
out. Now:

- an identifier source may declare which of its parameters takes the name
  to search for (`provides_identifiers[...]["lookup"]`); `list_events` gained
  a `query` parameter that searches the whole calendar;
- when the lookup returns **exactly one** row matching the name, the plan is
  **completed** rather than asked about — and the completion still goes
  through the permission gate, so a cancellation still waits for approval;
- when several rows match, the question still stands: a name that matches
  nine events identifies none of them.

Verified live: _"cancel my Design Review event"_ now raises a confirmation
for `uid=EVT-5618e9463852` and nothing else. The event was **denied** and
is still in the calendar.

**The test suite was writing into the user's calendar.** The browser tests
drove the real dashboard page against `http://127.0.0.1:8765` — the
developer's _live_ backend, against the real `~/.jarvis-forge` data
directory. The approval test raises _"schedule a meeting called Browser
Check"_, waits for the card, and clicks **Approve**, which runs a real
`create_event`. Measured on this machine after a few runs:

```
 10  called Browser Check      5  Busy        1  Standup
  5  Searchable sync           5  Tool test   1  Standup2
  5  Renamed                                1  Design Review
```

Thirty-six test events in the user's data, and the number grew every run.
`tests/isolated_server.py` now starts a **real uvicorn on a real port with
a real empty data directory** — the page, the session bootstrap and the
approval round trip are all exercised for real, against a calendar that is
thrown away with the test home. The 36 events were removed from
`~/.jarvis-forge/jarvis.db` after a consistent SQLite backup was taken to
`~/.jarvis-forge/jarvis.db.before-calendar-cleanup-2026-10-22`.

**The model-planner path was dead, and the conversation hid it.**
`llm_fast_model` and `llm_reasoning_model` default to OpenAI names
(`gpt-4o-mini`, `o4-mini`). They are hints about which model suits which
task, not a description of what the configured vendor offers. Pointed at
9Router, the router sent `o4-mini` to a gateway that has never heard of it:

```
{"error":{"message":"No active credentials for provider: openai",
          "code":"model_not_found"}}
```

The failure was invisible on the conversational path, because `conversation`
routes to `general` — the one role whose model _is_ the configured one. The
planner routes to `reasoning`, so **every model-proposed action died at the
first request**. Measured, before the fix:

```
POST /command "set a reminder for tomorrow at 9am to stretch my hamstrings"
  -> reply:    "I can set that up for you! I'll use the **Reminders app** …"
  -> decision: None      -> plan.planner: "rules"   -> actions: []
  -> pending confirmations: 0
```

The assistant promised an action, the user said "yes please go ahead", and
nothing happened and nothing was asked for approval. Now:

```
-> decision: CONFIRM   -> needs_confirmation: True   -> plan.planner: llm
-> "This needs your confirmation before I run it. … Reply with /confirm …"
```

Fixed in `jarvis/gateway/router.py`: a role model the vendor cannot serve is
never called (the vendor's own catalogue when it publishes one; the
route-health ledger's own record of what that gateway already refused when
it does not), and a refusal _of the model name_ is retried once on the
configured model. A timeout or a rate limit is **not** retried on another
model — that is a statement about the route at that moment.

**The provider page described a provider that does not exist.**
`dashboard/ai_connections.html` hardcoded three cards — `openai`, `gemini`
and `shree` — while the server's registry has five (`openai`, `gemini`,
`groq`, `9router`, `openai_compatible`). So the page offered "Shree AI",
which appears nowhere in the backend (`POST /ai/providers/shree/test` answers
404), and **9Router — the provider actually in use — had no card at all**.

**The provider dropdown could not be changed.** The page re-read
`/ai/selection` on the dropdown's `change` event, and the server's answer set
the dropdown back to the _current_ provider: picking "gemini" snapped
straight back to "9router", so "Save selection" would have saved the old
provider.

**Three checkboxes did nothing.** "Use free/plan allowance", "Allow paid API
fallback" and "Require confirmation before paid fallback" had no handler and
no setting behind them — the only code that reads `free_first` and
`allow_paid_fallback` (`find_compatible_providers`) is not called from
anywhere in the live path. They are gone, not left pretending.

The page is now built from `GET /ai/providers`, so a provider added to the
backend appears without a second edit that can be forgotten.

**The app's other screens had the same class of defect as `/health`.**
Every screen's Swift model declared field names the backend had never sent,
and the failure was silent because the caller catches it and shows an empty
list.

| model         | declared                                 | the server sends                                               | effect                                                                                     |
| ------------- | ---------------------------------------- | -------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| `AuditEntry`  | `detail: String?`                        | `detail` is an **object**                                      | the **whole `/audit` array failed to decode** — the Activity screen's audit list was empty |
| `AuditEntry`  | `action`, `timestamp`, `id`, `device_id` | `intent`, `tool`, `created_at`, `log_id`, `device`             | every row read nil and showed "Unknown"                                                    |
| `AuditEntry`  | results `allowed`/`denied`/`error`       | `success`/`denied`/`error`/`awaiting_confirmation`/`no_action` | **every** row, including the successful ones, rendered a neutral grey circle               |
| `Device`      | `device_name`, `device_type`, `status`   | `type`, `owner`, `platform`, `trust_level`, `online`           | every device's type showed as "unknown"                                                    |
| `Device`      | icons for `phone`/`watch`/`car`/`home`   | `computer`/`android`/`smartwatch`/`vehicle`/`home_hub`         | every device got the same laptop icon                                                      |
| `TaskInfo`    | `name`                                   | `goal`                                                         | every job showed its job id instead of what it was for                                     |
| `TaskInfo`    | `steps` in the list payload              | `steps` are in `GET /tasks/{job_id}`                           | the "Steps" heading could never appear                                                     |
| `ProjectInfo` | `id`                                     | `project_id`                                                   | identity was a fresh UUID each load, so SwiftUI's diffing saw the whole list as changed    |
| `SessionInfo` | `ref`, `created_at`                      | `session_ref`, `expires_in`                                    | "Revoke" sent `DELETE /auth/sessions/` with an **empty** path and reported success         |

The task detail sheet now fetches the job's own detail route, where the
steps live. The session revoke refuses to report success when it has no
reference to send.

**iOS: reported, and left paused.** `ios/VantageAI` **does not compile** —
`swift build` exits 1 with 23 error lines across four files: an
`Equatable` conformance on an enum carrying a non-`Equatable` payload; a
`browseResultsChanged` member that has never existed on `NWBrowser`; four
`post(_:body:)` calls that cannot infer a generic parameter; a `catch` that
binds `error` and then assigns to it; and an iOS-only
`navigationBarTitleDisplayMode` in a package that declares macOS. Its models
have the same contract drift the macOS app had: `security` and `privacy` as
`[String: String]?` where the server sends mixed-type maps, `peripherals` as
a `String?` where it sends a map, and a required `id` on both `Confirmation`
and `DeviceInfo` that the server never sends.

Per the project's own instructions ("preserve the sources; do not make iOS
work part of the project"), this is **recorded, not fixed**. The state is
pinned in `tests/test_r86_ios_package_state.py` so it cannot drift
unnoticed and so a future fix is noticed.

**Android: the build and the tests are real.** `android/build_apk.sh` with
JDK 17 and the Android SDK **builds the APK**:
`com.vantage.companion`, label "Vantage", launcher
`com.vantage.companion.MainActivity`, 18 MB, promoted to
`dist/Vantage-1.0-android-debug.apk` with a provenance manifest and its
SHA-256. The unit tests are **48 passed, 0 failures** (measured from the
JUnit XML, not from Gradle's exit code). Both known Android defects from
the defect register are **fixed**: the client redeems through
`POST /companion/pairing-redeem` (not the legacy `/phone` bootstrap), and
`PinnedCertificateTrustManager` pins the gateway's certificate by its
SHA-256 fingerprint, with its own unit tests. Its models are also correct
against the live responses.

**The runtime is not verified, and this says so.** `adb devices` lists
nothing, and this environment's SDK has no `emulator` binary and no system
images, so an emulator cannot be started. The build and the 48 tests are
real; nobody has watched the app run. Pinned in
`tests/test_r87_android_companion_state.py`.

## Verified in the running application

`swift build -c release` → `assemble_app.sh release` → `codesign --verify
--strict` VALID → `open -a` → a real 882x562 window, polling `/health`.

From the app's own code, against the live backend:

```
HEALTH decoded: status=ok tools=154 llm_enabled=Optional(true) ai_provider=Optional("9router") paired=Optional(6)
aiAvailable=true provider=9router
T1 model=9router:ag/gemini-2.5-flash isModel=true ms=3874
T2 model=9router:ag/gemini-2.5-flash session=SESS-1c1dc1386d5e
TOOL intent=laptop_battery decision=EXECUTE model=rules
GATE needs_confirmation=true decision=CONFIRM
```

From the live backend, after the planner fix:

```
POST /command "set a reminder for tomorrow at 9am to stretch my hamstrings"
  -> decision=CONFIRM needs_confirmation=True plan.planner=llm
  -> confirmation CONF-00369a…  tool=create_event  risk=2
POST /confirm/CONF-00369a…?approve=false
  -> "Understood - cancelled. Nothing was changed."  decision=BLOCK
  -> pending confirmations: 0        (no calendar event was created)
audit: model_proposal -> awaiting_confirmation -> denied
```

From the browser, the provider page:

```
providers-count: 5 providers the server knows
cards: provider-openai provider-gemini provider-groq provider-9router provider-openai_compatible
badges: 9router-api-status=Key stored   (all others: Not connected)
'shree' on the page: 0
after picking gemini, dropdown = gemini      (it used to snap back)
save button visible BEFORE a test: False
save button visible after a failed test: False
key round trip: configured false -> true -> false;  the key never appears in a response body
```

From the running app, the screens that were showing nothing:

```
artifacts/activity-audit.png   12 audit rows: create_event, schedule a meeting, set a reminder …
                                with the right icon per outcome (green ✓ success, blue clock
                                awaiting confirmation, red ✗ denied, grey no-action)
artifacts/devices.png          8 devices, each with its own icon and its own type:
                                laptop_main/computer, phone_main/android, watch_main/smartwatch,
                                home_gateway/home_hub, car_main/vehicle
artifacts/memory.png           10 real memory rows with their scope badge and timestamp
artifacts/projects.png         the real projects (AMR, Sanjeevani) with their descriptions
artifacts/sessions.png         the real sessions, each with "expires in 6h" and a Revoke
```

The app bundle was rebuilt and re-signed after the fixes, the capture
harness was removed, and `strings` on the shipped binary confirms 0
occurrences of `ViewCapture|vantage_capture|SelfCheck|vantage_selfcheck`.
The app runs as one process and polls `/health`.

## Screenshots

`artifacts/ai-connections.png` (and `-light.png`),
`artifacts/activity-audit.png`, `artifacts/devices.png`,
`artifacts/command-center-{conversation,empty,approval,no-provider,failed}.png`,
`artifacts/{home,providers,approvals}.png`. The dashboard shots are real
browser captures; the macOS ones are captured by hosting the real view in a
real offscreen `NSWindow` — see the prompt file for why `ImageRenderer` and
`screencapture` do not work here.

**The capture technique, refined:** the data has to be in hand _before_ the
snapshot. A hosted view's async state change does not reach a cached
display bitmap in time, so the harness fetches the payload with a blocking
`URLSession` task (off the main thread, with a lock — `DispatchQueue.main.sync`
from a background thread deadlocks) and hands the array to the view. A
`List` is NSTableView-backed and does not draw its rows into a cached
bitmap, so the rows are laid out in a plain `VStack` — with the **real**
row views. The window needs a hard dark background and
`.preferredColorScheme(.dark)`, or `Color.primary` is white on a white
backdrop and the row reads as empty.

- **Two macOS bundles, by design — the DMG question is owner-gated, not a
  defect** (verified 2026-10-23). `dist/Vantage.app` is the **Tk launcher**
  (`com.jarvisforge.app`, exec `JarvisForge`, from `build_app.py`) and it is
  current with `app/launcher.py` (identical, codesign verifies). The **native
  SwiftUI app** is `macos/Vantage/Vantage.app` (`com.jarvisforge.vantage`, from
  `assemble_app.sh`). `installers/macos/build_dmg.sh` packages the Tk launcher.
  `docs/RELEASE_AND_BUILD.md` records "⚠ Owner decision (open): which macOS
  artifact should ship in the DMG" — so changing the pipeline is **owner-gated**;
  do not "fix" it autonomously. R1.5 (completed) covers the ANDROID pipeline only.
  Recorded as `R-release-macos-dmg-artifact` in the task queue.

## Next action, in order

1. **P1 — email "organize" via a REAL IMAP operation is owner-gated**
   (R6.4c, option a). The **honest-answer half is DONE** (iteration 81, r95):
   "archive/move/copy/delete that email" no longer become file mutations — the
   planner declines with "the mail backend has no such operation" (both local
   maildir and IMAP/SMTP implement search/read/summarise/draft/send only).
   Implementing a _real_ archive (IMAP `MOVE` to a folder, or `\Deleted` +
   `EXPUNGE`), gated behind confirmation because it changes the mailbox, is an
   owner decision (adds a new capability + confirmation surface). Do **not**
   invent an endpoint or a fake operation. Until then the honest answer stands.
2. **P1 — the capability registry's _gate_ half is owner-gated** (R6.4).
   The safe **reporting** half is DONE (iteration 80, r94): the registry
   reports verified capability from `route_health` in `/health`, rendered in
   the dashboard. The remaining choice — wire it as a request gate (dangerous:
   would refuse a working provider), or delete the module / the owner's
   `providers.py.multimodal` copy — is an architectural decision the owner
   must make. Not performed. `can_handle_request` stays the conservative
   declared-only check (r88); do not re-wire it as a gate.
3. **P1 — the Android app's device-level runtime is BLOCKED (external).**
   Build + 48 unit tests are real (iteration 80). No emulator binary, no
   system-images, `adb devices` empty — so startup/auth/API behaviour cannot
   be observed. Needs a physical device or an emulator + system image.
   When one exists: install the APK, test startup, auth, API calls,
   loading/error states. Do not fabricate an install result.
4. **DONE (iteration 82) — macOS failure→recovery, proven end to end.**
   `macos/Vantage/IntegrationHarness/run_lifecycle_check.sh` drives the REAL
   `BackendManager` against a real local backend (via the internal
   `JARVIS_BACKEND_PORT` seam; production 8765 unchanged): connect→running,
   backend down→truthful error, recovery→running+refresh, **no duplicate poll
   loop on repeated connect**, 401→authorization error (distinct from
   network), clean shutdown — **12/12**. The Command Center path is proven the
   same way (`run_command_center_check.sh`, REAL ConversationService/APIClient,
   **17/17**: model answer shown, session adopted, provider/auth/malformed
   errors honest). Both are gated by `tests/test_r96_macos_integration_harness.py`.
   What remains _unobserved_ is only the app's own GUI pixels (no accessibility
   permission here) and a real live stop/restart of the **user's** 8765 backend
   (which would need a window where it is down — an owner-coordinated step).
   The connection **logic** itself is now fully exercised.
5. **P2 — the `dist/` bundle** was rebuilt in iteration 76; rebuild if the
   changed dashboard assets (iteration 80's provider health box) must ship in
   it. It is a gitignored build artifact.
6. **P2 — live IMAP/SMTP end-to-end** for the email workflow needs the
   owner's mail account (host/user/password in settings). The local-maildir
   path is verified; the remote path is the same code behind the credential
   boundary. State it as an external prerequisite, not a defect.
   6b. **P3 — calendar title keeps "called" (noted iteration 82, pre-existing).**
   "schedule a meeting called LoopCheck" proposes the title **"called
   LoopCheck"** — the "called" is not stripped for the "schedule a meeting
   called X" phrasing (the "create a calendar event called X" path _does_
   strip it). Harmless (the action still needs confirmation), but a real
   title-quality quirk in the calendar-create route. Actionable now, low risk;
   fix by stripping the leading `(?:called|named|titled)\s+` from the captured
   title in `_calendar`'s create branch, with a regression test.
7. **DONE (iteration 79) — email conversation continuity (R6.4).** Per-session
   selection, demonstrative + ordinal resolution, reply-to-selected (draft),
   provider-switch and deletion honesty. Pinned in
   `tests/test_r93_a_follow_up_points_at_the_message_selected.py`. Build on
   `EmailContext` if a later feature needs "the message we just talked about".
8. **DONE (iteration 80) — capability registry source of truth (report)** +
   Android build/tests. Pinned in
   `tests/test_r94_verified_capability_report.py` (11 tests incl. a Playwright
   render test). Do not re-implement.
9. **DONE (iteration 81) — macOS lifecycle runtime-verified** (attach,
   single poll loop, clean shutdown preserving the live backend), **Command
   Center conversational path verified live** (general Q, multi-turn, 422),
   **oversized `/command` → 422** (was 500; `jarvis/api/schemas.py` +
   `tests/test_api.py`), and **email requests are not file operations** (r95;
   `jarvis/core/planner.py`). Full suite 3281 passed / 22 skipped / 0 failed.

## Do not repeat

- The dashboard pages were all driven in a browser (iteration 72), and the
  provider page in iteration 76. They are clean. Do not re-audit them.
  They now run against the **isolated server** (`tests/isolated_server.py`),
  not the developer's live backend — do not point them back at `:8765`.
- `mission_control.html` and `phone.html` were fixed in iteration 72. Their
  regressions are pinned in `tests/test_dashboard_pages_browser_flows.py`.
- The macOS Command Center's conversation was built in iteration 74. Its
  regressions are pinned in `tests/test_macos_conversation_client.py`.
- XCTest cannot run here. Do not try; the contract is pinned in Python.
- The role-model routing defect is fixed and pinned in
  `tests/test_r83_router_serves_a_model_the_vendor_has.py`. Do not
  re-investigate it.
- The iOS and Android states were measured in iteration 76 and are pinned in
  `tests/test_r86_ios_package_state.py` and
  `tests/test_r87_android_companion_state.py`. Do not re-audit them.
- The domain narrowing, the natural-time parser, the calendar deferral and
  the identifier lookup were all fixed and pinned in iteration 77
  (`tests/test_r89_times_people_say_are_understood.py` and
  `tests/test_r90_the_name_in_the_sentence_finds_the_thing.py`). Do not
  re-investigate them.
- The test suite writing into the user's data was fixed in iteration 77 and
  is pinned in `tests/test_r91_the_suite_does_not_write_into_the_users_data.py`.
  Do not point a browser test at a running server again.
- The email find/read/summarise workflow was fixed and pinned in iteration
  78 (`tests/test_r92_an_email_search_searches_for_the_thing_named.py`):
  the search term is carried, a named message resolves through the
  identifier lookup, and the reply names the message. Do not re-investigate
  it. All seven identifier sources now declare a `lookup` (calendar, email,
  workspaces, schedules, design assets, papers, devices) — verified in
  `test_every_identifier_source_declares_a_usable_lookup`.
- **Date-sensitive tests:** a test that pins a fixed calendar date as a
  relative phrase ("tomorrow") goes stale on the rollover and looks like a
  product defect. It was measured twice now (`test_r89` on 2026-10-23).
  Compute the target from `datetime.now()`; if a fixed date is genuinely
  required, the test must say so and the suite owner must re-date it.
- The Phase 28 unreachable-tools ledger is at **34** after `read_email`
  gained its read route (iteration 78). Any future route that makes an
  unreachable tool reachable — or a new tool that is unreachable by design —
  must update that number on purpose, with a dated note in the test.
- **Email conversation continuity is done** (iteration 79,
  `tests/test_r93_a_follow_up_points_at_the_message_selected.py`, 29 tests).
  The per-session selection lives in `email_selections` (keyed by
  `session_id`), accessed via `jarvis/core/email_context.py`. Referents are
  carried by the planner as `Intent.referent` / `Plan.referent` and resolved
  by `JarvisCore._resolve_email_referent` before any other stage. Build on
  `EmailContext` if a later feature needs "the message we just talked
  about" — do not add a second pointer or a module global. Reply-to-selected
  drafts only; the send gate is untouched.
- **The email providers have no organize operations** (verified 2026-10-23):
  the public surface of both `LocalMailProvider` and `ImapSmtpProvider` is
  `available / search / read / save_draft / send / describe`. There is no
  archive, move, flag, label or mark-read method. An "organize that one"
  request has no real operation to call — routing it to an invented endpoint
  or faking the result is the defect, not the absence of the feature. Either
  extend a provider with a genuine operation (gated) or answer honestly; the
  choice is recorded as R6.4c in `.autodev/tasks.json`.
- **`test_r85` is live-data dependent.** It pins the Swift screen models
  against live responses from `127.0.0.1:8765` (real `~/.jarvis-forge`
  token). `/auth/sessions` lists only scoped (phone) sessions, 8-hour TTL;
  with no phone connected it is legitimately `[]`, and the two `SessionInfo`
  checks now skip (iteration 79) rather than assert on live state. A green
  run of r85 in one session does not imply a green run in the next — check
  `lsof -iTCP:8765` and what the live data holds before reading a failure
  there as a regression.
- **The capability registry's REPORT is live; its GATE is dead — keep it that
  way** (iteration 80). `CapabilityRegistry.capability_report` reads
  `route_health` (real completions) and is surfaced in
  `GET /ai/providers/{id}/health` + the dashboard. `can_handle_request` /
  `check_ai_capability` / `find_compatible_providers` remain the conservative,
  uncalled gate path (r88 pins that; r94 pins the report is live). Do **not**
  wire the gate back into a request decision — it would refuse a working
  provider. The "wire it as a gate vs delete the module" choice is owner-gated.
  Pinned in `tests/test_r94_verified_capability_report.py`.
- **The route-health ledger is per-test isolated** (autouse fixture in
  `tests/conftest.py` calls `reset_route_health()`). A test that records a
  completion must do so under the **exact** `(provider, model)` key the
  endpoint it drives queries — pin the model via `save_provider_config` (the
  real per-provider selection) so the query and the evidence line up, or the
  report will silently read `unknown`.
- **Android build/tests are reproducible but device-runtime is blocked**
  (iteration 80). Toolchain: JDK 17 + `ANDROID_HOME` at
  `$HOME/Library/Android/sdk` (platform 34, build-tools 34.0.0), Gradle 8.11.1
  wrapper, AGP 8.7.3, Kotlin 2.1.0. `./gradlew testDebugUnitTest` → 48 pass
  (read the JUnit XML, not the exit code); `./gradlew :app:assembleDebug` →
  APK. **No emulator, no system-images, `adb devices` empty** — so install/
  startup/API behaviour is NOT verified. Do not report it as verified; do not
  fabricate an install. Do **not** run Gradle concurrently with the Python
  suite (measured: caused spurious OCR timeouts).
- **An email request is not a file operation** (iteration 81, r95). The file
  routes (`_compress`/`_delete`/`_copy_move`) defer when the subject names an
  email message and is not itself a file or a container ("the emails folder").
  "archive that email" must NOT become `compress_path("that email")`. This
  mirrors the calendar deferral (r90). Do not route an email organize request
  to a file tool; answer honestly that the mail backend has no such operation
  (or implement a real, gated IMAP operation — owner-gated). The same class
  covered **mark-state** (autonomous loop): "mark that email as read" was stolen
  by the read-referent branch (it READ the email), "as unread" by the
  unread-search branch (it returned a search result that faked the mark). A
  guard in `_email` (gated on an email noun, before those branches) now declines
  both honestly. Pinned in `tests/test_r95_an_email_is_not_a_file.py` (17
  tests). **This theme is CLOSED** (audited 2026-10-23 autonomous loop): 17
  email-organize phrasings (archive/compress/zip/move/copy/delete/forward/
  flag/star/label/mark-as-read/mark-as-unread/tag/put-in-folder/...) audited
  end-to-end → **0 remaining mutating misroutes**; each now declines honestly
  with no mutating action. Two non-mutating quirks remain and are _safe_
  (no wrong action): "delete that email" → a _memory_ clarification (the
  memory route catches it first — confusing, but takes no action), and
  "replies to that email" → reads the message (reasonable). Do not re-audit
  this theme; do not "fix" those two quirks unless a user reports them (chasing
  them would be scope creep). The only remaining email-organize work is a real
  IMAP operation, which is owner-gated (see R6.4c).
- **A calendar request is not a file operation either** (autonomous loop,
  extends r90). The iteration-77 calendar deferral guarded only the _move_
  branch, so `_compress`/`_delete`/copy still turned a calendar request into a
  file mutation on a path named after the event ("delete my Design Review
  event" → `delete_file`, "archive my standup meeting" → `compress_path`,
  "copy that event to notes" → `copy_file`). Now all four file-mutating routes
  defer to the calendar rule when the subject names a calendar thing and is
  not a file/container. The container check is shared with the email path via
  `_SUBJECT_IS_A_CONTAINER` (a folder named "events" is a filesystem target,
  not a calendar entry, so "compress the events folder" still compresses).
  Unlike email (which declines — no backend op), calendar **defers** (the
  backend has cancel_event etc.). Pinned in
  `tests/test_r90_the_name_in_the_sentence_finds_the_thing.py` (5 new tests);
  non-OCR full suite 3210 passed / 0 failed. **The cross-domain misroute
  theme is closed for both email and calendar.** Memory-note phrasings that
  reach the memory route first take no mutating file action (safe) — left
  alone to avoid scope creep.
- **The cross-domain misroute was extended to ALL managed domains** (autonomous
  loop, 2nd). The same class hit design assets, papers, schedules and workspaces
  — each has a real backend tool (`delete_design_asset`, `forget_paper`,
  `cancel_schedule`, `close_workspace`) but the file routes stole the phrasing
  ("delete the design asset" → `delete_file`, "compress that paper" →
  `compress_path`, etc.). Generalized the deferral predicate to
  `_names_a_domain_thing` (calendar **or** a conservative managed-domain noun
  set — asset/paper/schedule/briefing/workspace — all container-excluded) and
  swapped all four file-mutating routes to it. Real files named after domain
  entries still work (`delete paper-notes.txt`, `move workspace.yaml to
backup`, `compress asset.png`). Pinned in r90 (8 new tests, now 40); non-OCR
  full suite 3217 passed / 0 failed. **Theme closed for email + calendar +
  design/paper/schedule/workspace.** The noun set is conservative on purpose
  (only audited domains) to avoid over-broad deferral; note/project/report
  route to memory/file by design and are excluded.
- **Pre-existing, benign, non-safety routing quirks — recorded, NOT to be
  re-chased** (audited 2026-10-23 autonomous loop). After closing the
  wrong-target file-mutation class, a bounded audit confirmed the _remaining_
  domain-delete phrasings land in clarifications or a READ tool, and **all are
  non-mutating and pre-existing** (the domain routes `_design`/`_scheduler`/
  `_research`/`_coding` run _before_ the file routes, so these landings predate
  the deferral — the deferral only made them safer): "delete the design asset"
  → a diagram clarification, "delete schedule JOB-4" → a schedule-id
  clarification, "cancel the morning briefing schedule" → `daily_briefing`
  (READ-tier, side-effect-free — it composes and shows a briefing, no write).
  These are _feature-completeness_ gaps (a domain delete not reaching its real
  tool), not safety defects and not introduced by the misroute fix. Fixing them
  means rewriting domain routing — a separate, larger effort and a new
  regression surface, so it is left as a recorded, owner-prioritized item
  rather than expanded on autonomous judgment. Do not re-audit or "fix" these
  unless the owner asks.
- **`/command` input length is enforced at the HTTP boundary** (iteration 81).
  `MAX_COMMAND_CHARS` (20 000) is on `CommandBody` (the FastAPI request model),
  so an oversized paste is a clean **422**, not a 500 from constructing
  `CommandRequest` inside the route. If you move validation, keep the cap on
  the body model, not just `CommandRequest`. Pinned in `tests/test_api.py`.
- **A full suite that runs 2× the usual time is under contention, not
  failing.** Measured (iteration 81): a 605 s run showed 7 OCR/attachment
  failures; the same tests pass in 13 s in isolation. Launching the macOS app
  (or any Gradle build) during the pytest run adds load that trips the OCR
  timeouts. Before reading an OCR/attachment failure as a regression, re-run
  those tests in isolation.
