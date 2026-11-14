# Phase 53 — Vantage talking about itself

|                       |                                                                                        |
| --------------------- | -------------------------------------------------------------------------------------- |
| Phase                 | 53                                                                                     |
| Date                  | 2026-10-04                                                                             |
| Starting commit       | `3708f85` (Phase 52 close-out + coverage correction)                                   |
| Ending commit         | `35c9abf` — filled in at the Phase 56 close-out; §10 promised it and did not record it |
| Tests before          | 2065 passed · 0 failed · 0 skipped                                                     |
| Tests after           | **2129 passed · 0 failed · 0 skipped** (§10)                                           |
| Tools before          | 136                                                                                    |
| Tools after           | **141** (five added: 4 READ, 1 LOW)                                                    |
| Planner routes before | 50                                                                                     |
| Planner routes after  | **51** (one rule, five targets)                                                        |
| §6 coverage           | 32 / 37 → **36 / 37**                                                                  |
| Push status           | **nothing pushed; local commits only**                                                 |

Opened on the master execution prompt §7: Vantage must answer questions about its own
operating state using state it already owns. Four new stores were not built; none were needed.

---

## 1. Measured first

The four authoritative sources, found before writing anything:

| question          | source                                    | shape                                                                                             |
| ----------------- | ----------------------------------------- | ------------------------------------------------------------------------------------------------- |
| pending approvals | `core.policy.pending()`                   | calls `expire_stale()` first, then selects `state = 'pending'` — the TTL is one rule in one place |
| health / mode     | the objects `/health` builds from         | `core.tools`, `core.settings.llm_enabled`, `core.scheduler.describe()`, `core.devices`            |
| recent actions    | `core.audit.recent(limit)`                | rows with `result` already recorded                                                               |
| current task      | `core.tasks.list()` + `task_steps` rollup | statuses: running / pending / queued / paused / awaiting_confirmation                             |
| stopping it       | `core.tasks.cancel(job_id)`               | returns a `LifecycleReport` with `__bool__` defined                                               |

Two things the measurement settled:

- **`LifecycleReport` is falsy when rejected.** `emergency_stop`'s `if tasks.cancel(...)` is
  therefore correct, not a latent bug. Checked by instantiating a rejected report and testing
  `bool()` rather than assuming either way.
- **`cancel_schedule` is a different capability.** It stops a thing that would run later; a
  task is one execution happening now. A test pins that stopping a task leaves
  `scheduled_jobs` untouched.

## 2. What was actually broken

Four of the six §6 self-state utterances reached nothing. **Three reached something worse:**

```
are you running            -> check_agent_status(app="You")
what is the current task   -> ask_question(topic="current task")
what are you doing         -> ask_question(topic="you doing right now")
what is your status        -> ask_question(topic="your status")
```

A question about Vantage answered by looking for an installed application literally named
"You", or handed to the general-knowledge tool, is a confident answer to the wrong question.
That is the class this project keeps having to kill, and it is worse than a refusal, because a
refusal tells the user something is missing.

## 3. What was built

`jarvis/tools/self_state.py` (new, registered in `jarvis/tools/__init__.py`):

| tool                        | risk | gated  | what it refuses to do                                                 |
| --------------------------- | ---- | ------ | --------------------------------------------------------------------- |
| `get_pending_confirmations` | READ | no     | show a confirmation past its TTL, or synthesize one from audit rows   |
| `get_vantage_status`        | READ | no     | name a provider that is not configured; report a mode it did not read |
| `get_recent_activity`       | READ | no     | smooth `denied` / `blocked` / `unverified` into a success count       |
| `get_current_task`          | READ | no     | claim work is running because a request arrived                       |
| `stop_current_task`         | LOW  | **no** | say it stopped something without reading the job's status back        |

One planner rule, `_vantage_self`, registered **third in the chain** — before the application
probe, because that probe was the thing stealing "are you running". Order inside the rule:
stop → approvals → status → task → activity, so "stop the current task" cannot be read as
"what is the current task".

`stop_current_task` is LOW and confirmation-free on purpose: §30 of the build prompt requires a
stop to be immediately executable, and PHYSICAL stays reserved for the emergency stop.

Every handler refuses when `ctx.core` is absent rather than building a fresh registry/engine —
a self-report from an object the handler just constructed would describe that object, not the
running system.

## 4. One behaviour the live run taught, not the suite

Driving a gated command on an isolated instance showed that a command stopped at the permission
gate **writes no task row at all**:

```
tasks: (empty)          confirmations: CONF-…  state=pending
```

So "what is the current task" honestly answered _"Nothing is running right now"_ — true, and
useless to someone who had just watched an approval appear. Both facts are now said:

```
Nothing is executing right now, but 1 action(s) is waiting for your approval --
ask me for pending approvals to see what they are.
```

Pinned by `test_nothing_executing_while_an_approval_waits_says_both`, written after the
observation and before the change.

## 5. Live, over HTTP, on an isolated instance

Never against the user's running app. Fresh `JARVIS_HOME=$(mktemp -d)`, a port bound from an
ephemeral socket after the first choice turned out to be occupied by something else (a 401 from
an unknown server is not a test subject).

```
delete /tmp/p53target/delete-me.txt  -> CONFIRM  "This needs your confirmation before I run it."
what is the current task             -> "Nothing is executing right now, but 1 action(s) is
                                         waiting for your approval…"                    1 ms
show pending confirmations           -> "1 action(s) waiting for a decision: delete_file
                                         (CONF-77a0e54a…). Each expires 300 seconds…"   1 ms
are you running                      -> "Vantage is running in Basic Mode: deterministic
                                         rules, no model. 141 capabilities across 15
                                         agents, 1 waiting for approval…"              1 ms
what did you just do                 -> "4 most recent action(s): 3 success,
                                         1 awaiting_confirmation. Last was
                                         get_vantage_status -> success."               1 ms
stop the current task (idle)         -> "Nothing was stopped: no task is running."     1 ms
```

The cancel path, against a job with one finished step and one pending step, then read back
from SQLite independently of the tool:

```
what is the current task -> Current task: "p53 live stop proof" -- running, 1 of 2 step(s) done.
stop the current task    -> Stopped "p53 live stop proof" -- the job is cancelled. Steps that
                            already ran are not undone (1 done). Not started or interrupted:
                            delete_file.                                    verified=True
sqlite read-back         -> ('JOB-P53LIVE', 'cancelled')
```

Cleanup: the isolated home, its token file and `/tmp/p53target` were removed; the user's own app
was left running and was verified still serving.

## 6. Browser

**Not performed this phase, and no UI claim is made.** No page changed: the five capabilities
are reachable by sentence and by `POST /command`, and the phone client's approvals list already
polls `/confirmations`, which this phase did not touch. Claiming a browser verification that
did not happen is the thing §30 of the operating prompt forbids.

## 7. Failures found in this phase's own work

Five of my test-harness assertions were wrong while the product was right, and each was fixed
in the test with the reason recorded rather than by loosening a claim:

1. `result.output["verified"]` — `ok()` consumes `verified` as a field, not an output key. The
   assertion now reads `result.verified`.
2. A Basic-Mode assertion banned the word "model" and so failed on the phrase "no model".
3. An empty-history assertion expected "nothing"; the tool says "no recorded actions". Both
   honest; the test now matches the product.
4. A no-op stop was asserted to be non-SUCCESS. Reconsidered: the tool _knows_ nothing was
   stopped, so `ok` with `cancelled=False` is right, and what must be forbidden is a message
   claiming a stop. The test now asserts that instead.
5. A helper lost its `ToolContext` import to my own bulk edit, producing 16 `NameError`s that
   looked like product failures.

Two further things the full run caught that the module run did not — the session-database leak
this project has now been bitten by three times: four self-state tests assumed an empty
approval queue and no active jobs. They now clear that state explicitly (`_clear_pending`,
`_clear_active_jobs`) with a comment saying why, rather than depending on test order.

## 8. Count guards, again

`test_the_registry_grew_by_exactly_two_tools` — written yesterday as `== 136` — failed the
moment Phase 53 legitimately added five tools. It is now a drift guard instead of a frozen
total: the two Phase-52 tools must exist, and **the number the README publishes must equal the
number the registry reports**. That is the defect worth catching (a stale published count), and
it cannot block real growth. The other guard, `test_the_readme_counts_the_planner_routes_it_ships`,
caught this README's own staleness during the work and is satisfied by §9.

## 9. Numbers, re-measured

|                         | before                                | after                                                                                                    | how                                                      |
| ----------------------- | ------------------------------------- | -------------------------------------------------------------------------------------------------------- | -------------------------------------------------------- |
| Test suite              | 2065 passed · 0 skipped               | **2129 passed · 0 failed · 0 skipped** (66.4 s at the last full run before the doc pass; 2129 collected) | `JARVIS_HOME=$(mktemp -d) .venv/bin/python -m pytest -q` |
| Phase 53 tests          | 0                                     | **64**                                                                                                   | `tests/test_phase53_vantage_talks_about_itself.py`       |
| Test modules            | 79                                    | **80**                                                                                                   | `ls tests/test_*.py \| wc -l`                            |
| Phase reports           | 54                                    | **55** (this file)                                                                                       | `ls audit/*.md \| wc -l`                                 |
| Lines                   | 66,334 — 40,506 source · 25,828 tests | **67,266 — 40,911 source · 26,355 tests**                                                                | `wc -l`                                                  |
| Tools                   | 136                                   | **141**                                                                                                  | registry                                                 |
| Risk spread             | 72 / 36 / 13 / 12 / 3                 | **76 / 37 / 13 / 12 / 3**                                                                                | registry                                                 |
| Confirmation-gated      | 28                                    | **28**                                                                                                   | unchanged — no gate was loosened                         |
| Planner routes          | 50                                    | **51**                                                                                                   | `len(core.planner._rules)`                               |
| Registered applications | 15                                    | 15                                                                                                       | unchanged                                                |
| HTTP surface            | 113 objects / 107 paths               | **113 / 107**                                                                                            | no new endpoint was added                                |

## 10. Known limits, and what is still open

- **53-A** `stop_current_task` cancels the newest job in an active state. There is no
  in-process registry of the command executing _right now_ — a job row is written when a plan
  is created, and a gated command writes none. So "what are you doing" during a running
  multi-step command is answered from the table, which is correct but not instantaneous.
- **53-B** `get_vantage_status` and `/health` are two projections of the same objects, not one
  shared payload. A test pins their agreement on the fields they share; a future phase may want
  one builder for both.
- **53-C** `get_recent_activity` reports what the ledger says and does not verify the chain.
  This machine's history still carries the 5 deliberate forged rows, so the dashboard continues
  to read `Audit chain: BROKEN`. Cleaning them is a user decision, not a fix.
- **53-D** The phone UI still polls `/confirmations` directly rather than the new tool. Same
  source, two readers; harmless today, and worth unifying if the tool ever gains logic the
  route lacks.
- **53-E** `search Google for X` remains the one §6 gap, and remains a capability rather than a
  route — no tool performs a web search. It was **not** aliased to `open Google`; a test asserts
  a search request is not answered by navigating to a home page.
- **53-F** No emergency-stop **route** yet (Gap B), no tool-level revoke surface (Gap C), no
  first-run permission request path (Gap E), no host persistence (Gap F), no native mobile app
  (Gap G), no relay (Gap H), no provider adapters (Gap I), no Computer Use (Gap J). The roadmap
  order in §26 of the operating prompt is unchanged.

## 11. Delivery

Phase report written; README, badges, `PROJECT_DETAILS.md`, `VANTAGE_CURRENT_STATE.md`,
`benchmarks/suite_history.json` and the growth chart updated to the §9 numbers; committed
locally; bundle rebuilt, re-signed, redeployed to `~/Downloads/Vantage/`, relaunched, and the
extracted archive re-verified. Commit hash and the delivered-app checks are recorded in the
commit and in the close-out message rather than asserted here in advance.
