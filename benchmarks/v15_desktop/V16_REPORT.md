# V16 — the completion report

The campaign asked for working desktop automation, real parallel work, and no
invented results. This file reports what is true at revision `85dd1ca`, measured
on this machine, including the part the machine refuses.

---

## A. What was genuinely performed on macOS

| Operation | Performed? | Evidence |
|---|---|---|
| Move the real cursor | **Yes** | four legs, each read back: asked (412,336), pointer reported (412,336) |
| Read window geometry | **Yes** | TextEdit at (273,208) 656×422, from the window server |
| Focus an application | **Yes** | `frontmost_after: TextEdit` |
| Open an application | **Yes** | T9 step S1, through `open_application` |
| Click | **No** | refused before acting; `CGPreflightPostEventAccess()` is false |
| Type text | **No** | refused before acting |
| Scroll / drag / shortcut | **No** | refused before acting |

**No click, keystroke, scroll or drag has ever executed on this machine.** That is
the permission gate doing its job, not a missing implementation.

## B. The native executor and its permissions

```
bundle id     com.jarvisforge.vantage.desktopdev
bundle        native/desktop_executor/VantageDesktopDev.app   (version 15.0, revision in Info.plist)
executable    …/Contents/MacOS/vantage-desktop
signing       adhoc          (no Developer ID exists here: 0 valid identities)
```

| Switch | Value | Gates |
|---|---|---|
| `CGPreflightPostEventAccess()` | **false** | every click, keystroke, scroll, drag |
| `AXIsProcessTrusted()` | **false** | reading and pressing UI elements |
| `CGPreflightScreenCaptureAccess()` | **true** | window bounds, cursor warping |

The backend shells out to that binary (`jarvis/desktop/executor.py:_run`), so macOS
evaluates **that binary's** identity. `vantage-desktop identity` prints all of the
above from the running process rather than from a hand-written table.

## C. Acceptance, T1–T15

Levels: **L1** source · **L2** build · **L3** bridge executes · **L4** native event
issued · **L5** target state observed · **L6** complete workflow verified.

| Test | Result | Level |
|---|---|---|
| T1 cursor | **VERIFIED** | L5 — pointer readback per leg |
| T2 left click | PERMISSION BLOCKED | L3 — refused, nothing posted |
| T3 double click | PERMISSION BLOCKED | L3 |
| T4 right click | PERMISSION BLOCKED | L3 |
| T5 keyboard | PERMISSION BLOCKED | L3 |
| T6 shortcut | PERMISSION BLOCKED | L3 |
| T7 scroll | PERMISSION BLOCKED | L3 |
| T8 drag | PERMISSION BLOCKED | L3 |
| T9 TextEdit | **PARTIAL** | L5 — opened TextEdit, moved the real pointer in, stopped at the click |
| T10 takeover | **VERIFIED** | L5 — 409 during takeover, 17 workers paused, held-input release *reported* |
| T11 invalid coordinates | **VERIFIED** | L5 — 422, pointer unchanged |
| T12 permission denied | **VERIFIED** | L5 — refuses and names the binary and the switch |
| T13 stale target | **VERIFIED** | L5 — a dead window id is refused; a live one passes the check |
| T14 automation replay | **VERIFIED** | L5 — a recorded workflow replayed through the mission runner |
| T15 multi-display | **VERIFIED** | L5 — the centre of each display reached and read back |

**7 verified · 1 partial · 7 permission-blocked · 0 failed.**

T2–T9 are deliberately not called complete: an API returning 200 is not evidence
of a click, and nothing here does that.

## D. What was built this campaign

- **One coordinate parser** (`jarvis/desktop/coordinates.py`). It replaced three
  drifted copies and now judges every point against real display geometry. NaN,
  infinity, empty strings, booleans and non-numbers are each refused with a
  sentence saying what was wrong. Negative coordinates are valid when a display
  sits left of the primary one — the old check refused them blindly.
- **Stale-target refusals.** A click can name the window it was resolved against;
  if that window closed or moved more than 2 points, the click is refused before
  anything is posted. Typing likewise checks the frontmost application and refuses
  if it is not the one aimed at. Both checks run *before* the permission request,
  because a stale target is knowable without permission.
- **A per-operation capability report** (`vantage-desktop capabilities`): which
  permission gates each op and whether that gate is currently open.
- **A real capability matrix in Desktop Control** — seven rows, five states
  (AVAILABLE, PERMISSION_REQUIRED, UNSUPPORTED, UNKNOWN, ERROR), with AVAILABLE
  requiring an explicit `true`. The derivation was proved against the live
  `/desktop` JSON with a standalone program.
- **A takeover that releases held input.** Pausing stops the next dispatch; it
  did nothing about a drag already in progress with the button down.
- **T11–T15** added to the acceptance harness, with the honest failure modes
  (T11 reports L2 when the pointer is moving for other reasons).

## E. Bugs fixed, with root causes

1. **`/desktop/move` had no bounds check at all.** `nan` and `inf` reached the
   native executor. Root cause: the check was re-implemented three times and this
   door had none.
2. **Negative coordinates were rejected everywhere.** A monitor left of the
   primary has negative global x; a click there could not be planned or made.
3. **A takeover left held mouse buttons down.** Root cause: it paused workers and
   stopped, and pausing only stops the *next* dispatch.
4. **Typing went to whatever was focused**, with no check that it was the intended
   application.

## F. Tests

| Suite | Result |
|---|---|
| `test_desktop_coordinates.py` (new) | 30 passed |
| `test_desktop_planner.py` | 60 passed |
| `test_desktop_native.py` | 31 passed |
| `test_reality_hud.py` | 17 passed |
| Full regression at `85dd1ca` | see the run recorded in `docs/PROJECT_STATUS.md` |

Two tests that pinned the *old* buggy behaviour were corrected rather than
deleted — `drag from -1 100` is now valid, and the off-display case now asserts
the refusal names the geometry. The invariants they protected (nonsense refused;
nothing lands off-screen) are unchanged.

## G. Multi-agent work, honestly

Two specialist workers ran concurrently in isolated git worktrees (native input
engineer; SwiftUI engineer), plus the lead. **Both worktrees were cut from a stale
base** that predated the desktop subsystem, so the Swift worker built a parallel
view against files that did not exist there. Its five-state derivation logic was
sound and was taken into the real view; its duplicate model definitions were not.
The native worker's output was verified by running it before being accepted.

This is reported because "fourteen agents worked in parallel" would be false. Two
ran, one was cut from the wrong base, and the integration was done by hand after
checking the output rather than trusting the reports.

## H. Remaining blocker — exactly one

**Post-event access for `VantageDesktopDev.app`.**

```bash
open -R native/desktop_executor/VantageDesktopDev.app
# drag it into System Settings → Privacy & Security → Accessibility, then:
.venv/bin/python benchmarks/v15_desktop/acceptance.py
```

Nothing in the code needs to change. The refusal path is the only path that has
ever run; the success path is written, built and waiting behind the gate. Because
the signature is ad-hoc, a rebuild may require the grant again — `identity` says so
rather than implying a stable grant.

## I. What is not claimed

- No click, keystroke, scroll or drag has executed here.
- T9's save step has never run; no file has been produced by typing.
- The four blocked demos' level-3 observables have never been driven by a real
  event — they are plumbing-verified, not actuation-verified.
- The desktop UI has not been visually verified in a running GUI session; it
  builds clean, and its state derivation was proved against real JSON.
