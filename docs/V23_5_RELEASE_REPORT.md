# V23.5 final integration and release report

**Date:** 2026-11-18 · **HEAD:** `acfd958` plus the report commit · **Candidate:** `/tmp/vantage-cand/Vantage.app` and `dist/Vantage-0.2.0.dmg`

## Workers

| worker | assignment | outcome |
|---|---|---|
| A3 — approval acceptance | end-to-end Command Center journey | **delivered** — `tests/test_command_center_acceptance.py`, 7 tests |
| A5 — acting-tool security | audit every operation's guard | **delivered** — `tests/test_desktop_guard_audit.py`, 52 tests, one real finding |

Two subagents, independent parallel execution, peak concurrency 2. Agent Teams
were not used. Everything else was done directly and is not presented as agent
work.

## Release candidate

| property | value |
|---|---|
| path | `dist/Vantage-0.2.0.dmg` (45 MB) · `/tmp/vantage-cand/Vantage.app` |
| bundle id | `com.jarvisforge.vantage` |
| signing | `adhoc`, deep verify passes |
| architecture | Mach-O arm64 |
| Python | `Contents/Resources/python/bin/python3.11` |
| backend | `Contents/Resources/run.py` + `jarvis/` — runs from inside the bundle |
| executor | `Contents/Resources/native/desktop_executor/VantageDesktopDev.app/…` |
| executor SHA-256 | `f786a64b…` — **identical in app bundle and DMG** |

## Acceptance, and the level reached for each

The levels are separate on purpose; collapsing them is how a build passes for a
product.

| property | SOURCE | BUILT | BUNDLED | RUNNING | OBSERVED |
|---|---|---|---|---|---|
| ambiguous target refused | yes | yes | yes | **yes** | yes (binary) |
| unambiguous target resolves | yes | yes | yes | yes | yes (binary) |
| stale window refused | yes | yes | yes | yes | **yes** (see below) |
| press_element guard | yes | yes | yes | yes | forwarding only |
| approval journey | yes | — | yes | — | **yes** (routes) |
| Finder-launched input | yes | yes | yes | **no** | **no** |

**Stale-window refusal, measured against the candidate's own executor:**

```
click x=400 y=300 --expect-window-id=999999  -> target_not_found  "nothing was posted"
click x=400 y=300 --expect-pid=999999        -> target_stale
```

**RUNNING was demonstrated** for the executor: a backend started from the
candidate bundle on an isolated port (8801) reported `source_root:
/tmp/vantage-cand/Vantage.app/Contents/Resources` and resolved the executor from
inside that same bundle. Not the checkout's.

## The finding that changes the permission story

The candidate backend reported `frontmost_application: "loginwindow"` while the
real session had Claude frontmost — and enumerated 24 windows successfully, while
another application's AX tree came back empty in the same process.

There are **two** conditions for desktop control, and only one had been
documented:

1. Accessibility for the **responsible process** — `Vantage.app` must hold it.
2. Membership of the user's **GUI session** — a backend started from a shell is
   not in it.

Condition 2 explains why `read_element` found nothing while `windows` worked
perfectly. It also corrects an earlier conclusion: "the fixture stopped
publishing AX" was wrong. The fixture was healthy; the reader was outside the
session.

A backend spawned by the app inherits the app's session, so the product satisfies
condition 2 and this test harness does not. That is why Cases A–D could not be
run against a live fixture here, and why `docs/DESKTOP_PERMISSION_MODEL.md` now
records both conditions.

## Security audit result

The governed path is sound. Every desktop tool's explicit handler parameters are
declared in its `ToolSpec`; every guard reaches the executor; the real
`PermissionEngine` does not BLOCK a guarded call; an undeclared parameter still
BLOCKs (a control, so the audit cannot pass by validation being loosened).

Three weaknesses recorded, none reachable through Vantage today:

- **`scroll_element`** — a targeted AX write with no guard that silently ignores a
  guard flag. Unreachable from Python; a test now fails the moment it is exposed
  without a guard.
- **`type`/`key`** ignore `--expect-window-id`. The Python wrapper never sends one
  for those ops, but a direct caller could believe a guard was in force.
- **`verify_change`** can emit `verified` about the wrong element after a window
  move. Claim-integrity, not input safety.

## Tracker

**96 completed, 4 blocked, 1 pending** (was 93 / 5 / 3).

Closed this campaign: `R-release-macos-dmg-artifact` (the DMG now exists and was
inspected), `E7-calls-contacts` (verified; call placement is a scope boundary,
not a gap), `E11-continuity` and `V15-desktop-automation` corrected earlier.

Remaining, all genuinely external:

| task | why |
|---|---|
| `R6.4c-email-organize-operations` | no organize operation in either provider; an honest decline is live |
| `R6.4-capability-registry-gate-or-delete` | owner decision on gate-vs-delete |
| `R7-android-companion` | no device; **and the JDK has gone missing** since the October record |
| `V15-desktop-automation` | Accessibility grant for `Vantage.app` + GUI session membership |
| `E12-release` | installed-app replacement is a user action |

## Release decision: **READY WITH LIMITATIONS**

The candidate is a self-contained SwiftUI app whose bundled executor refuses
ambiguous and stale targets and whose approval path refuses everything it should.
Those are measured on the artifact, not inferred from a build succeeding. **415
tests pass**, 2 skipped for an absent fixture.

Four limitations, each a known external condition rather than unfinished work:

1. The installed `/Applications/Vantage.app` is a three-day-old shell with no
   Python, backend or executor. Installing the DMG is the user's step.
2. Finder-launched desktop input needs an Accessibility grant **and** a GUI
   session; neither can be manufactured by a build.
3. Cases A–D against a live fixture could not run because this harness sits
   outside the GUI session. The underlying logic is covered by unit tests and the
   binary's own behaviour, not by a live ambiguous-target press.
4. Notarization is not done; no Developer ID credentials are available.
