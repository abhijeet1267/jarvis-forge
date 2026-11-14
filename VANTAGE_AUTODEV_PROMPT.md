# VANTAGE — Autonomous Development Prompt

> The enduring implementation requirements for this project. This file says
> what "done" means and what must never be traded away. It does not change
> with each session; `VANTAGE_AUTODEV_STATE.md` does.

## What this project is

Vantage is a permissioned, multi-agent personal AI operating system for the
owner's own Mac.

- `jarvis/` — Python/FastAPI backend. `api/` (HTTP routes), `core/`
  (orchestrator, planner, conversation), `tools/` (the tool registry),
  `permissions/` (the gate), `gateway/` (AI **provider** adapters),
  `companion/` (the phone gateway), `memory/`, `voice/`, `scheduler/`.
- `macos/Vantage/` — the native Swift/SwiftUI app (SwiftPM, zero deps).
- `android/` — the native Kotlin/Compose companion (untracked by git).
- `ios/` — **paused**. Preserve the sources; do not make iOS work part of
  the project.
- `dashboard/` — the web dashboard the backend serves on `:8765`.
- `tests/` — the pytest suite.

## The mission

**Ship visible product functionality, not audits.** A cycle is:

> choose one incomplete objective → implement it → run the affected
> application → observe actual behaviour → fix defects → add focused
> regression protection → update the checkpoint → choose the next objective

An iteration that produces only tests and documentation while a product
requirement is still incomplete is an incomplete iteration.

## What must never be traded away

1. **The permission gate.** A model may _propose_ an action; only the
   permission engine may _authorise_ it. Never let a model's output, a
   client's claim, or a test convenience widen the gate.
2. **Self-approval protection.** A narrow surface (the phone) must not
   approve its own request. The guard needs the _session_ as well as the
   _surface_; a path that fails to send the session disarms it.
3. **Honest reporting.** A tool that did not run, an effect that could not be
   verified, and a provider that was not reached must each be reported as
   what they are. Never a fabricated success, never an invented answer, never
   a simulated model response presented as a live one.
4. **Secrets.** The master token lives only on the Mac. Never print a key,
   token, or password — not in source, tests, logs, screenshots, or reports.
   Compare credentials by hash when checking which one is in use.
5. **The owner's work.** The tree carries a large body of uncommitted and
   untracked work that is the owner's. Never `git clean`, `git reset --hard`,
   never delete untracked files, never mass-format a file to land a small
   change, never commit the tree without the owner's decision.
6. **TLS verification.** Never disable certificate verification.

## The P0 standard — a conversational assistant

A general question must be answered by the **configured provider**, through
the project's real provider integration, with the conversation's history
threaded. Specifically:

- General questions reach the model, not a canned greeting, a fixed command
  list, or a "not understood" reply.
- A follow-up resolves against what was already said. The conversation has a
  session that scopes it; a new conversation does not inherit the old one's
  history.
- The app **knows whether the AI path is answering** and says so when it is
  not. A lookup fallback presented like a model answer is a defect, not a
  feature — the backend's Wikipedia fallback can return an article about a
  _different subject_ from the one that was asked about.
- Tool execution and conversation stay separate. A model that _mentions_ an
  action has not performed it.
- Loading, provider failure, timeout, unauthorised, and unreachable-backend
  states are each shown honestly, with a retry where retrying is safe.
- The result is visible in the **running application**, with real screenshots
  where the environment supports them.

## The P1 standard — provider configuration and the dashboard

Provider and model names must match the server's actual payloads. A missing
or invalid key must not produce a false success indicator. Keys are stored
and deleted through the established routes. The dashboard's existing actions
keep working.

## The P1 standard — the macOS app

The freshly built `.app` bundle must be launched with `open -a` (exec'ing the
binary is a _different_ launch path and the SwiftUI scene never activates).
It must connect using the established authentication, show real data, update
when the backend stops, and recover when it restarts. No duplicate polling
loops; no stale callbacks; no leaked resources.

## How to verify, in this environment

- `swift test` **cannot run** — only the Command Line Tools are installed
  (`no such module 'XCTest'`). Pin the contract in the **Python** suite
  (`tests/test_macos_*.py`): the server's side, and the Swift source.
- **Screen recording is refused** (`screencapture`, `CGWindowListCreateImage`),
  and the Accessibility API returns 0 windows. `ImageRenderer` does **not**
  work: it does not draw a `ScrollView`'s content.
- **What works for a picture:** host the real view in a real borderless
  offscreen `NSWindow`, read `bitmapImageRepForCachingDisplay`, and force
  `NSAppearance(named: .darkAqua)` on the app _and_ the window — a borderless
  window resolves to the light appearance, where a 5%-white bubble on a white
  backdrop is white, and the answer is invisible.
- **To exercise the app's own code paths:** a temporary file in the app's own
  module, triggered by a marker file checked in
  `applicationDidFinishLaunching` (`launchctl setenv` and `open --env` do
  **not** reach a GUI app). Remove it and rebuild the bundle afterwards, and
  check the shipped binary with `strings | grep` for the harness's marker.
- **The backend refuses to start** with LAN mode on and a loopback bind.
  Start it with `JARVIS_HOST=0.0.0.0` to match `~/.jarvis-forge/lan_mode`.
- **The browser tests need that backend**; without it they fail with
  `ERR_CONNECTION_REFUSED`, which is an environment gap, not a defect.
- **Those browser tests write to the real calendar.** Use an isolated home or
  clean up with `get_calendar_provider().cancel(uid)`.

## Before claiming anything is done

Show the **actual** change in source and in runtime behaviour. A green suite
is evidence that an implemented feature works — it is never the goal, and
never the evidence that a feature works at all.

Use these labels, honestly: **Implemented**, **Tested**, **Runtime-verified**,
**Visually verified**, **Blocked**, **Not done**. Do not claim a UI was
visually verified unless it was seen.
