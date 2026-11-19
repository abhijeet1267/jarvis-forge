# V26.4 — Who calls `/command`, and what mission mode would change for them

Read-only audit of worktree `/Users/abhijeetmiskin/AppData/Vantage-v26`
(branch `feature/v26-voice-operator-actionmesh`).
No file other than this one was written; no code was changed.

> **Concurrent-writer note.** The tree was clean when this audit began. Partway
> through, `git status` showed `jarvis/api/app.py` modified by an `Idempotency-Key`
> addition around `POST /command` that is **not mine** — I did not touch that
> file. Line numbers in this document were re-checked after that change; the
> `/command` route is still at `app.py:1045`. If another session is working in
> this worktree, review that diff separately.

Scope: `JARVIS_COMMAND_MISSION_MODE` / `Settings.command_mission_mode`
(`jarvis/config.py:350`, default `False`) diverts `JarvisCore._execute_plan`
(`jarvis/core/orchestrator.py:1359-1362`) into
`_run_plan_as_mission` (`jarvis/core/orchestrator.py:1270-1266`) whenever the
flag is on, the plan has actions, and the call is not a retry
(`job_id is None and not seqs`).

---

## 1. What the flag actually changes (read from source, not from the docs)

The diversion happens **before** the inline loop builds anything else. Compared
with the inline path, a mission-mode turn differs in all of the following:

| Property of the response | Inline path | Mission mode |
|---|---|---|
| `reply` | composed from each action's message (`_compose_reply`) | built from the **mission record** (`state`, `verdict`, condition statements). The tool's own output text is not used. |
| `actions` | one `ActionResult` per action | **always empty** — `_run_plan_as_mission` never appends to `response.actions` |
| `confirmation_id` | set from the gate verdict | **never set** — `needs_confirmation=True` is set, but the confirmation id stays `None` |
| `job_id` | minted for multi-action commands (`orchestrator.py:1364-1368`) | **never minted** — the diversion returns before job creation |
| `mission_id` | ledger-stub id (settled `partially_completed`) | the real mission id, often `verified` |
| per-action audit row (`action_logs`) | one per action via `_audit_action` | **none** — the runner writes mission evidence/refusal rows only |
| email selection / outcome memory / V2 entity memory | recorded (`_record_email_selection`, `_remember_outcome`, `_remember_verified_entity`) | **skipped** — none of those helpers are called |
| permission context | `session_id=request.session_id`, `requested_surface=_request_surface(request)` | `session_id="mission:<mission_id>"`, `requested_surface="mission"` (`jarvis/api/missions_routes.py:758-760`, used by `voice_mission.run_as_mission`) |
| `model_used`, `intent`, `plan`, `session_id`, `duration_ms`, `decision`, `blocked_reason`, `recovery_notes` | populated | still populated (`_handle` set `model_used`/`intent`/`plan` before dispatch) |

Two consequences matter more than anything else in the table:

1. **A read-only command loses its answer.** `voice_mission.conditions_for`
   (`jarvis/core/voice_mission.py:41-46`) only emits verifiers for
   `open_application` and `close_app`. Every other tool — including
   `get_battery_status`, `check_storage`, `list_applications`, calendar reads,
   search — produces a mission with **no condition**, so `verdict()` returns
   `unknown` (`jarvis/core/missions.py:226-242`) and the reply becomes
   *"I ran that, but could not confirm it: …"*. The fact the person asked for
   (the battery percentage, the file list) is no longer in `reply` at all,
   because `reply` is composed from the mission, not from the action messages.
2. **The permission gate loses the caller.** The mission gate asks the engine
   as `surface="mission"`. As shown in §5, that silently disables the Phase 71
   phone self-approval guard for any request diverted into a mission.

### The full `CommandResponse` field list

From `jarvis/core/models.py:417-444` (every field, in order):

`request_id`, `session_id`, `reply`, `intent`, `plan`, `actions`,
`recovery_notes`, `decision`, `needs_confirmation`, `confirmation_id`,
`blocked_reason`, `job_id`, `mission_id`, `model_used`, `duration_ms`,
`created_at`.

`mission_id` is **not new** — it already exists on the model and is returned by
the inline path (as the ledger-stub id). Mission mode changes its value, not the
schema. No client needs to change decoding because of a new field.

---

## 2. Caller table

"Reads" lists only fields the caller actually touches — checked in the source,
not inferred. "Breaks?" is about the flag being **ON**, default behaviour being
the inline path.

| # | Caller (file:line) | What it sends | Response fields it actually reads | Breaks under mission mode? | Why / why not |
|---|---|---|---|---|---|
| 1 | `macos/Vantage/Sources/APIClient/APIClient.swift:443` `focusApp` | `{text: "switch to <app>"}` | `{reply}` only (`struct Reply { let reply: String? }`); return value discarded | **No (decode)** / partly (wording) | `reply` is still non-empty: the AWAITING/verified/blocked/unverified branches all set it. But `reply` is a `String?`, so even an absent value would not fail decoding. Only the sentence changes. |
| 2 | `macos/Vantage/Sources/APIClient/APIClient.swift:451` `openApp` | `{text: "open <app>"}` | `{reply}` only | **No (decode)** / partly | Same as #1. For `open_application` a verifier exists, so mission mode can reply `"Done, and I checked it: …"`; still a valid non-nil string. |
| 3 | `macos/Vantage/Sources/Services/ConversationService.swift:410` `turn()` — used by `CommandCenterView.swift:675` and `VoiceOperatorV2View.swift:402` | `APIModels.CommandRequest` (`text`, `session_id`, `device_id:"laptop_main"`, `source:"native"`) | `session_id`, `model_used`, `duration_ms`, `needs_confirmation`, `decision`, `reply`, `blocked_reason`, `intent` | **YES — most likely product caller to break** | This is the conversational surface. Desktop instructions are diverted to `/missions/desktop` first (`ConversationService.swift:130-160`); everything else, including reads like "what is my battery", goes to `/command`. Under mission mode the read is diverted into a mission, `actions` is empty, and the reply becomes "I ran that, but could not confirm it: …". The decoded fields still parse (all optional, no `CodingKeys`), so the break is **behavioural, not a crash**. |
| 4 | `macos/Vantage/Sources/Views/WorkView.swift:408` `runCommand` | `APIModels.CommandRequest(text:)` (`source:"native"`) | none — `_ = try await … as CommandResponse`; only catches transport/decoding errors | **No** | Full-model decode is safe: `APIModels.CommandResponse` (`APIModels.swift:209-237`) is all-optional, has no `CodingKeys`, and `JSONDecoder` ignores unknown keys. `mission_id` is not declared, so it is simply ignored. No synchronous semantics expected. |
| 5 | `dashboard/index.html:2614` `send()` | `{text, source:"dashboard"}`, token header | `reply`, `confirmation_id`, `actions[0].tool`, `intent`, `job_id`, then `pipelineState(data)` | **YES** | With `actions` empty, `pipelineState` (`index.html:1435-1517`) falls through the "no actions" branch and returns **`PLANNED`** instead of `VERIFIED`. `renderWorkflow(data.job_id)` gets `""` for multi-action commands because no job was minted. `data.confirmation_id` is `None`, so the "open the Approvals panel" sentence is never appended. |
| 6 | `dashboard/index.html:2328` `changePersistence()` | `{text, source:"dashboard"}` | `confirmation_id`, `reply` | **Partly** | The branch keyed on `confirmation_id` never fires (mission mode leaves it `null`), so a real pending approval is rendered as if it were a completed action. The `reply` fallback still shows text. |
| 7 | `dashboard/mission_control.html:1305` quick actions | `{command: action}` (note: field is `command`, not `text` → exercises the 422 path, not the action path) | `ok`, `result`, `error` — **none of which `/command` returns** | **No** | This caller is already broken against the current contract (it posts an unknown field and reads fields the schema does not have); mission mode does not make it worse. Recorded as a pre-existing defect, not a reason to scope the flag. |
| 8 | `dashboard/operator.html:467` `run()` | `{text, source:"operator", session_id}` | `needs_confirmation`, `reply`, `actions[].tool/message/error/ok/verification`, `intent`, `decision`, `model_used`, `session_id` | **YES** | The main render branch is `resp.actions && resp.actions.length`; under mission mode that is always false, so every turn collapses to the `reply`-only branch and the per-action verified/failed badges disappear. |
| 9 | `dashboard/operator.html:1075` app "Open / Bring forward" button | `{text: "open <name>" / "switch to <name>"}` | **nothing** (response not read; only catch) | **No** | Fire-and-forget; it re-reads `/apps/status` afterwards. Mission mode may change which sentence is ignored, but nothing decodes. |
| 10 | `dashboard/phone.html:560` `send()` | `{text, source:"phone"}`, HttpOnly cookie credential; no session token in the body | `session_id`, `decision`, `needs_confirmation`, `reply`, `actions[].tool` | **YES — highest severity** | Two problems. (a) `actions` is empty, so the "tool" detail line always reads "no action". (b) The confirmation the mission gate mints is recorded with `requested_surface="mission"`, and the self-approval guard in `orchestrator.resolve_confirmation` keys on `record["requested_surface"] in NARROW_SURFACES`. It is not, so **a phone can approve its own consequential request** under mission mode (see §5). `needs_confirmation` still renders the warn chip, so the page behaves as if everything is normal. |
| 11 | `dashboard/work.html:827` `runCommand()` (powering the "Open in editor" button, `work.html:378`) | `{text, source:"work"}` | `reply` (written into `#job-err`) | **No** | Only `reply` is read, and it is still a string. The sentence shown on failure changes, which is a UX regression, not a decode break. |
| 12 | `android/.../data/api/VantageApiClient.kt:242` `sendCommand` → `CommandViewModel.kt:103` | Kotlin `CommandRequest` (`text`, `device_id`, `source:"mobile"`, `session_id?`) | `reply` (via `displayText`), `request_id`, `intent`, `decision`, `actions`, `needs_confirmation`, `confirmation_id`, `blocked_reason`, `recovery_notes` | **YES** | `WireJson` uses `ignoreUnknownKeys = true` (`ApiModels.kt:18-21`), so decoding is safe. But `displayText` prefers `reply`, which no longer contains the read's value; `hasVerifiedEffect` is `actions.any { it.isVerified }`, and `actions` is empty, so the verified indicator turns off for every action. |
| 13 | `android/.../ui/google/GoogleViewModel.kt:194` `sendWrite` | `CommandRequest(text, source:"mobile", sessionId=sessionToken)` | `confirmationRequired`, `isBlocked`, `actions.any { it.ok == true }`, `reply`, `blockedReason` | **YES** | `acted` is computed from `response.actions`, which is empty under mission mode, so a Google task write that actually ran is never followed by `readTasks()/readEvents()` — the screen keeps showing stale data after a successful write. This is a concrete silent-behaviour change on a write path. |
| 14 | `ios/VantageAI/.../CompanionAPIClient.swift:139` `sendCommand` (iOS paused) | Swift `CommandRequest` (`text`, `device_id:"phone"`, `source:"mobile"`, `session_id?`) | `CommandResponse` as declared (`result`, `status`, `error`, `task_id`, `tool_calls`, `verification`, `confirmation_required`, `confirmation_id`) — **a superset that does not match the server** | **Already broken, independent of the flag** | The iOS client's model (`ios/.../Models/APIModels.swift:59-68`) declares fields the server never sends and does not declare `reply`. Per project policy iOS is paused; it is listed for completeness only. Mission mode does not change its state. |
| 15 | `benchmarks/acceptance_run.py:100` | `{text, source:"benchmark"}` | `needs_confirmation`, `confirmation_id`, `intent`, `actions[].tool/verification`, `reply`, then queries `action_logs` | **YES — guaranteed hard failure** | It posts `/confirm/{id}` only when `confirmation_id` is present (it will not be), then requires a new `action_logs` row and calls `SystemExit` if there is none ("produced no audit row"). Mission mode writes no per-action `action_logs` row, so the benchmark aborts. Also loses `actions[]` entirely. |
| 16 | `benchmarks/safety_run.py:100` | `{text, source:"safety-matrix"}` | `actions`, `reply`, `decision`/`blocked_reason` via `pipelineState`, `action_logs` | **Partly** | Most matrix phrases are refused/clarified before `_execute_plan`, so they are unaffected. The one that reaches the gate ("turn on heater") would lose its `actions` and its audit rows; its `passed` predicate also asserts `not payload.get("actions")`, which would still pass — but for the wrong reason. |
| 17 | `tests/isolated_server.py:175` `post_command` (test helper) | `{text}` | whole JSON as dict | **No (decode)** — tests that assert on `actions`/`confirmation_id` would fail | A helper, not a product caller; listed because tests using it would need review if the flag is turned on in CI. |
| 18 | `macos/Vantage/IntegrationHarness/run_work_render.sh:48`, `run_command_center_render.sh:48`, `run_approvals_render.sh:51` | `{text, source:"seed", session_id:"w1"}` / `{text, source:"seed", session_id:"approvals-render"}` | the served screenshots (depend on a job and a pending confirmation existing) | **YES** | The work/command-center render seeds `"check battery, then check storage"` and then expects a job row; mission mode mints no `job_id`, so the Work view renders with no job. The approvals render seeds a delete to create a pending confirmation; mission mode still parks a mission, and `/confirmations` lists the policy row, so that one may survive — but it is not certain without running it. |

### Indirect paths that reach the same executor (the flag is not route-scoped)

These are not `POST /command`, but they call `JarvisCore.handle` /
`propose_and_execute`, which reach `_execute_plan`, so a global flag changes
them too:

| Path (file:line) | Effect under mission mode |
|---|---|
| `jarvis/api/app.py:2785` `GET /command/stream` → `_event_stream` (`app.py:2724-2737`, `source="stream"`) | The stream reads `response.actions` to emit `event: action` frames (`app.py:2749-2761`). With `actions` empty, **no action frames are emitted**, and `tests/test_api.py:194-197` asserts `"event: action"` and `get_battery_status` in the stream. |
| `jarvis/api/app.py:2635` `POST /voice/command` → `jarvis/voice/session.py:162` (`source="voice"`) | `session.pending_confirmation = response.confirmation_id` (`session.py:176`). Mission mode leaves `confirmation_id` `None`, so a spoken **"yes"** on the next turn cannot resolve the parked confirmation and is planned as a new command. The Voice Operator's spoken approval path breaks. |
| `jarvis/api/app.py:5927` watch routine reply (`source="watch"`) | Routines that reach the gate lose `actions` and their audit rows; `reply` is still returned to the watch. |
| `jarvis/api/app.py:6501` `/ai/chat/send` → `propose_and_execute` (`orchestrator.py:472-542`) | `propose_and_execute` calls `_execute_plan` directly, so the flag applies to model-proposed chat actions too. The route builds its `action` block from `outcome.actions` (`app.py:6505-6530`); under mission mode that is empty, so the chat UI reports `executed: false` for an action that ran. **This is a silent misreport on a write path.** |

### Proxies that carry `/command` (not callers, but doors)

| Door (file:line) | Consequence |
|---|---|
| `jarvis/companion/gateway.py:56` `PHONE_ALLOWED` includes `/command` | Every Android/phone command is proxied to the backend as the **master token** with `X-Vantage-Surface` set by the gateway (`gateway.py:386`) and `X-Vantage-Device-Id` forwarded (`gateway.py:371-372`). The gateway replaces the session token, so the backend cannot look the session up. Mission mode therefore applies to **phone-originated commands**, and the gate loses the surface that the whole self-approval rule depends on. |
| `jarvis/relay.py:78` `RELAY_ALLOWED` includes `/command` | A remote relay client's commands are forwarded with **the client's own host credential** (`relay.py:279-291`) and the body is stamped with `device_id` and `source: "relay"` (`relay.py:379-399`). So mission mode also applies to relay-originated commands, and `source` is set server-side there — but the credential is the client's, so the same surface problem exists. |
| `jarvis/api/app.py:299-323` `PHONE_ALLOWED` (local phone page) | The phone page talks to the backend directly on loopback with an HttpOnly cookie session minted with surface `"phone"` (`app.py:5988`). That session **is** resolvable by `_surface_of`, but `/command` never calls it. |

---

## 3. Is `focusApp` / `openApp` safe? (the specific question asked)

Yes for decoding, and yes for `reply` being populated — verified in
`_run_plan_as_mission` (`orchestrator.py:1270-1326`): every branch sets
`response.reply` to a non-empty string, and the Swift `Reply` struct only
requires `reply` to be *present* (it is `String?`, so even absence would decode).
The sentence changes from "Calculator is open on your macos machine." to
"Done, and I checked it: …" (flag on, verifier succeeds) — a wording change, not
a contract break. The one risk is the app name: the verifier derives the
process name from the planner's `app` parameter (`voice_mission._process_name`),
so an app whose process name differs from its display name can produce
"…but could not confirm it". `OperatorView.swift:165` ignores the reply, so it
would show "Asked Calculator." regardless.

## 4. Does `WorkView.runCommand` decoding break? (the specific question asked)

No. `APIModels.CommandResponse` (`macos/Vantage/Sources/APIClient/APIModels.swift:209-237`)
has only optional stored properties, no `CodingKeys`, no custom `init(from:)`,
and it does not declare `mission_id` at all. Swift's `JSONDecoder` ignores keys
that are not declared, so the extra/different values cannot fail the decode.
Mission mode also adds no new fields — `mission_id` was already in the Python
model. `runCommand` discards the response anyway.

## 5. Security finding: mission mode disables the phone self-approval guard

This is the most important consequence of a **global** flag and is not visible
in any of the caller rows above.

- Inline path: `orchestrator._execute_plan:1405-1408` calls
  `self.policy.evaluate(action, session_id=request.session_id,
  requested_surface=self._request_surface(request))`. For a phone-originated
  command that is `"phone"` (mapped from `source` in `:432-447`, or forwarded by
  the gateway as the header used for `/confirm`).
- Mission path: `voice_mission.run_as_mission` builds the runner with
  `permission=_gate_factory(core)` (`jarvis/core/voice_mission.py:166-172`), and
  `_gate_factory` (`jarvis/api/missions_routes.py:750-780`) evaluates every step
  with `session_id=f"mission:{mission_id}", requested_surface="mission"`.
- The confirmation row therefore stores `requested_surface="mission"` and
  `requested_by_session="mission:MSN-…"` (`policy._create_confirmation:413-450`).
- `orchestrator.resolve_confirmation` (`jarvis/core/orchestrator.py:1669`;
  rule at `:1745-1753`) computes
  `raised_on_narrow = requested_surface in NARROW_SURFACES` → **False**, and
  `identity_match = bool(session_id) and requested_session == session_id` →
  **False** (`"mission:MSN-…"` never equals the phone's `SESS-…`).
- Therefore `self_approval` is **False**, and a phone that polls
  `/confirmations`, finds its own parked step, and taps Approve has that
  approval honoured.

Verified by reading the code paths, not by running a phone: the store, the row
and the guard are the same ones exercised by
`tests/test_phone_self_approval_guard.py`. I did not execute the two-server
phone flow, so this is **NOT TESTED end-to-end** — it is a source-level
finding with a clear call chain. It should be reproduced with that test before
the flag is enabled anywhere.

## 6. Can `source` (or another field) be trusted to scope the flag?

Exactly what a caller can already set, from `jarvis/api/schemas.py:20-27`:

```
text: str            # free text, validated non-empty and length-capped
session_id: Optional[str]
device_id: str = "laptop_main"
source: str = "api"          # cli | voice | api | watch are the documented values
context: Dict[str, Any] = {}
```

- **`source` is not trustworthy.** It is an arbitrary body string; nothing
  validates it against an enum or against the credential. `_request_surface`
  maps it to a surface for audit/confirmation purposes only. Any client — a
  phone, a script, a model-proposed request — can send `source: "voice"` or
  `"operator"`. Keying mission mode on it would be a spoofable switch.
- **`device_id` in the body is a claim.** The route prefers
  `X-Vantage-Device-Id` when present (`app.py:1053-1059`), the gateway forwards
  it (`gateway.py:371-372`), and the relay overwrites it
  (`relay.py:393-398`) — but on loopback any process can set the header.
- **`context` is caller-supplied JSON.** Not an identity.

What **is** trustworthy at the route, and is not currently used:

1. **The presented credential's own surface.** If the caller authenticates with
   a session token in `_sessions`, `_surface_of(_presented_credential(request))`
   (`app.py:400-404`, `_presented_credential:503-518`) returns the surface the
   server assigned at bootstrap mint time (`"phone"` at `app.py:5988`,
   `"dashboard"` elsewhere). This is server-minted, single-use, revocable, and
   cannot be forged by the client. The master token has no surface — it is the
   operator (macOS app, CLI), which is a legitimate separate bucket.
2. **`X-Vantage-Surface`**, set by the companion gateway itself
   (`gateway.py:386`) after validating the session. It is trustworthy *as
   forwarded by the gateway*, but the backend cannot distinguish the gateway
   from another loopback client — both arrive on 127.0.0.1. The existing code
   treats it as authoritative for `/confirm` and the tests pin that it is not
   copied from the client (`tests/test_phone_self_approval_guard.py:140-151`).
   Using it on `/command` would be consistent with `/confirm`, with the caveat
   that a local process that already holds the master token could claim it —
   but such a process can already do anything.

So: **no field a caller sets today can be trusted; the credential's surface can
be, and the route already has the helper to read it.**

---

## 7. Recommendation

**Do not leave the flag global. Scope it, and fix three propagation bugs before
it is enabled anywhere.**

The flag as written is a process-wide switch over an endpoint with at least
18 call sites across macOS, Android, three dashboard pages, the phone, the relay,
the gateway, the SSE stream, the voice session, the watch and the AI chat. The
audit shows the change is not uniform across them:

1. **Scope by the authenticated surface, not by `source`.** Decide mission mode
   per request from the credential the caller actually presented
   (`_surface_of(_presented_credential(request))`, and `X-Vantage-Surface` for
   gateway-forwarded calls), never from the body. If per-caller control is
   wanted, that is the only non-spoofable input available. A body field would
   let any client opt itself into a different execution path — and, given the
   confirmation-id regression below, into a weaker one.
2. **Only divert when the change cannot lose the answer.** At minimum, refuse to
   divert tools that have no verifier (`conditions_for` returns `[]`). Today
   every read-only command — the majority of conversational turns — is diverted
   into a mission with no condition, and its reply becomes "I ran that, but
   could not confirm it" with the actual value gone from `reply`. Either keep
   reads on the inline path, or make the mission reply include the step's own
   message when the verdict is `unknown`.
3. **Carry the caller's context into the mission gate.** `_gate_factory` must
   receive the real `session_id` and `requested_surface` (or the mission must
   store them and the gate must read them), otherwise the phone self-approval
   guard is bypassed for every diverted command (§5). This is a security
   invariant in `CLAUDE.md`, not a preference.
4. **Propagate `confirmation_id`, `actions` and `job_id` from the mission back
   into the response**, or the dashboards, the voice "yes" path, the SSE stream
   and the AI chat all misreport a parked or completed action. Each of those is
   a caller that reads the field today.

Short version for the lead: the flag is safe for `focusApp`/`openApp` and for
`WorkView`'s decode, and unsafe as a global switch for the phone (security), the
voice approval path (functional), the dashboards and AI chat (silent
misreporting), and the acceptance benchmark (hard failure). Keep the default
`False`; when it is turned on, make it a server-derived, per-credential decision
and fix the context/confirmation/action propagation first.

---

### Evidence classification

| Claim | Class |
|---|---|
| Caller list, fields read, decode strictness | PASS — read from source at the cited lines |
| Mission path leaves `actions`, `confirmation_id`, `job_id` empty/unset and skips `_audit_action` | PASS — read `_run_plan_as_mission` and the shell of `_execute_plan` |
| Phone self-approval guard bypass under `requested_surface="mission"` | **NOT TESTED** — source-level call chain in §5; reproduce with `tests/test_phone_self_approval_guard.py` before enabling |
| Read-only commands lose their answer | PASS (source) — `conditions_for` maps only two tools; `reply` is built from the mission, not from action messages |
| Acceptance benchmark aborts under the flag | PASS (source) — `acceptance_run.py:100-125` raises `SystemExit` when no `action_logs` row is added |
| Any runtime behaviour of the flag in a live server | **NOT TESTED** — this audit did not start a backend |
