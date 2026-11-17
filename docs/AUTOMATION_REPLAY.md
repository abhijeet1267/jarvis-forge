# Automation Studio — multi-step replay, verified at the target

**Script:** `benchmarks/v23_studio/studio_roundtrip.py`
**Run:** `VANTAGE_TEST_PORT=8798 .venv/bin/python benchmarks/v23_studio/studio_roundtrip.py`
**Date:** 2026-11-17 · revision `b51a47acc9d7+dirty` · macOS 25.3.0 (Darwin 25.3.0, 1352×878 pt display)
**Result:** **17 passed, 0 failed, 0 skipped, exit 0** — with the fixture running.
No-fixture path: **8 passed, 0 failed, 2 skipped, exit 0** (the fixture read and
the window move are the two skips).
Blocked path (`VANTAGE_TEST_PORT=8765`): **exit 2**, one `BLOCKED`, no request made.

The prior verified claim (`benchmarks/v21_desktop/studio_replay.py`) covered a
*single* recorded `desktop_click`. This covers a **3-step** workflow: record,
save, move the target window, replay, and check the target's own state.

## What was recorded

Three steps through the real HTTP surface, on the isolated backend
(`:8798`, `JARVIS_HOME=/tmp/vantage_probe2`), never `:8765`:

| # | kind | params | note |
|---|---|---|---|
| 1 | `desktop_move_pointer` | the point its own state file publishes as `targets.click` centre, offset +240,+240 | LOW risk, ungated |
| 2 | `desktop_move_pointer` | the button centre | LOW risk, ungated |
| 3 | `desktop_click` | the button centre | DESTRUCTIVE, one approval |

Endpoints: `POST /intelligence/recordings` → `POST /intelligence/recordings/steps`
(×3) → `POST /intelligence/recordings/stop` → `POST /intelligence/recordings/draft`.
All returned 200, all three steps `skipped: false`, draft reported
`3 step(s), needs_input 0, verdict=unverified, conditions=0`.

The button centre is read through **Accessibility** (`read_element …
role=AXButton` → `center_x`/`center_y`), not from the fixture's `state.json`
targets. That distinction turned out to matter — see the defect below.

## What replayed, and what was observed

Each replay is a real mission: `POST /missions` with the draft's steps, then
`POST /missions/{id}/run`, and the approval loop that reads the confirmation id
back off the parked step (a fresh id is minted per gate call, so the one recorded
on the step is the only one an approval can match).

**Pass 1 — before the window moves.** Owner state
`partially_completed`, `verdict=unknown`, steps `['done','done','done']`.

`partial/unknown` is the **correct** verdict here, not a replay failure: the
recorder proposes no acceptance condition for a pointer move or a click (no
trusted observer exists for "the button really fired"), so `Mission.verdict()`
has nothing to check. Treating `completed` as the pass criterion was my first
mistake and it produced a false failure; the script now requires every step to
have **run**, and reports the verdict as an observation.

Real effects, read from the target rather than the mission:

- fixture counter **0 → 1** (its `clicks` field, written by its own button handler)
- pointer readback after the run **(180.0, 220.5)** — the recorded point —
  from `CGEvent(source: nil)?.location`, i.e. the real cursor
- each `desktop_move_pointer` step's detail carried a readback matching its
  recorded point

**Window moved.** `System Events … set position of window 1` from (120, 86) to
(420, 186), confirmed by the window server (`windows` reports (420, 186)). The
button's live AX centre moved (180, 220.5) → **(480, 320.5)**.

**Pass 2 — replay at fresh coordinates.** The script asks the Studio's own review
endpoint (`POST /intelligence/recordings/draft {"steps": [...]}`) for a draft
whose step 2 and 3 carry the fresh AX centre, then runs it the same way.

Observed: mission step params are `(480.0, 320.5)` — the fresh values, not the
recorded `(180, 220.5)`; owner state `partially_completed`, steps all `done`;
executor readbacks `(480.0, 320.0)` and `(480.0, 320.5)`; fixture counter
**1 → 2**; pointer readback `(480.0, 320.5)`.

**Negative control (run by hand, outside the script).** With the window shifted
so the button sat at (220, 260.5), posting a click at the *recorded* point
(180, 220.5) returned `performed_unverified` and left the counter at **2** — the
event was accepted and hit nothing. The *fresh* AX point moved it to **3**. So
the window move genuinely invalidates the recorded coordinates, and the pass-2
success is attributable to the fresh values, not luck.

**Window restored** to (120, 86) in a `finally` block; verified through the
window server.

## Defect found

**Automation Studio stores absolute global points and re-resolves nothing at
replay time.** This is the design (documented: a recorded step is a tool call
with parameters), but nothing in the recording path re-derives them:

- Re-deriving the draft *after* the window moved still yields the **recorded**
  `(180, 220.5)` (step `draft freshness`, PASS = recorded values kept).
- The mission that runs carries those recorded values verbatim.
- No code path in `jarvis/core/recorder.py`, `jarvis/tools/desktop_native.py`,
  or `AutomationStudioView.swift` calls `desktop_windows`/`read_element` to
  refresh a stored coordinate. `desktop_click` accepts `expect_window_id` but the
  recorder does not capture or supply one, so the executor's staleness guard
  cannot fire for a recorded workflow.

Consequence: a saved workflow replayed after its window moves silently aims at
the old place. `desktop_click` returns `performed_unverified` (never
`verified`), so nothing claims success — but the mission still reports every
step `done`, and the only thing that distinguishes "clicked the button" from
"clicked the desktop wallpaper" is an observation the recorder does not propose.
The freshness step is therefore an operator/UI responsibility today, not a
product guarantee. Whether that is acceptable is a product decision; it is
recorded here as a gap, not a crash.

**Fixture observation, not a Vantage defect:** `vantage-probe`'s `state.json`
`targets` are cached and do **not** follow a window move (it still reported
(180, 220.5) after the window moved to (420, 186), while AX correctly reported
(480, 320.5)). Its `frames` are also stale. A test that trusts `state.json`
targets after moving the fixture measures the wrong point. Live AX is the honest
source; the script uses it and says so when they disagree.

## Not verified

- **Window-move resilience when no fixture runs.** With no fixture the script
  records three pointer moves to distinct on-screen points and verifies each by
  readback; it skips the move and prints that window-move resilience is NOT
  verified. It cannot be exercised without a movable target.
- **A human driving the UI.** Everything here goes through the same HTTP calls
  `saveAndRun`/`runMission`/`approveParked` make, not by clicking the Studio's
  controls. The Swift `read_element`/`desktop_windows` freshness path — if one is
  added — is not covered.
- **Non-pointer recorded kinds.** `desktop_type_text`, `desktop_press_key`,
  `desktop_shortcut`, `desktop_drag`, `desktop_scroll` and
  `desktop_press_element` are recordable and replayable in principle; none was
  exercised here.
- **Approvals beyond one DESTRUCTIVE step.** One click, one confirmation. The
  multi-approval ordering was verified in the V12 journey, not re-verified here.
- **The installed app.** All of this is the development checkout against the
  dev executor bundle granted in System Settings. The packaged app's own
  executor identity was not exercised.

## Reproducing

```bash
scripts/dev_port_guard.sh check 8765        # must be held by the app: exit 1
scripts/dev_port_guard.sh claim             # e.g. 8799 — free
# start a test backend on that port if none is running:
#   JARVIS_HOME=/tmp/vantage_probe2 JARVIS_BACKEND_PORT=<port> \
#     .venv/bin/python -m uvicorn jarvis.api.app:app --host 127.0.0.1 --port <port>
tools/input_probe/vantage-probe &           # optional; omit to skip the move
VANTAGE_TEST_PORT=<port> .venv/bin/python benchmarks/v23_studio/studio_roundtrip.py
```

Exit codes: `0` every check that could run passed · `1` a check failed · `2`
blocked (no backend, no desktop permission, or the window could not be moved).
`VANTAGE_TEST_PORT=8765` is refused before any request is made. An open recording
left by an earlier run is stopped first (verified: `recording cleanup` PASS).