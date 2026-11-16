# V14 — real desktop control: what works, and what macOS will not allow here

This directory holds the acceptance run for real pointer, keyboard and
Accessibility control. Run it against a live backend:

```bash
JARVIS_HOME=/tmp/vantage_probe2 JARVIS_BACKEND_PORT=8798 \
  .venv/bin/python -m uvicorn jarvis.api.app:app --host 127.0.0.1 --port 8798
```

```bash
.venv/bin/python benchmarks/v14_desktop/acceptance.py
```

## The measurement that shaped the design

Three permission checks, read fresh on this machine, from a newly built binary:

| Check | Value | What it gates |
|---|---|---|
| `CGPreflightPostEventAccess()` | **false** | clicks, drags, scrolls, keystrokes |
| `AXIsProcessTrusted()` | **false** | reading and pressing UI elements |
| `CGPreflightScreenCaptureAccess()` | **true** | screenshots, and cursor warping |

What that produced, measured rather than assumed:

- `CGEvent.post` with post-event access **false** returns *no error and does
  nothing*. A click "sent" that way moves nothing.
- `CGWarpMouseCursorPosition` moves the **real** pointer with only screen-recording
  access: observed (701,718) → (731,748), readback confirmed.
- `osascript ... click at {x,y}` exits **1** with
  `osascript is not allowed assistive access (-25211)`. The pre-existing
  AppleScript `click_desktop` therefore could not click, and reported that
  honestly.

So the capability is **split by what macOS actually authorizes**, and the whole
implementation reports which half is available instead of implying both are.

## Why a separate binary

macOS grants input permission per **binary identity**. A Python script, a
`swift run` harness and the app in the Dock are three different subjects, and a
grant to one is not a grant to another. The executor is therefore a compiled
artifact at a stable path (`native/desktop_executor/vantage-desktop`), and every
result names the executable it describes.

Build it with:

```bash
./native/desktop_executor/build.sh
```

## Granting permission (this cannot be done for you)

```bash
./native/desktop_executor/vantage-desktop request_access
```

That shows macOS's own dialog. Then in **System Settings → Privacy & Security →
Accessibility**, add `vantage-desktop` from
`native/desktop_executor/`. Quit and run it again — the grant is read at process
start.

**A rebuilt binary can be a different identity.** If you rebuild to a different
path, the old grant may not follow. The path must stay stable, which is why
`build.sh` writes to a fixed location.

## What the acceptance run reports today

| Demo | Status | Level |
|---|---|---|
| A — move the cursor | **VERIFIED** | 3 — pointer moved, readback confirmed |
| B — click a button | PERMISSION BLOCKED | 1 |
| C — type into TextEdit | PERMISSION BLOCKED | 1 |
| D — scroll | PERMISSION BLOCKED | 1 |
| E — double/right click | PERMISSION BLOCKED | 1 |
| F — drag and drop | PERMISSION BLOCKED | 1 |
| G — keyboard shortcut | PERMISSION BLOCKED | 1 |
| H — multi-step mission | **PARTIALLY VERIFIED** | 3 — the move step ran through the real gate and verified; the type step stopped on permission |
| I — permission refusal | **VERIFIED** | names the executable and the fix, target untouched |
| J — human takeover | **VERIFIED** | a move during takeover returns HTTP 409 |

Level 1 = the request reached the executor. Level 2 = a system event was posted.
Level 3 = the target UI actually changed, observed. Nothing above claims level 3
without a readback.

## What is genuinely not done

- **No click, keystroke, scroll or drag has ever executed on this machine.** They
  are refused before acting because post-event access is not granted. Granting it
  is a user action; the code path is implemented and will report
  `performed_unverified` (not `verified`) until a separate observation confirms
  the target changed.
- **Accessibility element targeting is implemented but untestable here** until
  Accessibility is granted — `inspect`, `press_element`, `read_element` and
  `verify_change` all refuse with the executable named.
- **Demo H is not a complete desktop mission.** Its move step is real and
  verified; its typing step cannot run without the grant.