# AUTODEV — Autonomous Development Runner Runbook

A bounded, resumable coding-agent runner for this repository. The worker is a
real headless `claude -p` session per task with a bounded tool allowlist; the
supervisor (`scripts/autodev/autodev`, Python stdlib only) owns the budget,
the queue, the checkpoints, and restarts.

## What is automatic, and what is not

| Capability                                                                        | Status                                                                                                |
| --------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| Bounded run with persisted deadline (12h default, 24h hard cap)                   | **automatic** — deadline survives restarts; a restart never extends it                                |
| Task selection from `.autodev/tasks.json` (priority, deps, attempt caps)          | **automatic**                                                                                         |
| Real coding work by a headless `claude` session per task                          | **automatic**                                                                                         |
| Worker report → queue/checkpoint merge                                            | **automatic**                                                                                         |
| Bounded retries with exponential backoff (300s base, 30m cap)                     | **automatic**                                                                                         |
| Final 10% of budget reserved for wrap-up (full suite + status update)             | **automatic**                                                                                         |
| Crash recovery: a task left `running` is reverted to `ready` on next start/resume | **automatic**                                                                                         |
| Checkpoints (state + queue snapshot, last 10 kept)                                | **automatic**                                                                                         |
| Login-time resumption                                                             | **manual install** — see below; it only resumes an _unexpired_ run, never invents one                 |
| Cost/usage budget                                                                 | **not verifiable** from this environment — no usage/cost API is wired; do not claim a cost cap exists |
| Pushing to the remote / publishing releases                                       | **never** — outside the worker's allowed tools                                                        |

## Commands (from the repo root)

```bash
scripts/autodev/autodev setup                 # validate prerequisites
scripts/autodev/autodev start --hours 12      # begin a bounded run (foreground)
scripts/autodev/autodev status                # deadline, phase, current task, counts
scripts/autodev/autodev logs [--task ID]      # tail the newest worker log (secrets redacted)
scripts/autodev/autodev stop                  # controlled stop (worker + supervisor)
scripts/autodev/autodev resume                # resume an active, unexpired run
```

Run the supervisor in the background from a terminal:

```bash
nohup scripts/autodev/autodev start --hours 12 > .autodev/supervisor.log 2>&1 &
```

(Or use a terminal tab you can watch; `status` works regardless of how the
supervisor was launched, because state lives on disk.)

## Budget and deadlines

- `start --hours N` (default 12, capped at 24). The **wall-clock deadline** is
  persisted in `.autodev/state.json`; `resume` reads the same deadline —
  restarting Claude, the worker, or the machine never resets it.
- When 10% of the budget remains, the supervisor stops scheduling tasks and
  runs one wrap-up worker (full test suite, PROJECT_STATUS update, notes in
  `.autodev/logs/wrapup-notes.md`).
- On expiry the run is marked `expired` and will not be extended; start a new
  run explicitly.

## Failure handling

- Worker exits without a valid `.autodev/worker-report.json` → task marked
  `failed`, backoff, next eligible task (or retry after backoff).
- A task at its `max_attempts` is left in its last status and never retried
  again in this queue state; raise `max_attempts` or fix the blocker to
  re-enable it.
- The supervisor never enters a rapid restart loop: each attempt is a full
  worker run, and backoff grows.

## State layout (`.autodev/`, gitignored except `tasks.json`)

- `tasks.json` — the durable queue (committed; the source of truth for _what_).
- `state.json` — run budget/deadline/phase (runtime; the source of truth for _when_).
- `checkpoints/checkpoint-*.json` — periodic state+queue snapshots (last 10).
- `logs/*.log` — per-worker stream-json logs.
- `presnapshot-*.txt` — git-state snapshots taken before major runs.

Malformed `state.json` or `tasks.json` → the supervisor refuses to start and
says why; it never overwrites a last-valid file it cannot parse.

## Login-time resumption (optional, current-user only)

Installs a LaunchAgent that runs `autodev resume` at login. `resume` does
nothing unless a run is active and unexpired — a reboot never starts a new
12-hour run.

```bash
scripts/autodev/install-login-agent     # install ~/Library/LaunchAgents/com.vantage.autodev.plist
scripts/autodev/autodev status          # confirm no run is being resumed unexpectedly
scripts/autodev/uninstall-login-agent   # remove the plist + bootout
```

No administrator privileges required. To stop everything at any time:
`scripts/autodev/autodev stop` (works with or without the LaunchAgent).

## What the worker may and may not do

Allowed (enforced by the `--allowedTools` allowlist in the supervisor): Read,
Edit, Write, Glob, Grep, Task (subagents), and Bash limited to
`git status/diff/add/log/show/commit`, `.venv/bin/python …`, `pytest …`.

Not allowed: anything else — no `git push`, no network installs, no shell
outside the allowlist, no files outside this repo, no permission-prompt
bypassing (headless print mode auto-denies unallowed tools).

## Verifying the runner itself

```bash
# 1. setup passes
scripts/autodev/autodev setup
# 2. a tiny real run (30 min) against the smallest ready task
scripts/autodev/autodev start --hours 0.5
# 3. mid-run, status shows deadline + current task; stop is clean
scripts/autodev/autodev status
scripts/autodev/autodev stop
# 4. resume of an expired/stopped run refuses
scripts/autodev/autodev resume   # expect: not resumable
```
