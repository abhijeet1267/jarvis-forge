# V23.4 release closure — report

**Date:** 2026-11-18 · **HEAD at report:** see `git log -1` · **Executor source revision:** `e657218` + the press guard commit

## Workers

Two real subagents were launched in this campaign:

| worker | assignment | outcome |
|---|---|---|
| A5 — remaining task audit | verify the 8 non-completed tracker entries | **delivered** — `docs/REMAINING_TASKS_AUDIT.md`; three records corrected |
| A3 — guard completeness | audit every acting desktop operation for target guards | **stopped after 23 min with no writes** — I completed its question directly (`press_element` gap found and fixed) |

Execution mode: independent parallel subagents. Agent Teams were not used. Peak
concurrency: 2. The rest of this campaign was done directly, and the report does
not present that as agent work.

## The defect that was shipped

`locate()` took `hits.first` when several differently-described Accessibility
elements matched. Measured against the input fixture: `keyword="Click"` matches
**three** elements — the button, its counter label, the right-click target. The
old binary pressed whichever the AX walk reached first and reported success. A
`target_ambiguous` status existed in the enum and was never emitted.

## Promotion chain — verified at every stage

| stage | artifact | identity | has fix |
|---|---|---|---|
| source | `native/desktop_executor/main.swift` | rev `e657218`+ | yes |
| staged | `vantage-desktop.staging` | CDHash `4afafcc5` | yes |
| promoted | `native/desktop_executor/vantage-desktop` | CDHash `4afafcc5` | yes |
| dev bundle | `VantageDesktopDev.app/…/vantage-desktop` | SHA-256 `f786a64b…` | yes |
| **app bundle** | `Vantage.app/Contents/Resources/native/…` | SHA-256 `f786a64b…` | yes |
| **DMG** | `dist/Vantage-0.2.0.dmg` (45 MB) | SHA-256 `f786a64b…` | yes |

Byte-identical from the dev bundle through the DMG. The bare binary differs only
because `make_dev_app.sh` re-signs its copy.

## Acceptance, run against the shipping binary

| case | expected | observed |
|---|---|---|
| `read_element keyword="Click"` | `target_ambiguous` | `target_ambiguous`, 3 matches |
| `+ role="AXButton"` | `target_ambiguous` | `target_ambiguous`, 2 matches |
| `keyword="probe.field"` | resolves | `verified`, `probe.field` |
| no side effect | clicks unchanged | `clicks = 0` |

The approval card was rendered by the DMG's own binary; all six PNGs match the
committed evidence **by SHA-256**, so the recorded images are what the artifact
produces rather than a snapshot of whatever was on disk.

Command Center journey, live: **3/3** — deny parks and writes nothing; approve
writes exactly the approved bytes; a forged id is refused and the issued id is
unchanged.

## Release gate

| | status |
|---|---|
| IMPLEMENTED | **yes** |
| STAGED | **yes** |
| BUNDLED | **yes** — app bundle and DMG |
| RUNNING | **no** — the app currently open (pid 65474) predates this rebuild |
| VERIFIED | **yes as a binary** — measured against the DMG's own executor |

## Decision: READY WITH LIMITATIONS

**What is ready.** The DMG at `dist/Vantage-0.2.0.dmg` contains a self-contained
SwiftUI app whose backend runs from its own bundled Python 3.11 and whose desktop
executor refuses an ambiguous target instead of pressing an arbitrary one. Both
properties were measured on the artifact, not inferred from a build succeeding.

**What limits it.**

1. **The installed app is not this app.** `/Applications/Vantage.app` is a build
   from 2026-11-15 whose `Contents/Resources/` holds exactly one file —
   `Vantage.icns`. No Python, no backend, no executor. Installing the DMG is the
   user's step; it interrupts whatever they have open.
2. **Finder-launched physical input is blocked on a permission.** macOS grants
   Accessibility to a *responsible process*: the same binary reports
   `accessibility: true` from a terminal and `false` under `Vantage.app`. Granting
   the executor or `VantageDesktopDev.app` cannot work. See
   `docs/DESKTOP_PERMISSION_MODEL.md`.
3. **The press guard's enforcement path is unproven.** `press_element` now accepts
   and forwards `expect_window_id`/`expect_pid` and declares them; forwarding and
   declaration are tested and mutation-proven. But the fixture's window stopped
   publishing an `AXWindow` in this session — for the old binary as well — so a
   real ambiguous press could not be exercised.
4. **Automation Studio's second run was environment-blocked.** The first run
   stands at 17/17 with a real counter change 1 → 2. The re-run could not bring
   the fixture to the front and refused rather than acting on the wrong window.
5. **Android remains blocked, now two ways:** no device, and the JDK has gone
   missing since the October record.

## Tracker corrections

Three of the eight non-completed tasks were recorded wrongly; see
`docs/REMAINING_TASKS_AUDIT.md`.

- `R-release-macos-dmg-artifact` was **not blocked** — decided 2026-10-27. The
  real problem was two release scripts shipping different applications, now
  reconciled.
- `E11-continuity` recorded a NOT-DONE item that landed the next day (`b559bf3`).
- `V15-desktop-automation` named the wrong grant target; a worker following it
  would have asked for a grant measured to be useless.
