# Command Center ↔ desktop instruction: audit

Audited 2026-11-17 by reading the source and making live read-only requests to
the already-running backend on `127.0.0.1:8765`. No source file was modified;
no mission was run; no desktop input was sent.

## 1. Verdict

**No.** A desktop instruction typed into Command Center is not connected to the
desktop planner: the view sends it to `POST /command`, which never calls
`plan_desktop`, and the endpoint that does call it (`POST /missions/plan`) is
referenced by no Swift code. The one route the UI does use for desktop planning,
`GET /desktop/plan`, is preview-only and lives on a different screen.

## 2. Evidence chain

### 2.1 View → endpoint

- `macos/Vantage/Sources/Views/CommandCenterView.swift:623` — `sendCommand()`.
  Its only outbound path is `chat.send(text)` at **:654**.
- `macos/Vantage/Sources/Services/ConversationService.swift:87` — before any
  HTTP, `MacControlCoordinator.shared.handleCommandCenter(trimmed)` is tried.
  It returns `nil` for this sentence: the file-move rule at
  `MacCommandParser.swift:137-142` needs `movePair`, which requires **two
  quoted** values (`MacCommandParser.swift:393-398`); `"move the cursor to 400
  300"` has none. So it falls through.
- `ConversationService.swift:138` — `self.api.post("/command", body: request,
  ...)`. **This is the endpoint Command Center uses.**

### 2.2 Endpoint → planner: **breaks here**

- `jarvis/api/app.py:1044-1070` — `POST /command` builds a `CommandRequest`
  and calls `core.handle(...)`.
- `jarvis/core/orchestrator.py:1053` `handle()` → `:1081`
  `self.planner.parse(request.text, request.context)` — the **natural-language
  rule planner** (`jarvis/core/planner.py`), not `jarvis/desktop/planner.py`.
- `grep -rn "plan_desktop" jarvis/` returns exactly three call sites, none in
  `orchestrator.py`:
  - `jarvis/api/desktop_routes.py:278,285` (inside `GET /desktop/plan`)
  - `jarvis/api/missions_routes.py:61,74` (inside `POST /missions/plan`)
  - definition at `jarvis/desktop/planner.py:75`

So `/command` has **no path** to `plan_desktop`. The desktop planner and the
command pipeline are two disconnected graphs.

### 2.3 The mission endpoint is reachable over HTTP but not from the UI

- `jarvis/api/missions_routes.py:48-106` — `POST /missions/plan`, registered
  (`jarvis/api/app.py:632-634`) and live: `GET /missions/plan` → **404**,
  meaning the path exists with POST only, not that it is missing.
- `grep -rn "missions/plan" macos/Vantage/Sources/` → **zero matches.**
  No Swift file anywhere in `macos/` references it.
- The Swift mission screens POST to `/missions` (create) and
  `/missions/{id}/run`: `MissionControlView.swift:339` (create with `steps: []`),
  `AutomationStudioView.swift:598,619` (create from a hand-authored studio
  draft, then run). Neither turns a typed sentence into steps.
- `DesktopOperatorView.swift:748` calls `GET /desktop/plan?…`, and
  `jarvis/api/desktop_routes.py:269-292` does route it to `plan_desktop` — but
  the view's own result label says **"Preview only — planning does not execute
  anything; no desktop action was sent"** (`DesktopOperatorView.swift:768`).
  There is no POST route that accepts a plan and runs it; the sibling routes
  are single verb calls (`/desktop/move`, `/desktop/click`, …).

## 3. Real responses (2026-11-17, this backend)

### `POST /missions/plan` — the path that actually plans a desktop action

```json
{"status":"planned","request":"move the cursor to 400 300","steps":[{"step_id":"S1","title":"Move the pointer to (400, 300)","tool":"desktop_move_pointer","params":{"x":400.0,"y":300.0},"depends_on":[],"idempotency_key":""}],"clarification":null,"notes":[]}
```
HTTP 200. Correct plan, correct tool. **Unreachable from the UI.**

### `POST /command` — what Command Center actually calls

```json
{"request_id":"REQ-b1438eb992b1","session_id":"audit-probe","reply":"This needs your confirmation before I run it.\n  Will do: move_file on laptop_main | source=the cursor | destination=400 300\n  Tool: move_file\n  On device: laptop_main\nReply with /confirm move_file to approve, or /cancel to stop.","intent":"move","plan":{"goal":"move the cursor to 400 300","intent":"move","actions":[{"step_id":1,"tool":"move_file","params":{"source":"the cursor","destination":"400 300"},"device":"laptop_main","reason":"matched intent move","status":"awaiting_confirmation"}],"planner":"rules"},"decision":"CONFIRM","needs_confirmation":true,"confirmation_id":"CONF-910e12cba2d749666c87ffcc52af8141","mission_id":"MIS-3247adf50aa2","model_used":"rules","duration_ms":7}
```
HTTP 200, with `Source: native` semantics. The follow-up `GET
/missions/MIS-3247adf50aa2` reports `"state":"blocked"`, `"reason":"waiting for
approval before any action ran"`, and a step `{"tool":"move_file","params":
{"source":"the cursor","destination":"400 300"}}`.

## 4. What happens today for "move the cursor to 400 300"

1. Command Center sends it to `POST /command`.
2. The Swift Mac-control parser declines (no quoted pair), so nothing is done
   natively.
3. The rule planner's generic move rule (`jarvis/core/planner.py:1550`)
   matches "move X to Y" and proposes **`move_file`** — a *filesystem rename*
   of `the cursor` to `400 300`.
4. The policy gate holds it (`CONFIRM`) and a mission is created in `blocked`.
5. If the person approves, the `move_file` agent receives
   `source="the cursor"`, a path that does not exist. The pointer is never
   moved. The desktop planner is never consulted.

This is not a wiring gap that degrades gracefully — it is the **wrong tool**,
proposed with confidence, for a desktop instruction.

## 5. What would need wiring

1. `macos/Vantage/Sources/Services/ConversationService.swift` — add a
   desktop-intent branch before `:138` that calls `POST /missions/plan`, or
   route desktop phrases to a mission-preview surface. This is the only hook
   point Command Center has.
2. `jarvis/core/planner.py:1550` — the `move` rule must stop treating a
   coordinate/pointer subject as a file rename. Today only a bare `(x, y)`
   destination defers (`is_coord`); `400 300` without parentheses does not.
3. `macos/Vantage/Sources/Views/CommandCenterView.swift` — needs a step-preview
   and an approve/run affordance for a plan, and a call to
   `POST /missions/{id}/run`; it currently has no mission UI at all.
4. `jarvis/api/missions_routes.py:48` — `POST /missions/plan` returns steps but
   creates nothing; a plan → mission → run path (or a create-with-planned-steps
   call) has to be added on the client, since no route chains the three.
5. Optional: `DesktopOperatorView.swift:748-786` already shows the correct
   preview for this sentence — its `GET /desktop/plan` result could be the
   model for what Command Center should display, but that view executes nothing
   either.