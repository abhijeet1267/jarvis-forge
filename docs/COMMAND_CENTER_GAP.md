# Command Center → desktop mission: audit (V22 follow-up)

Audited 2026-11-17 by reading the Swift sources and probing the backend that was
already running on `127.0.0.1:8765` (a bundled `Vantage.app` backend; it was
replaced by a different instance part-way through the audit — see §7).

No Swift source was modified. The audit found the bounded fix is **not** bounded:
the approval loop it would need is not implementable inside
`ConversationService.swift`, and one pre-existing decode defect would strand
every desktop mission it created. Details in §5.

Companion doc: `docs/COMMAND_CENTER.md` (the earlier audit that established the
wrong-planner route through `/command`, now superseded on the "what to wire"
points by §5 below).

## 1. What `send("move the cursor to 400 300")` does today

1. `ConversationService.send` (`macos/Vantage/Sources/Services/ConversationService.swift:75`)
   tries `MacControlCoordinator.shared.handleCommandCenter(trimmed)` at **:87**.
   `MacControlCoordinator.handleCommandCenter`
   (`macos/Vantage/Sources/Services/MacControl/MacControlCoordinator.swift:260`)
   goes to `parser.parse(text)` at **:272**. `MacCommandParser` has no cursor or
   pointer rule, so `parse` returns `.notMacControl` (or `.explanation` for a
   how-to question), and the coordinator returns `nil` at **:273-274**.
2. `send` falls through to `turn(trimmed, appendUserTurn: true)` at
   `ConversationService.swift:93`.
3. `turn` posts **`POST /command`** at `ConversationService.swift:138` with
   `APIModels.CommandRequest(text:sessionId:)`. That is the only endpoint a
   Command Center turn reaches.
4. Live, that endpoint answers wrongly: `POST /command` replies
   `"This needs your confirmation before I run it. Will do: move_file on
   laptop_main | source=the cursor | destination=400 300"`. The filesystem
   planner claims the sentence; the desktop planner is never consulted. This
   still reproduces (2026-11-17 11:52 UTC probe, HTTP 200).

So today: a desktop instruction becomes a **`move_file` confirmation for a file
that does not exist**, and no pointer moves.

## 2. Is `POST /missions/desktop` referenced anywhere in Swift?

No. `grep -rn "missions/desktop" macos/` → exit 1, zero matches (the only hits
anywhere are the bundled backend copy under `Vantage.app/Contents/Resources/`).
The mission screens POST `/missions` (create) and `/missions/{id}/run`
(`macos/Vantage/Sources/Views/MissionControlView.swift:271,339`,
`macos/Vantage/Sources/Views/AutomationStudioView.swift:598,619`), but neither
starts from a typed sentence.

## 3. What would need to call it, run it, and handle approval

- **Call it.** `ConversationService.turn` (`ConversationService.swift:115`) is the
  right seam: before `:138` it would `POST /missions/desktop`
  (`jarvis/api/missions_routes.py:108`, body `{"request": …}`), treat **422** as
  "not a desktop instruction" and fall through unchanged (the route itself uses
  `looks_like_desktop`, `jarvis/desktop/planner.py:101`, as the single source of
  truth).
- **Run it.** `POST /missions/{id}/run` (`missions_routes.py:450`) — body
  `{"approval": {"confirmation_id": …, "lease_id": …} | null}`. The route parses
  the body per request (`Body(None)`), and a live bodyless ping parked a click
  correctly, so `MissionRunRequest(approval: nil)` is safe for the first call.
- **Drive the approval loop.** Nothing in `ConversationService` can. See §5.

## 4. How the existing approval flow works

Two separate mechanisms exist; neither is usable from the conversation service:

- **`/command` confirmations.** `POST /command` returns
  `needs_confirmation`/`confirmation_id` (`APIModels.CommandResponse` fields,
  `macos/Vantage/Sources/APIClient/APIModels.swift:231`). The view renders
  "Waiting for your approval" from `ChatMessage.MessageMeta.awaitingApproval`
  (`CommandCenterView.swift:1015`, meta built at `ConversationService.swift:145-151`).
  The answer path is `refreshConfirmations` (`CommandCenterView.swift:755`, via
  `GET /confirmations`) → `resolve(_:approve:)` (**:771**) →
  `APIClient.resolveConfirmation` (`macos/Vantage/Sources/APIClient/APIClient.swift:729`,
  `POST /confirm/{id}?approve=…&session_id=…`). The card list is refreshed only by
  the screen's `.onAppear` (`CommandCenterView.swift:170`) and by `resolve` itself
  (**:801**); there is no hook the service can trigger after a send.
- **Mission approvals.** A mission step the gate holds parks the mission at
  `awaiting_approval` (`jarvis/core/mission_runner.py:317-327,383-402`), with the
  confirmation id written into the step's `detail` text. The app reads it back
  out of that string (`MissionControlView.swift:285`, `AutomationStudioView.swift:640-650`)
  and resumes via `POST /missions/{id}/steps/{step_id}/decide` then
  `POST /missions/{id}/run` with the approval (`MissionControlView.swift:204-282`,
  `AutomationStudioView.swift:655-672`). Verified live end-to-end:
  `decide` → 200, `run` with the parked `CONF-…` → `partially_completed`, step
  `done`, pointer/click executed. Note the mission confirmation is **not** the
  same card as `/confirmations`: it has `requested_surface="mission"` and its own
  id-match resume path (`mission_runner.py:352-402`); approving it through
  `POST /confirm/{id}` is the `/command` path and does not resume the mission.

## 5. Why the fix is not bounded (stop at audit)

Four facts, each measured:

1. **`ConversationService` has no surface to show an approval on.**
   `ChatMessage.MessageMeta.awaitingApproval` is only drawn by
   `MessageCaption`, which is rendered on the **assistant** row
   (`CommandCenterView.swift:894`), not the tool row the service writes
   (`ConversationService.swift:89`). The only actionable card comes from
   `pendingConfirmations` (`CommandCenterView.swift:323`), populated by a view
   function (`refreshConfirmations`, :755) the service cannot call. A
   service-side "wait for approval, then report the real result" needs either a
   new published state in the service **and** a new branch in
   `CommandCenterView` — outside this task's allowed files — or it silently
   strands the mission.
2. **The mission's parked confirmation is not listed for this surface.**
   `policy.pending()` (`jarvis/security/policy.py:457-464`) returns every pending
   row, including the mission's, so the card would appear — but answering it goes
   through `/confirm/{id}`, which executes the tool directly
   (`jarvis/core/orchestrator.py:1793`) and never touches the mission row: the
   mission stays in `awaiting_approval`, and the runner only honours an approval
   whose `confirmation_id` matches the id it wrote onto the parked step
   (`mission_runner.py:358-402`). So the person sees one card, approves it, and
   the mission they were shown is still parked. Showing a card whose approval
   skips the mission is worse than no card.
3. **`MissionStep.params: [String: String]` cannot decode a desktop mission.**
   `macos/Vantage/Sources/APIClient/APIModels.swift:1525`. The planner emits
   numbers: `{"x":400.0,"y":300.0}` (`missions_routes.py:157-166` →
   `MissionStep.to_dict`, `jarvis/core/missions.py:163-175`). Proven with a
   standalone Swift decode: a single desktop mission in a `MissionList` fails
   with `typeMismatch(… "params" … "x" … "Expected to decode String but found
   number instead")`, which fails **the whole list** (`MissionList`,
   `APIModels.swift:1530`). Every mission screen loads that list
   (`MissionControlView.swift:82`, `MissionSteeringView.swift:467`,
   `ShadowMissionView.swift:246`), so the first desktop mission created from
   Command Center would blank Mission Control, Steering and Shadow. The fix is
   in `APIModels.swift`/the three views, not in `ConversationService.swift`.
4. **The 422 fall-through is cheap but noisy, not wrong.**
   `looks_like_desktop` is first-word only (`planner.py:113-120`, verbs at
   :95-98). Sentences that start with a desktop verb but are conversation fall
   through as *planned* work, not 422: live probes on this backend returned
   `status:"planned"` for **"type this up for me"** (mission
   `desktop_type_text {"text":"this up for me"}`) and **"focus on your goals"**
   (mission `desktop_focus_window {"application":"on your goals"}`) — both are
   questions `/command` answers sensibly today. Only 422 is a safe
   fall-through; a `planned` result for these would create draft missions and
   change what the person sees. Measured cost of the probe itself: 422 in
   ~0.8-1.2 ms, planned in ~5 ms, so latency is not the objection.

## 6. What the integrator must wire (and in what order)

1. **`APIModels.swift`** — decode `MissionStep.params` as
   `[String: AnyCodable]?` (or a lossless value type) at `:1525`; audit the other
   `[String: String]` mission params at `:1745,1814,2331,2609,2629,2730` for the
   same mixed-type hazard. Until this lands, *nothing* that creates desktop
   missions from the app can be shown.
2. **`ConversationService.swift`** (this file, if the plan allows) — call
   `POST /missions/desktop`; on `clarification` show the clarification and run
   nothing; on 422 fall through to `/command`; on `planned` store the mission id
   and step confirmation id, publish a pending approval, and **not** claim a
   result.
3. **`CommandCenterView.swift`** — render that pending mission approval using the
   existing `MissionControlView` pattern (`decide` → `run` with
   `MissionApproval(confirmation_id:lease_id:)`, `MissionControlView.swift:204-282`);
   after the run, refresh the mission and append the real step `detail` as a tool
   turn. Without this, the conversation cannot complete an approval.
4. **`MacCommandParser.swift` / `MacControlCoordinator.swift`** (optional, same
   product gap): a cursor move ("move the cursor to 400 300") is currently not
   native mac-control either, so it would take the new mission path. That is
   coherent only once 1-3 land.

Do **not** route the mission's parked confirmation through `POST /confirm/{id}`
(see §5.2). Do not widen `PHONE_ALLOWED` (`jarvis/api/app.py:294-322`) — none of
this requires it.

## 7. Evidence notes and limitations

- Live probes at 11:51-12:04 UTC used `X-JARVIS-Token`; all mission verdicts are
  the server's, copied verbatim.
- From approximately 12:19 UTC the `127.0.0.1:8765` responder answered **401 to
  the on-disk token** (`~/.jarvis-forge/api_token`, unchanged since Nov 14
  11:01). A different backend instance (a standalone `uvicorn … --port 8798`
  process, then a bundled `Vantage.app` one) was serving. This is the known
  V22 "interpreter still external" area, **not** something this audit caused or
  fixed; the Swift facts in §1-§5 do not depend on the live server.
- Probes created real missions in `~/.jarvis-forge/jarvis.db`
  (`MIS-5339e8994efb`, `MIS-d99f53911525`, `MIS-2a1e827f10da`,
  `MIS-aa31ef35031b`, `MIS-c11762fd5f37`); one pointer move and one right-click
  were actually executed at 12:02 and 12:04 UTC as part of verifying the
  approval resume path. One `CONF-…` row remains `pending`
  (`CONF-c2351bb07117928bc2bb36207fd12d08`, `desktop_right_click`), because the
  bundled backend instance changed mid-audit; it will expire on its own
  (`confirm_timeout` 300 s).
- `swift build --package-path macos/Vantage` was **PASS** (exit 0) before this
  audit wrote anything; no Swift file was changed, so the tree's build state is
  unchanged by it.