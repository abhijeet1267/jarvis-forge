# Phase 27 — A retry that retries

Report date: 2026-09-29. Continues Phases 9-26.

No brief. The scope was written by Phase 26 itself, in the sentence its own endpoint
returned to users:

> *"Nothing ran. 1 failed step(s) are marked as needing another attempt… This build has no
> job re-dispatcher, so ask me the goal again to run it."*

That was the right thing to say when it was written -- Phase 26's job was to stop `retry`
from lying. But §31 of the working plan lists `retry` as a task-manager capability, and an
honest "cannot" is a promise someone eventually has to keep or retract. This phase kept it
and made it true.

---

## 1. What was actually missing (measured before touching anything)

A step could not be re-issued from its own record. Direct measurement on a real two-step
workflow whose second step fails:

```
step columns: ['step_id', 'job_id', 'name', 'status', 'detail', 'seq', 'confirmation_id']
params recoverable from the row? False | device? False
invocations after tasks.retry(): {'get_time': 0, 'battery_status': 0}
core has retry_job? False
```

The row kept the tool's *name* -- `tasks.create(goal, [a.tool for a in plan.actions])` --
and none of what that tool had been asked to do. Phase 26's own report had already named
this as the reason not to bolt an executor onto the verb (26-A), along with the second
hazard: *"If an executor is ever added, note that it would replay failed steps with no
`policy.evaluate` call."* Both observations were load-bearing, and both are dealt with here.

---

## 2. What was built

**A step remembers its own call.** Two columns, added through the same additive migration
the scheduler and Phase 14's `confirmation_id` used (`jarvis/db/database.py`, `additions`
dict):

```sql
task_steps: + params TEXT DEFAULT '{}'   + device TEXT DEFAULT ''
```

`name` already held the tool, so no third column was invented to duplicate it.
`TaskManager.create(goal, steps, planned=…)` writes them when the job opens -- not when it
fails -- so a step that never got its turn can still be re-issued.

**One execution path, not two.** The gate→execute→verify→ledger→step loop was extracted
from `handle()` into `_execute_plan(request, plan, response, started, job_id=…, seqs=…)`.
`retry_job()` calls that same method. A re-run is therefore not a second, weaker
implementation of the pipeline: it gets the parameter validation, the device check, the
risk verdict, the confirmation prompt, the recovery stage, the audit row and the step
update exactly as a first run does.

`seqs` is the part that makes a *partial* re-run safe. A retried plan contains only the
failed steps, indexed 0..n-1, but their job rows live at their original positions, so the
mapping is passed in rather than assumed. Without it, re-running step 2 would have written
its result onto step 1.

**Three rules `retry_job` exists to keep.**

* Only steps recorded `failed` are re-run -- a step whose effect was verified is never
  replayed (`executed` and the counting handlers both assert this).
* Everything goes back through the gate. A gated step mints a **fresh** confirmation and
  runs nothing until it is answered: the earlier approval was spent, and reusing its token
  would let an expired or revoked consent authorise a second execution.
* A step it cannot reconstruct is refused. Params `{}` on a tool that requires `path`
  answers `missing required parameter 'path'` with zero invocations; a step naming a tool
  that no longer exists answers *"… is no longer a registered tool"* instead of the
  `AttributeError` the risk-label lookup would have thrown.

**The trail both attempts deserve.** The re-run writes its own summary over the step, which
would silently delete the evidence Phase 26 made a point of preserving. Measured after the
fix, on a step that fails the same way twice:

```
[1] take_screenshot failed: Screenshot failed: could not create image from display
    | re-attempt requested 2026-09-29T15:21:56+00:00
    | this attempt: Screenshot failed: could not create image from display
```

`TaskManager.restore_attempt_trail()` is what keeps all three facts in one readable line.

---

## 3. Live, over HTTP, on a launched server

```
=== 1. a real workflow whose second step genuinely fails ===
  job: JOB-8c5c83b07496 | reply: 1. It is 20:47 … / 2. That did not work: Screenshot failed
    0 get_time         succeeded | params: {}      | device: 'laptop_main'
    1 take_screenshot  failed     | params: {}      | device: 'laptop_main'

=== 2. POST /tasks/{id}/retry : does it actually re-run? ===
  ok: True | executed: 1 | steps_touched: 1 | confirmation: None
  note: 1 step(s) re-ran.
  job status: failed                      # it genuinely tried, and genuinely failed again

=== 3. a gated re-run mints a fresh approval and runs nothing ===
  approving CONF-cfb050000656… (a file that does not exist):
    That failed: Nothing to delete at …
  now retrying that job:
    executed: 0 | new confirmation: True
    note: Nothing ran: the re-attempt needs your approval before it executes.
    job: awaiting_confirmation [('delete_file', 'awaiting_confirmation'), ('get_time', 'pending')]
    the approval from the retry is a NEW one: True

=== 4. unknown job ===
  unknown -> HTTP 404  detail: no such job
```

Note the honesty of case 2: the screenshot step *still* fails, because the reason is real
(no Screen Recording permission on this machine, which is a carried item) -- the retry
reported one execution and left the job `failed`. A re-run that "succeeds" because it ran
nothing would be the old bug again.

---

## 4. Regressions

`tests/test_phase27_job_retry.py` -- **16 tests**, red first (`ImportError`-free but
failing: `retry_job` did not exist, so every one of them had nothing to call). Grouped as
27-A what a step stores (2), 27-B what a re-run touches (5), 27-C the gate and fresh
approval (2), 27-D what it refuses (2), 27-E what the caller is told (5, including a
`Node`-free check that the ledger distinguishes `retry:JOB-…` from the original session and
that `verify_chain()` still holds).

Three existing tests changed, each for a stated reason:

| Test | Was | Now |
|---|---|---|
| `test_phase26_recovery_trust.py::test_the_retry_endpoint_reports_zero_executions` | asserted the endpoint's `executed == 0` -- true only because no dispatcher existed | renamed `test_a_retry_that_cannot_run_reports_zero_and_no_fake_state`: a *gated* re-run answers `executed: 0`, no `running`, and a fresh `confirmation_id`. Phase 26's claim was never "retry can't run things"; it was "a verb may not invent a state", and that survives a working retry |
| `test_pipeline.py::test_audit_records_every_action` | compared the lengths of two `recent(limit=200)` windows | scoped to its own session id. The window arithmetic stopped meaning anything once the shared session database held more than 200 rows -- Phase 27's new tests were enough to trip it, and any future suite growth would too |
| `test_pipeline.py::test_audit_result_is_a_status_token_not_a_message` | asserted `battery_status` appeared nowhere in the last 50 failure rows -- a global-state assumption, which a legitimate *failing* battery test in another file can violate | filters the failure rows to its own session. Same claim, no longer order-dependent |

The second and third were found the honest way: they passed alone and failed in the full
run. They are recorded here rather than as "flaky", because their premise -- that nothing
else in the process has ever failed at that tool -- was never true.

---

## 5. Everything re-measured after the change

| | |
|---|---|
| Suite | **1433 passed · 0 failed · 0 skipped · 0 xfailed** (1433 collected), 51 modules |
| Acceptance | `python benchmarks/acceptance_run.py`: 17 of 18 measured, median **4 ms**, sum **2,060 ms**; **no command changed state or audit token** against the Phase 26 artifact. `research` was 1,903 ms this run (arXiv 406 + retry, 15-F) and the sandboxed search 80 ms |
| Safety matrix | `python benchmarks/safety_run.py`: **5 of 6 ok**, `turn on heater` still FAIL (22-G, unchanged, owner's call) |
| Registry | 127 tools, 25 gated, tiers read 68 · low 33 · external 12 · destructive 11 · physical 3 -- untouched by a schema and orchestrator change |
| Figures | `assets/build_charts.py` + `build_diagrams.py` regenerated; `chart-suite-growth.svg` ends at the Phase 27 point |

### One run where five vision tests failed, and what it turned out to be

The first full run after this phase reported **5 failed / 1428 passed**, all of them
`@pytest.mark.slow` OCR tests (`test_ocr_reads_real_text`, `test_ocr_returns_bounding_boxes`,
`test_find_text_locates_a_word`, `test_read_image_text_tool`, `test_find_image_text_tool`).
It is recorded rather than skipped over, because "the suite was green afterwards" is how a
real defect gets buried.

What the measurements say:

* the failing run took **46.9 s**; the same file afterwards ran clean twice, in **4.5 s** and
  **3.6 s**, and a single one of those tests alone passes in 2.98 s;
* no stray project process was left running (`ps` for `run.py serve` / `uvicorn`: nothing),
  but the failures came immediately after two benchmark regenerations and a launched-server
  sweep, all of which drive the same macOS Vision helper the tests shell out to;
* the assertions that failed are the ones reading *recognised text* out of the helper --
  `KeyError: 'lines'` is a response with no lines in it, i.e. the helper answered nothing,
  not that the code changed shape.

So: an environment-sensitive test under load, in a capability (`macos_vision`) the suite
already treats as skippable when the helper is *absent*. It is not a Phase 27 regression --
none of the changed modules are on that path -- but the gap is real and belongs to the same
family as Phase 21's runner/laptop divergence: a green suite can be a *quiet-machine* suite.
Carried as **27-D**: those five tests should get the same capability/timeout treatment the
battery and ffmpeg tests got, instead of failing when the machine is busy.

---

## 6. Carried forward

* **27-A** `resume()` lifts a pause but does not dispatch a halted sequence's `pending`
  steps. `_execute_plan(job_id=…, seqs=…)` already supports it; what is missing is the
  decision about what "continue" should mean after a *denied* step -- re-offering work the
  user refused is not automatically right, and Phase 14's "one approval buys one action"
  is the precedent to argue against.
* **27-B** Jobs created before this phase store `params = '{}'`; their retry is refused by
  the gate rather than guessed at. Re-issuing the sentence is the supported path. A
  backfill is not possible from the row alone -- the parameters were never written down.
* **27-C** No dashboard affordance for a job that failed, though the endpoint exists and
  the page already renders the step states.
* **27-D** Five `@pytest.mark.slow` OCR tests fail when the machine is busy (§5). They need
  the capability/timeout treatment Phase 21 gave the battery and ffmpeg tests.
* Everything already open: 22-G, 23-E, 26-B, the `ToolSpec.parameters` residual, mic/speech
  TCC, 15-F, 14-D, 16-F, hardware **BLOCKED**, LLM planner, unattended workflows.

---

## 7. Git

Committed and pushed to `main` on `abhijeet1267/jarvis-forge`:

```
63137ef  Give retry something true to report: a failed step now re-runs
         15 files changed
```

CI run `36619081137`, verdict copied from the runner after it finished:

```
conclusion=success
  suite (3.11): success      # includes the desktop-bundle build step
  suite (3.12): success
```

The schema change matters here: both runners start from an empty `JARVIS_HOME`, so a fresh
database has to be created with the new columns *and* the additive migration path has to be
harmless when they already exist -- both are exercised on every run now.

And 27-D's diagnosis is supported rather than assumed: `ci.yml` runs plain `python -m
pytest -q` with no `-m 'not slow'` deselection, so those five `@pytest.mark.slow` OCR tests
genuinely executed on both macOS images and passed. A busy laptop is the difference, not a
code path the runner avoids.

