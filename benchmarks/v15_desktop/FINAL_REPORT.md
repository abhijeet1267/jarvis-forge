# V15 — the final report

The campaign asked for real native desktop automation: a cursor that actually
moves, a click that actually lands, keys that actually type. This file reports
what is true on this machine, measured, including what it refuses.

It is deliberately short. The main result is the T1–T10 table; everything else is
what was built to get there and what blocked the rest.

---

## The headline, without softening

**Two of the ten required demonstrations are verified, one is partial, and seven
cannot run because macOS has not granted this executable permission to post input
events.** No click, keystroke, scroll or drag has ever executed on this machine.

That is not a code gap. The executor is complete and every input operation checks
permission *before* acting and returns a refusal naming the binary and the switch.
The gate is doing exactly what it should. Granting it is a person's decision, made
in System Settings, and this session did not attempt to make it another way.

## T1–T10

Levels: **1** the request reached the executor · **2** a real system event was
posted · **3** the target actually changed, and was read back.

| Demo | Result | Level |
|---|---|---|
| T1 — cursor | **VERIFIED** | 3 — four legs, each move confirmed by reading the pointer back |
| T2 — left click | PERMISSION BLOCKED | 1 |
| T3 — double click | PERMISSION BLOCKED | 1 |
| T4 — right click | PERMISSION BLOCKED | 1 |
| T5 — typing | PERMISSION BLOCKED | 1 |
| T6 — shortcut | PERMISSION BLOCKED | 1 |
| T7 — scroll | PERMISSION BLOCKED | 1 |
| T8 — drag | PERMISSION BLOCKED | 1 |
| T9 — TextEdit mission | **PARTIALLY VERIFIED** | 3 — opened TextEdit, moved the real pointer into its window and verified it, then stopped at the click on permission |
| T10 — stop and takeover | **VERIFIED** | 1 — a desktop action during a human takeover returns HTTP 409 |

**2 verified · 1 partial · 7 blocked · 0 failed.**

Run it yourself:

```bash
JARVIS_HOME=/tmp/vantage_probe2 JARVIS_BACKEND_PORT=8798 \
  .venv/bin/python -m uvicorn jarvis.api.app:app --host 127.0.0.1 --port 8798
.venv/bin/python benchmarks/v15_desktop/acceptance.py
```

## Native permissions, measured

| Switch | Value | What it gates |
|---|---|---|
| `CGPreflightPostEventAccess()` | **false** | every click, keystroke, scroll, drag |
| `AXIsProcessTrusted()` | **false** | reading and pressing UI elements by label |
| `CGPreflightScreenCaptureAccess()` | **true** | window bounds, and cursor warping |

`CGWarpMouseCursorPosition` needs only screen-recording, which is why the pointer
moves for real while every event that lands on a target is refused before it is
posted.

## What was built

- **A native executor** (`native/desktop_executor/`) that moves the real pointer,
  posts clicks, types through keyboard events, scrolls, drags with the button
  released on every exit path, reads and presses Accessibility elements, and
  refuses a secure field, an authorization prompt or a locked screen before typing
  a character.
- **Chained planning.** `jarvis/desktop/planner.py` turns a sentence into ordered
  steps without ever inventing a target. The full demonstration sentence plans as
  five steps — open, move, click, type, cmd+s — and an ambiguous one returns a
  question and no steps.
- **Mission integration.** `POST /missions/plan` plans that sentence into
  dependency-chained mission steps, checked against the real tool registry.
- **Window geometry without Accessibility.** Reads the window server on
  screen-recording alone; measured TextEdit at (273, 208) 656×422.
- **An identity read** that reports the executable, bundle id, signature state and
  SHA-256, and says plainly that an ad-hoc signature does not survive a rebuild.
- **A dev app bundle** (`VantageDesktopDev.app`, `com.jarvisforge.vantage.desktopdev`,
  version 15.0) so there is a stable, findable thing to grant. It grants nothing
  itself.
- **Automation Studio** can now record and replay the eleven desktop actuation
  kinds, still confirmed through the permission engine on replay.
- **The Desktop Control screen** shows the probe's per-leg evidence, and refuses a
  malformed coordinate instead of sending the pointer to (0, 0).
- **A probe fixture** that publishes real observables for the four blocked demos
  (menu openings, text selection, scroll offset, dropped item), ready for the grant.

## Three real defects found by running things

1. **TextEdit was not in the app catalog.** The required demonstration could not
   open the application it names — `open_application` answered "I could not find an
   application matching 'TextEdit'". Found by running T9, not by reading code.
2. **A typed secret was stored.** The recording guard inspected parameter *names*,
   so `{"text": "password=hunter2"}` was accepted, would have replayed the password
   on every run, and sat in the draft in plain text. Found by driving the live
   endpoint. Fixed and re-verified: `skipped=true, params={}`.
3. **A malformed coordinate moved the real pointer to (0, 0).** The field parsed
   with `?? 0`. It is refused now.

Each was found by exercising the thing, which is the only way any of them would
have surfaced.

## Tests, with the revision they ran at

| Suite | Result |
|---|---|
| `tests/test_desktop_planner.py` | 58 passed |
| `tests/test_desktop_native.py` | 29 passed |
| `tests/test_recorder.py` | 40 passed |
| `tests/test_missions_api.py` | 30 passed |
| `tests/test_phase28_reading_is_not_writing.py` | 33 passed |
| `test_phase41/39/51/52`, `test_r43_browser_slice` | 166 passed |
| Full suite | see `docs/PROJECT_STATUS.md` — the current figure is recorded there |

The published tool count moved 196 → 200; the test that pins README against the
live registry caught the drift, which is why it exists.

## What is genuinely not done

- **No click, keystroke, scroll or drag has ever executed here.** They are refused
  before acting.
- **T9's save step has never run.** The plan includes `cmd+s`, but saving to a
  named path means driving the save panel, which needs the same post-event access.
  No file has been produced by typing.
- **The four blocked demos' observables have never been exercised by a real
  event.** They were cross-checked by driving the fixture's own AppKit callbacks;
  that is plumbing proof, not actuation proof.
- **There is no Developer ID on this machine**, so the bundle is ad-hoc signed and a
  grant will not survive a rebuild at the same path.

## The one thing that would change all of this

Grant post-event access to
`native/desktop_executor/VantageDesktopDev.app`, then re-run the acceptance script.
Nothing in the code needs to change: the refusal path is the only path that has ever
run, and the success path is written and waiting behind it.

```bash
open -R native/desktop_executor/VantageDesktopDev.app
# drag it into System Settings → Privacy & Security → Accessibility, then:
.venv/bin/python benchmarks/v15_desktop/acceptance.py
```
