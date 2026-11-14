# Phase 68 — a job cannot outlive the process that was running it

Date: 2026-10-06. Repo `jarvis-forge/`, HEAD at start `2d2f990`.
Found by: the "debug all issues" sweep, while checking surfaces after the Phase 67 redeploy.

## 1. The symptom, measured rather than inferred

A GET on the delivered host's `/tasks` returned twelve jobs, all named `lifecycle test`, all
`running`:

```
JOB-5362d14b3b2b | 'lifecycle test' | running | created 2026-09-25T14:19:50 | steps 2
JOB-d133f96f1e2e | 'lifecycle test' | running | created 2026-09-25T14:18:20 | steps 2
… 12 rows, 14:01:24 → 14:19:50 on 2026-09-25, each with both steps still `pending`
```

To see whether that was a cosmetic wart or a lying system, the two tools that consume it were called
against a **copy** of the real database — never the user's, per the standing rule about probing
writes on the live app:

```
GET_CURRENT_TASK says:  Current task: "lifecycle test" -- running, 0 of 2 step(s) done.
STOP_CURRENT_TASK says: Stopped "lifecycle test" -- the job is cancelled. … (0 done)
                        verified: True   cancelled: True
```

So the assistant told a person it was doing work that had been dead for ten days, and the emergency
stop **spent itself cancelling a phantom and reported `verified: True`**. That second one is the
serious one: `_active_job()` picks the _newest_ active row, so a real job that is not the newest
would keep running while the user was told it stopped. It is Phase 54's rule — a stop has to stop the
thing it named — violated from the other side, and it is reachable without any attacker.

## 2. Root cause

`TaskManager.refresh_status()` exists, and its own docstring names this exact hazard:

> "A job's status must never be written from the plan that created it, because a plan can stop
> half-way … **anything not recomputed there stayed `running` forever**."

Reading every call site shows the cure was applied to writes only:

| Path                                                                                                             | Recomputes?                    |
| ---------------------------------------------------------------------------------------------------------------- | ------------------------------ |
| `orchestrator.py:373, 618, 646, 676, 734, 818`, `tasks.py:243, 284` (step outcomes, approvals, cancel, retry)    | **yes**                        |
| `GET /tasks` → `tasks.list()` (`app.py:867`)                                                                     | no — returns the stored column |
| `GET /tasks/{id}` → `tasks.get()` (`app.py:872`)                                                                 | no                             |
| `core/briefing.py:86` — `running = [j for j in jobs if j["status"] == "running"]`                                | no                             |
| `tools/self_state.py:32 ACTIVE_STATES` → `_active_job()`, used by `get_current_task` **and** `stop_current_task` | no                             |
| process start                                                                                                    | **nothing at all**             |

A process that died between `create()` and its first step write leaves `status='running'` with every
step `pending`, and no code path ever contradicts it again. The origin of these twelve rows is
`tests/test_api.py:385` — `get_core().tasks.create("lifecycle test", ["step_a", "step_b"])` — run
against the default home in the week before `tests/conftest.py` gave each session its own
`JARVIS_HOME`. Conftest isolation means no _new_ ones; it says nothing about the ones already there,
and it never addressed the missing reconciliation for a genuine crash.

**This is the third time this project has been bitten by a stored `running` that nothing
reconciles** — Phase 26 found `retry()` setting a job to `running` and executing nothing ("0
executions and a job permanently `running`, which the briefing then published as '1 job(s) in
progress'"), Phase 14 §3 fixed orphaned step state, and now the job row itself. The pattern is worth
naming: status is _derived_ data stored in a column, and every fix so far added another call to the
derivation on the write path, while the read paths and the process boundary were left to trust it.

## 3. The fix

One method, one call site.

- `TaskManager.reconcile_after_restart()` — for every row stored as `running`, recompute it with the
  existing `job_status_for(step_statuses(...))` and write it if the derivation disagrees. Returns the
  rows it moved, and logs the count and the resulting statuses.
- Called from the lifespan **before `core.scheduler.start(core)`** — the one moment at which the
  statement "nothing is in flight in this process" is guaranteed true.

Why only there, and why that matters: the orchestrator writes a step's status at its **outcome**, not
when it begins, so a live job's steps are `pending` while it runs. A sweep that ran on a read, or
mid-flight, would look at real work and call it `incomplete`. Startup is the only safe moment, and
`test_the_reconcile_is_not_wired_into_a_read_path` pins that a GET does not mutate job state — the
same principle Phase 28 was built on.

Nothing was deleted. The twelve rows still exist, now labelled what their own steps prove:
`incomplete`.

## 4. Tests — 9, red first as 8 failures and 1 pass

| Test                                                                  | Pins                                                                                                              |
| --------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| `..._running_row_outliving_its_process_is_reconciled`                 | Asserts the pre-state (`running`, steps `pending`) in the same test, so the sweep cannot pass vacuously           |
| `..._job_this_process_started_is_left_alone`                          | A job created after the sweep is still `running`, and reading the list does not change it                         |
| `..._paused_and_cancelled_jobs_are_not_touched`                       | `paused` is a state a person set and expects to survive a restart, not a crash mark                               |
| `..._sweep_reports_the_rows_it_moved_and_logs_nothing_it_did_not_do`  | Returns the ids it moved; a second pass returns `[]`                                                              |
| `..._current_task_answer_does_not_narrate_a_phantom`                  | "Nothing is running right now", `running: False`                                                                  |
| `..._stop_refuses_instead_of_cancelling_a_job_that_was_never_running` | `cancelled: False`, "nothing was stopped", and the row is not written by the refusal                              |
| `without_the_sweep_the_stop_would_still_spend_itself_on_the_phantom`  | **Negative control** — asserts the bug is still reachable without the fix, so the two tests above are not vacuous |
| `a_starting_server_reconciles_the_rows_it_inherits`                   | Driven through the real lifespan, not grepped (Phase 58's lesson: a textual check passes on dead code)            |
| `the_reconcile_is_not_wired_into_a_read_path`                         | `list()` and `get()` do not reconcile — reads must not write                                                      |

One of my own tests was order-dependent and is on the record: the startup test first swapped
`db_module._DB` and **passed alone, failed in a full run**, because `get_core()` is a module global
already built against the session database — the lifespan was reconciling a database the test was not
looking at. Rewritten to seed through the core the server actually uses, and to delete its own row
afterwards. Same class as Phase 33's stub handlers and Phase 63's shared OCR backend.

**Suite: 2442 passed, 0 failed in 90.77 s** (2433 + 9), plus a confirming run of the neighbouring
files (`test_api.py`, `test_phase14_workflows.py`, this file) at 102 passed.

## 5. Live proof, on the delivered app

Rebuilt (`build_app.py`, ad-hoc signed, `codesign --verify --deep --strict` valid), redeployed, and
restarted — the launcher stopped as well as the server, which Phase 67's delivery note learned the
hard way.

| Check                                                  | Before                                                              | After                                                                  |
| ------------------------------------------------------ | ------------------------------------------------------------------- | ---------------------------------------------------------------------- |
| `SELECT COUNT(*) FROM tasks WHERE status='running'`    | **12**                                                              | **0**                                                                  |
| status distribution of those rows                      | 12 × `running`                                                      | **12 × `incomplete`**                                                  |
| `jarvis.log`                                           | —                                                                   | `stale running jobs reconciled at startup {"jobs": 12, …}`             |
| `get_current_task` (against a copy of the repaired DB) | `Current task: "lifecycle test" — running, 0 of 2 step(s) done.`    | **`Nothing is running right now.`** `running: False`                   |
| `stop_current_task` (same copy)                        | `Stopped "lifecycle test" — the job is cancelled.` `verified: True` | **`Nothing was stopped: no task is running.`** `cancelled: False`      |
| panel, live DOM                                        | (task card narrating a phantom)                                     | no task rows claiming work; `Audit chain intact · 520 of 520 verified` |
| `GET /health`                                          | 200                                                                 | 200                                                                    |

## 6. Numbers

| Measure                                      | Before             | After                                      |
| -------------------------------------------- | ------------------ | ------------------------------------------ |
| Tests                                        | 2433               | **2442** (9 new)                           |
| Test modules                                 | 94                 | **95**                                     |
| Phase reports                                | 69                 | **70**                                     |
| Versioned lines                              | 76,467             | **76,725** (shipped 44,489 · tests 31,618) |
| Route objects / paths / decorators           | 118 / 111 / 118    | unchanged — no route added                 |
| Tools / gated / planner rules / tables       | 148 / 32 / 52 / 32 | unchanged                                  |
| Phantom `running` jobs on the delivered host | 12                 | **0**                                      |

## 7. Limits of this fix, stated

- **68-A:** a row stored `running` with **no step rows at all** is left alone — the derivation has
  nothing to work from, and inventing a status is what this project keeps refusing to do. Measured on
  the delivered database: 0 such rows exist today, so nothing is currently hidden by this limit.
- A crash mid-run leaves the row `running` **until the next start**. That is deliberate: during a run
  there is no way to distinguish "the process died" from "this step is slow" without a heartbeat or
  an ownership epoch, and adding either is a schema change with its own failure modes. If phantoms
  turn out to matter between restarts, the honest next step is an owner epoch column, not a shorter
  timeout.
- The twelve rows are still in the ledger of jobs; they are now labelled honestly instead of being
  removed. No history was deleted.
- The fixtures that produced them (`tests/test_api.py:385`) are correct as they stand — they run in an
  isolated home now. What was missing was never test hygiene alone; it was the reconciliation.
