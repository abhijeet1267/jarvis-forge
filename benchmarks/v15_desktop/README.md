# V15 — real desktop automation, measured

The acceptance run for the ten mandatory demonstrations (T1–T10). It talks to a
live backend and a live fixture, and it reports the level each demo actually
reached.

```bash
JARVIS_HOME=/tmp/vantage_probe2 JARVIS_BACKEND_PORT=8798 \
  .venv/bin/python -m uvicorn jarvis.api.app:app --host 127.0.0.1 --port 8798
tools/input_probe/vantage-probe &          # a disposable target, does nothing on its own
.venv/bin/python benchmarks/v15_desktop/acceptance.py
```

Levels: **1** the request reached the executor · **2** a real system event was
posted · **3** the target UI actually changed, observed. Only level 3 is proof
that an action worked.

## Measured on this machine (2026-11-16)

| Check | Value | Gates |
|---|---|---|
| `CGPreflightPostEventAccess()` | **false** | click, drag, scroll, keystroke |
| `AXIsProcessTrusted()` | **false** | reading and pressing UI elements |
| `CGPreflightScreenCaptureAccess()` | **true** | screenshots, and cursor warping |

| Demo | Status | Level |
|---|---|---|
| T1 — cursor | **VERIFIED** | 3 — pointer readback per leg |
| T2 — left click | PERMISSION BLOCKED | 1 |
| T3 — double click | PERMISSION BLOCKED | 1 |
| T4 — right click | PERMISSION BLOCKED | 1 |
| T5 — typing | PERMISSION BLOCKED | 1 |
| T6 — shortcut | PERMISSION BLOCKED | 1 |
| T7 — scroll | PERMISSION BLOCKED | 1 |
| T8 — drag | PERMISSION BLOCKED | 1 |
| T9 — TextEdit mission | **PARTIALLY VERIFIED** | 3 — the move step ran and verified; the type step stopped on permission |
| T10 — stop and takeover | **VERIFIED** | 1 — a move during takeover returns HTTP 409 |

**2 verified, 1 partial, 7 permission blocked, 0 failed.**

## Why the cursor works and the click does not

They are authorized by different switches. `CGWarpMouseCursorPosition` is
permitted with only screen-recording access, so the real pointer moves and can be
read back. `CGEvent.post` — every click, keystroke, scroll and drag — needs
post-event access, which this helper does not have. The cursor square below is
therefore a genuine Level-3 result while every input demo beside it is blocked:

```
/desktop/probe  →  4 legs, each observed at the requested point
```

## What the code does regardless of the grant

The refusal is a first-class result, not an error path:

- Every input operation checks permission **before** posting and returns
  `permission_required`, naming the executable and the switch to flip.
- An event that was posted but not observed reports `performed_unverified`, never
  `verified`. Only a readback that confirms the target changed sets `verified`.
- A drag that is cancelled or fails releases the mouse button on **every** exit
  path (`defer` is not enough: `Emit.out` calls `exit()`, which skips it).
- Typing refuses a secure text field, a macOS authorization prompt, or a locked
  screen before it posts a single character.
- A desktop-action request during a human takeover is refused with HTTP 409.

## Granting the permission

This cannot be done for you, and the reproduction deliberately does not try:

```bash
./native/desktop_executor/vantage-desktop request_access
```

Then add `vantage-desktop` from `native/desktop_executor/` in **System Settings →
Privacy & Security → Accessibility**, quit and rerun it (the grant is read at
process start), then run the acceptance script again. A rebuilt binary can be a
different identity; the path must stay stable, which is why `build.sh` writes to a
fixed location.

## What is genuinely not done

- **No click, keystroke, scroll or drag has ever executed on this machine.** They
  are refused before acting. The code path is complete and tested at the boundary,
  but it has never been exercised end to end.
- **Cancellation is implemented but unexercised live** — the permission gate
  refuses a drag before the loop that checks the cancel flag begins.
- **T4, T6, T7 and T8 would be Level 2 at best even with the grant**: the fixture
  does not publish the state those demos need (chosen menu item, text selection,
  scroll offset, dropped item). Making them Level 3 requires extending the fixture,
  not the executor.
- **TextEdit is not opened or driven.** T9 runs a two-step mission whose move step
  is real; the typing step cannot run without the grant, and the save step does not
  exist yet.