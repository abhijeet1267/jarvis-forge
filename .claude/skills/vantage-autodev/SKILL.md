---
name: vantage-autodev
description: "Run one bounded, verified engineering iteration on the Vantage project (jarvis-forge). Reads the durable status, picks the highest-priority unfinished task, implements and tests it, and records evidence. Use when asked to continue/advance the Vantage build, run an autodev iteration, or pick up where the last session stopped."
---

# Vantage autodev — one iteration

Execute **one** bounded engineering iteration on the Vantage project and stop.
The `/loop` scheduler can invoke this again; each invocation must leave a
checkpoint that says exactly where things stand.

## Where the truth lives

Read these in this order. They are the source of truth — not this file, and not
your own recollection of a previous session.

| What                 | Where                                                 |
| -------------------- | ----------------------------------------------------- |
| Resume protocol      | `CLAUDE.md` (repo root)                               |
| Status + next task   | `docs/PROJECT_STATUS.md`                              |
| Completion evidence  | `docs/PROJECT_COMPLETION_MATRIX.md`                   |
| Durable task queue   | `.autodev/tasks.json`                                 |
| Engineering history  | `docs/AUTONOMOUS_ENGINEERING_LOG.md` (last iteration) |
| Latest checkpoint    | newest file in `.autodev/checkpoints/`                |
| Build & run commands | `docs/RELEASE_AND_BUILD.md`, `docs/TESTING.md`        |

If those disagree with each other or with the code, **the code wins** — fix
the document, and say so in the report.

## The loop

1. **Read** the status, the queue, the last checkpoint, and `git status --short`.
2. **Select** the highest-priority task that is genuinely unfinished. A task
   marked `completed` in the queue is _not_ proof — confirm its evidence still
   holds against the current tree. A task marked `pending` may already be done
   in the source; that is worth discovering.
3. **State** the concrete change in one sentence before making it.
4. **Reproduce** the defect or write down a testable acceptance criterion.
5. **Implement** the smallest change that satisfies it.
6. **Test** — focused first, then adjacent, then the full suite at a milestone.
7. **Inspect the diff** for unintended changes and regressions.
8. **Record** evidence: exact command, exit code, counts, duration.
9. **Update** `.autodev/tasks.json` (status + evidence) and the status doc.
10. **Checkpoint** into `.autodev/checkpoints/checkpoint-<date>-<slug>.md`.
11. **Stop.** One iteration.

## Rules that are not negotiable

- **Never** commit, push, reset, stash, clean, or discard. The working tree
  carries the owner's large uncommitted WIP — `macos/`, `android/`,
  `jarvis/companion/` are untracked and must not be lost. A `git clean` would
  destroy the native app.
- **Never** weaken a test, delete an assertion, or widen a timeout to hide a
  failure. If a test is wrong, establish the intended contract first and
  change the _test_ deliberately, with a comment saying why.
- **Never** claim a test passed that did not run. If the toolchain is missing,
  say exactly what is missing (`swift test` needs Xcode — XCTest is absent
  under Command Line Tools alone) and mark it unverified.
- **Never** print, log or copy a credential. Use the environment or the
  existing keyring path.
- **Never** terminate a process you have not established is Vantage's. Check
  `ps`/`lsof` first and say why you believe it is ours.
- **Do not run CPU-heavy suites concurrently.** A Gradle build overlapping a
  pytest run caused five spurious OCR timeouts that looked like product
  defects. Run them one at a time.

## Choosing well

Good next task: a defect you can reproduce, a boundary you can draw with a
test, a path that is stubbed and can be made real, a doc that is provably
wrong.

Bad next task: re-running a passing suite, re-reading unchanged files,
writing a plan instead of code, or auditing a path the last checkpoint already
verified.

If the highest-priority task is owner-gated (a product decision, a commit
authorisation, a credential), **record it and move to the next actionable
task** — do not let one blocked decision stop the iteration.

## Verifying a runtime claim

Source reading is not evidence about behaviour. When the claim is about the
running product, measure it:

- backend: the access log and `curl` against the live port;
- macOS app: the process, its windows (`CGWindowListCopyWindowInfo` by owner
  PID), its sockets, and the backend's request log;
- port ownership: `lsof -nP -iTCP:<port> -sTCP:LISTEN`.

**Launch the macOS app the way the user does:** `open -a <bundle>`. Exec'ing
the binary directly is a _different launch path_ — it runs the event loop and
polls the backend, but the SwiftUI scene never activates, so the app has no
window and looks broken when it is not. (Measured 2026-10-21: the same binary
had no window when exec'd and a correct 900x572 window when opened.)

If visual verification is unavailable (no screenshot tool, no assistive
access), say which checks were established by other means and which UI details
remain unverified.

## Report format

Keep it to these five lines of substance:

- **Implemented** — the behaviour that changed, and the files.
- **Proof** — the command and its actual result (counts, exit code).
- **Runtime** — what was observed, and how it was observed.
- **Remaining** — unresolved criteria and precise blockers.
- **Next** — one distinct actionable task.
