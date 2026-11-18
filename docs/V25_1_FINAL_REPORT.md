# V25.1 final product acceptance — report

**HEAD:** `ae36ba4` · **Artifact:** `dist/Vantage-0.2.0.dmg` · **Date:** 2026-11-18

## Workers

| worker | assignment | outcome |
|---|---|---|
| A2 — GUI session identity | is the app's backend blind to the frontmost app? | **delivered, and refuted my finding** |
| A6 — E12 release closure | audit the release artifact against acceptance criteria | **delivered** — found two shipping defects |

Two subagents, independent parallel execution, peak concurrency 2. Agent Teams
not used; the rest was done directly.

## Release artifact

| property | value |
|---|---|
| path | `dist/Vantage-0.2.0.dmg` |
| bundle id / version | `com.jarvisforge.vantage` / `0.2.0` |
| Python | `Contents/Resources/python/bin/python3.11`, recorded **relatively** |
| executor | SHA-256 `709aa01b…`, carries the ambiguity, stale-target and `scroll_element` guards |
| signing | ad-hoc; `codesign --verify --strict --deep` passes; Gatekeeper **rejects** |
| integrity | `hdiutil verify` VALID |

## What changed this campaign, and why

**`scroll_element` was guarded.** It previously scrolled a Finder scroll bar from
0 to 0.35 while being handed `--expect-window-id=999999` — a guard parsed and
discarded, which is worse than none. Before/after measured on the binary; the
DMG's own executor now returns `target_not_found`.

**The bundle was not movable.** `python_path.txt` recorded an absolute build-machine
path. Copied elsewhere, `backendInterpreter` fell through to `/usr/bin/python3`
(3.9.6) and the backend died on `import fastapi` with `SyntaxError: match stmt`.
Measured, then fixed: the file is now relative and the reader resolves it against
the bundle. Verified by copying the app outside the checkout and importing
fastapi 0.162.7 from the copy's own site-packages.

## What I got wrong

I recorded that the app's backend could not read `NSWorkspace.frontmostApplication`,
and built a two-condition theory around it. **That was false.** Re-measured three
times against the installed app's own backend: `frontmost: 'Brave Browser'`,
`screen_capture: True`, 25 windows. The `nil` was a transient — the display was
asleep 08:51–09:42 across that measurement.

A2 settled the general question with a negative control (`launchctl bsexec` into
a non-GUI bootstrap): AppKit initialisation changes nothing; the read tracks GUI
session membership and nothing else. No product change was warranted, and none
was made.

`docs/GUI_SESSION_INVESTIGATION.md` keeps the superseded analysis rather than
deleting it, because the error is the instructive part.

## Acceptance levels

| property | level |
|---|---|
| streaming ambiguity refusal | SOURCE→BUNDLED→**OBSERVED** (DMG's own executor) |
| stale window/pid refusal | OBSERVED |
| `scroll_element` guard | SOURCE→BUNDLED→OBSERVED |
| bundle relocatability | SOURCE→BUNDLED→**OBSERVED** (copied outside the checkout) |
| approval/denial journeys | OBSERVED (real routes, filesystem facts) |
| native SwiftUI UI | **NOT OBSERVED** — no GUI driver available |
| Finder-launched physical input | **PERMISSION_BLOCKED** |
| notarization | **EXTERNAL_BLOCKED** — no Developer ID credentials |

Tests: **194 passed, 2 skipped** on the touched surface; **415 passed, 2 skipped**
on the wider set earlier this session.

## The installed app is not this product

Observed live: `/Applications/Vantage.app` (pid 77185) spawned a backend running
`/Users/abhijeetmiskin/AppData/Vantage/run.py` — **the checkout**, on the Homebrew
Python, not the bundle's. It depends entirely on the development tree, and shares
only a bundle id and version with the DMG. `docs/E12_RELEASE_AUDIT.md` confirms
they are different binaries (3 files vs 2,749 in `Contents/Resources`).

Installing the DMG would replace it. That interrupts whatever is open, so it is
the user's step, not an agent's.

## Tracker

96 completed, 4 blocked, 1 pending. All five open items are external:

| task | blocker | next action |
|---|---|---|
| `E12-release` | install/uninstall docs do not exist; Gatekeeper rejects ad-hoc; no manifest ties the DMG to a commit | agent-doable, listed below |
| `V15-desktop-automation` | Accessibility grant for `Vantage.app`, which only the user can give | user |
| `R6.4c`, `R6.4` | owner decisions on email operations and registry gating | owner |
| `R7-android-companion` | no device **and** no JDK since October | external |

E12 stays **blocked**, not completed. Its remaining agent-doable work — install
and uninstall docs, a build manifest, and pointing the README at the real
installer — should not sit behind a user action, and is the next task.

## Decision: READY WITH LIMITATIONS

The artifact is a self-contained, movable SwiftUI app whose bundled executor
refuses ambiguous, stale and unguarded targets, measured against the DMG itself.

**Not** a full autonomous desktop release: physical input from the
Finder-launched app has never been observed, the native UI has never been driven,
and the build is not notarized. Those are stated as limits, not omissions.
