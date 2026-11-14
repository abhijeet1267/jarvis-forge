# Phase 26 — Recovery and risk trust, measured on the tree that has them

Report date: 2026-09-29. Continues Phases 9-25.

No brief. The scope came from the oldest carried line in the backlog. Every report from
Phase 11 onwards ends with the same list of things not touched, and in it: **"RT-3 mutable
risk tier · R-2 broad auto-retry · R-11…R-14"**. Those ids were written in Phase 9 and
re-inherited for seventeen phases without being re-measured, which is the exact failure
this program exists to catch -- Phase 25 found a stale _claim_ about the desktop bundle;
this one found that the stale claims were about recovery.

Re-measured before anything was edited. All four were real, and two were worse than their
one-line summaries.

---

## 1. How it was measured

One script, run against a throwaway `JARVIS_HOME` and a sandboxed `HOME`, printing the
state of the tree rather than a verdict:

```bash
JARVIS_HOME=$(mktemp -d) HOME=$(mktemp -d) PYTHONPATH=. python <sweep>   # M1-M7 below
```

| Probe | Question                                                                           | Result before                                                                                                                                                                    |
| ----- | ---------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| M1    | Which registered tools does the retry policy let run twice, and _on what grounds_? | **45 of 127** -- every tool between read tier and risk 3 whose failure text matched a regex                                                                                      |
| M2    | What does `TaskManager.retry()` actually do to a failed job?                       | `failed → running`, both steps `pending`, `detail` **erased**, 0 executions; the briefing then said **"1 job(s) in progress."**                                                  |
| M3    | Is `resume()` the same family?                                                     | Yes: `paused → running` with nothing to dispatch it                                                                                                                              |
| M4    | Can a tool rewrite the fields the gates read?                                      | Yes, three ways: gate `CONFIRM → EXECUTE`, `is_retryable False → True`, planner catalogue `risk=read confirm=False`                                                              |
| M5    | Symmetry of the failure ledger                                                     | write: exception swallowed at `log.info`, 0 rows, normal reply; read: `failure_stats()` and `recent_failures()` both **raised** through `/metrics`, `/failures` and the briefing |
| M6    | What does a successful fallback leave behind?                                      | `outcome.result.tool == 'list_directory'` sitting on the step planned as `search_files`, attributed nowhere                                                                      |
| M7    | Does the confirmed-action path record its failures?                                | A failed approved `delete_file`: `failures 1 → 1`. Nothing. `recovery_notes: []`                                                                                                 |

The scheduler half of M7 was measured separately, because `PHASE9_RECOVERY_AUDIT.md` R-12
names it alongside the confirmation path:

```
run_job -> {'outcome': 'failed', 'detail': 'File not found: /private/tmp/definitely-absent-p26'}
failures rows: 0 -> 0
schedule_runs: {'schedule_id': 'SCH-p26', 'outcome': 'failed', 'detail': 'File not found: …'}
```

So that half was **half right**: an unattended failure was not invisible, it had its own
ledger (`schedule_runs`, correctly `failed`) -- but the _recovery_ ledger, which is what
`/metrics` and `explain_failures` read, had never heard of it.

Note also that Phase 9's two reports number these differently: `PHASE9_FINAL_REPORT.md`
R-11…R-14 are _not_ the same four findings as `PHASE9_RECOVERY_AUDIT.md` R-11…R-14. This
report uses the **recovery audit's** numbering, which is the one the backlog copied.

---

## 2. R-2 — the guard asked the wrong question

`is_retryable()` was a correct piece of code answering a different question:

```python
if spec.risk >= RiskLevel.DESTRUCTIVE:      # "is it destructive?"
    return False
if spec.name in IDEMPOTENT_TOOLS or spec.risk is RiskLevel.READ:
    return True
return looks_transient(result)              # "did the error text sound transient?"
```

The question that matters is **"is running this twice safe?"** Everything below risk 3 fell
through to a regex over the error string, so the safety of a re-run was decided by what the
failure _happened to say_. M1's 45 offenders included `move_file`, `copy_file`,
`write_file`, `create_event`, `update_event`, `schedule_task`, `run_schedule_now`,
`send_to_device`, `register_phone`, `apply_code_change`, `run_allowlisted_command`,
`run_workspace_tests`, `control_home_device`, `run_home_routine`, `vibrate_watch`,
`send_watch_notification`, `compress_path` and `remember_fact`.

The case that makes this concrete is the one the audit already named: a killed subprocess
reports `command timed out after 30.0s`, which matches `timed?\s*out` -- and a timeout is
precisely the state in which the effect is _unknown_. The tool that answered "it looked
transient" was the one tool that could not know whether the first attempt had already
moved the file.

### What changed

`retry_policy()` is now the single source of the decision, and returns the reason with it:

```python
if spec is None:                -> "it is not a registered tool, so nothing is known about it"
if "safety" in spec.tags:       -> "it is a safety action"
if spec.risk >= DESTRUCTIVE:    -> "it is a destructive action, and a retry could act on a
                                   world you did not approve"
if read tier or idempotent:     -> may retry
otherwise:                      -> "its first attempt may have changed something already,
                                    so running it twice is not known to be safe"
```

`looks_transient()` survives, demoted to what it can honestly do: stop retrying early, and
word the note. It no longer grants anything. Re-measured with M1: **45 → 0**.

Deriving the reason from the same call that makes the decision also closed a defect this
report did not set out to fix. `recover()` used to re-construct a `skip_reason` after the
fact with its own `if spec.risk >= DESTRUCTIVE … else "the error is not transient"` chain,
which was wrong twice over: it misattributed every non-destructive refusal to the error
text, and it said **"not retried"** after retries had run (Phase 9's R-12 in the _final
report's_ numbering, asserted as a known-open item in `test_phase10_verification_authority`
since Phase 10). The note now counts its own attempts:

```
not attempted : "… — I could not recover automatically (not retried: its first attempt may
                have changed something already, so running it twice is not known to be safe)"
attempted     : "permission denied — I could not recover automatically (retried 1 time(s),
                then stopped: the error is not transient)"
```

### What this costs, said plainly

An EXTERNAL tool that genuinely timed out no longer self-heals. `create_event` failing on a
CalDAV hiccup used to be tried twice and sometimes succeeded; it now escalates on the first
failure and says why. That is the intended trade -- an automatic recovery of an unknown
effect is not worth more than the effect -- and it is reversible per tool: `IDEMPOTENT_TOOLS`
is the list a tool earns its way into by argument. Nothing was added to it here; the 16
names in it today are all reads.

---

## 3. RT-3 — the tier the gate reads was a mutable field

Measured, in one transcript:

```
delete_file: risk=destructive confirm=True tags=['files', 'destructive']
gate before:      CONFIRM (level 3 (destructive) action requires confirmation)
retryable before: False
mutation accepted: risk now read confirm now False
registry hands back the SAME object: True
gate after:       EXECUTE (low-risk action)
retryable after:  True
planner prompt line: ['- delete_file(path:string, permanent:bool) risk=read confirm=False ::
                       Delete a file or folder. …']
tags mutated in place: ['files', 'destructive'] -> ['files', 'destructive', 'safety']
parameters mutated in place: True
```

Three consequences from two assignments, because `PermissionEngine.evaluate()` does its own
`self.tools.get(action.tool)` and the registry hands out the _same object_ a tool module
already holds. `spec.risk = RiskLevel.READ` on a destructive tool is a confirmation bypass;
the same line makes the action automatically retryable; and `describe_for_prompt()` starts
advertising `confirm=False` to whatever planner reads it. Phase 9's red team called this P1
and recommended `model_config = ConfigDict(frozen=True)`.

Frozen alone was not enough, and that is the part worth recording: `frozen=True` stops
`spec.tags = [...]` but not `spec.tags.append("safety")`, and `is_retryable()` decides
whether an emergency stop may be re-run by reading membership in that list. So the three
fields any gate reads membership in became tuples:

```python
model_config = ConfigDict(frozen=True)
allowed_devices: Tuple[str, ...] = ("laptop_main",)
required_parameters: Tuple[str, ...] = ()
tags: Tuple[str, ...] = ()
```

Every existing declaration site passes lists through the `spec()` helper and pydantic coerces
them, so no tool module changed. `/tools` still serialises (127 rows over HTTP, `delete_file`
at `risk=3 confirm=True`), and `int(s.risk)` sorting is untouched.

After the fix, the same probe reads:

```
mutation refused: ValidationError 1 validation error for ToolSpec
  risk  Instance is frozen [type=frozen_instance, …]
tags mutation refused: AttributeError
parameters mutated in place: True
```

### The residual, named rather than papered over

`parameters` is still a plain dict, so `spec.parameters["x"] = "y"` works. It was left
because the alternative -- `Mapping[str, str]` validated into a `MappingProxyType` -- breaks
serialisation: pydantic 2.13 raises `Unable to serialize unknown type: mappingproxy` on
`model_dump_json()`, and `/tools` serves that field. What it would buy is nothing: no gate
authorises from `parameters`. It is read by `validate_params()`, which uses it to _reject_
unexpected parameters, so widening it removes a strictness rather than granting a
permission. Recorded under Known open items with that reasoning.

---

## 4. R-11 — a lifecycle verb invented a state

`retry()` was three statements about the world, none of them true:

```python
UPDATE task_steps SET status = 'pending', detail = '' WHERE job_id = ? AND status = 'failed'
self.set_status(job_id, "running")
return True
```

Nothing in the build re-dispatches a job row -- `briefing.py:86` and `tools/system.py:259`
_read_ `status == "running"`, they do not act on it -- so the measured result was a job
permanently `running` with 0 executions, and its failure text deleted. The user-visible
version, from the briefing in the same process:

```
briefing would say: ['1 job(s) in progress.']      # before
briefing would say: ['1 job(s) failed.']           # after
```

`resume()` was the same fabrication one verb over (`paused → running`), and `pause()`/
`cancel()` were the honest ones. The fix is that a verb reports the state its steps are
actually in rather than asserting one:

```python
@dataclass
class LifecycleReport:      # __bool__ -> accepted, so every old call site still works
    job_id, action, accepted, status, executed = 0, steps_touched, reason, message
```

- `retry()` leaves the failed step `failed`, appends `| re-attempt requested <iso>` to its
  `detail` instead of erasing it, recomputes the job status from the steps, and says
  `executed: 0`.
- `resume()` calls `refresh_status()`, so a job whose step is still waiting on an approval
  reports `awaiting_confirmation` -- measured M3 now returns exactly that, with the message
  "an approval is still waiting on it".
- A verb that cannot apply answers `accepted=False` with a reason, and the endpoint maps
  `no such job` to 404 and everything else to 409 instead of flattening both into 404.

Live against a launched server (`run.py serve`, port 8899, real screenshot failure as the
failed step):

```
status: failed | steps: [('get_time','succeeded','It is 12:08 …'), ('take_screenshot','failed','Screenshot failed: could not creat')]
retry  -> {'ok': True, 'executed': 0, 'steps_touched': 1, 'status': 'failed'}
  note: Nothing ran. 1 failed step(s) are marked as needing another attempt, with the
        reason they failed kept. This build has no job re-dispatcher, so ask me the goal
        again to run it; anything gated will ask for approval again.
pause  -> HTTP 200
resume -> {'executed': 0, 'status': 'failed'}
resume again (not paused) -> HTTP 409  detail: the job is not paused
retry on JOB-not-real     -> HTTP 404  detail: no such job
```

### Why there is still no re-run, and why that is the right place to stop

A genuine retry needs two things this build does not have. `task_steps` persists
`step_id, job_id, name, status, detail, seq, confirmation_id` -- no parameters column, so a
step cannot be re-issued from its own row (`jarvis/db/database.py:77-85`, read). And
re-entry would have to go through `policy.evaluate()` again; Phase 9 flagged that an
executor bolted onto `retry()` "would replay failed steps with no `policy.evaluate` call",
which is a permission hole dressed as a feature. Building it is a feature with its own
safety review, not a truth fix, so it is carried as **26-A** and the endpoint now names the
missing capability to the user instead of hiding it behind a fake `running`.

---

## 5. R-13 — one side of the ledger whispered, the other shouted

```python
except Exception as exc:                       # write side
    log.info("could not record failure", …)    # invisible
```

against unguarded readers. Measured with the `failures` table dropped, which is what a
corrupt or unmigrated home looks like from the inside: one failure occurred, the reply was
normal, zero rows existed; `/metrics` and `/failures` raised `OperationalError` and the
briefing crashed. The write loss and the read crash are the same condition seen from two
sides, and the operator got either nothing or a 500 -- never both facts at once.

Now:

- `record_failure()` returns whether the row landed, logs at `warning`, and moves a counter
  (`written`, `unwritten`, `last_error`) exposed by `recording_status()`.
- `failure_stats()` always returns its full key set and adds `ledger_error` when it could
  not read, so `/metrics` degrades into a payload that _contains the reason_ rather than
  failing: measured `/metrics` now answers 200 with
  `{'total_failures': 2, 'total_recovered': 0, 'recovery_rate': 0.0, 'ledger_error': None,
'written': 2, 'unwritten': 0}`, and with `ledger_error: 'no such table: failures'` when
  the read is broken.
- `/failures` answers **503 with the reason** instead of an unhandled 500 -- and
  deliberately not an empty list, because an empty list is what a system that has never
  failed looks like.
- The briefing says `failure ledger unavailable: <reason>` in that case, and additionally
  reports `N failure record(s) could not be written` whenever `unwritten` is non-zero.

That last pair is the actual fix: the silent-loss failure mode and the endpoint-breaking
failure mode were both answers to "is this ledger working?", and neither answer was
readable.

---

## 6. R-12 and R-14 — one closed, one closed halfway

**R-12** (the gated half bypasses recovery) is closed at its truthful half and stays
deliberately incomplete at the other. A confirmed action that fails now reaches the ledger
-- measured M7 as a test, and live over HTTP:

```
$ curl -X POST -H "$TOKEN" localhost:8899/confirm/CONF-b5cdff66…   # delete with nothing to delete
  That failed: Nothing to delete at …
  actions: [('delete_file', False, 'failed')]
$ curl -H "$TOKEN" localhost:8899/failures?limit=1
  delete_file: recovered=0 attempts=0 strategy='none'
    note: Nothing to delete at … — not retried: it ran under a confirmation, and
          one approval authorises one attempt
```

It does _not_ gain an automatic retry, and adding one would contradict everything in §2: the
tier the confirmation path serves is exactly the tier whose re-runs are unsafe, and the
approval the user gave covered one attempt. The scheduler branch is fixed the same way
(`strategy="none"`, `attempts=0`, note naming the missing recovery chain), because its
failure was recorded in `schedule_runs` but not in the ledger `/metrics` reads.

**R-14** (a fallback's result silently becomes the planned tool's result) was measured as:

```
recovered: True | result.tool: list_directory | result.summary: listed 41 entries in /tmp
tried: ['alternative:list_directory'] | note: recovered using list_directory
```

The dangerous half was the record: `_audit_action()` keys its row on `action.tool`, so the
chain said `search_files` had produced a directory listing. Fixed in both places the
attribution was missing -- `RecoveryOutcome.answered_for` (planned tool, survives into the
`recovered` audit row's detail) and `detail["answered_by"]` on the step's own row.

The remaining half is a wording gap, not a false claim, and is carried as **26-B**: the
session memory sentence still reads `list_directory: listed 41 entries` for a request that
asked to search. The reply already says the pair out loud (`search_files: recovered using
list_directory`), so nothing asserts something untrue; one stored sentence is just phrased
from the fallback's side. End-to-end attempt, recorded because it _didn't_ recover:

```
REPLY: 1. That did not work: Spotlight is indexing
       2. It is 12:35 on Tuesday, 29 September 2026.
recovery_notes: []      # the alternative ran with search_files' params and did not verify
```

---

## 7. Regressions

`tests/test_phase26_recovery_trust.py` -- **57 tests**, written red first against the seven
probes above (the first run failed on `ImportError: cannot import name 'LifecycleReport'`).
Grouped as: 26-A retry policy (5), 26-B frozen specs (6), 26-C lifecycle verbs (8, including
one that runs the page's own `JOB_STATE` in Node so a state the verbs can leave has a name
on screen), 26-D the failure ledger (5), 26-E the gated and unattended paths (4), plus the
sweep that pins the 45 → 0 number itself.

Three existing tests encoded the old behaviour and were rewritten rather than relaxed. Each
kept its substantive claim:

| Test                                                                                              | It asserted                                                                                                                                 | Now                                                                                                                                                                                                                                                                                               |
| ------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `test_recovery.py::test_external_tool_retries_only_on_transient_errors`                           | `run_allowlisted_command` **is** retryable on "connection refused"                                                                          | renamed `test_an_external_tool_is_not_retried_on_its_error_text`; asserts the opposite, and that the _reason_ comes from the policy                                                                                                                                                               |
| `test_api.py::test_task_lifecycle_roundtrip`                                                      | `resume` leaves the job `running`                                                                                                           | asserts `incomplete`, `executed == 0`, both steps still `pending`, and "nothing ran" in the note                                                                                                                                                                                                  |
| `test_phase10_verification_authority.py::test_recovery_does_not_call_a_no_op_a_recovered_success` | two invocations (one failure + one retry), and `attempts == 1` **with** a note claiming "not retried" -- a known defect asserted on purpose | one invocation (that home command is no longer re-run), and `attempts == 0` with "not retried" now true. The unverified-retry-is-not-a-recovery claim it was built to hold moved to `test_an_unverified_retry_is_not_reported_as_a_recovery`, under a read-only tool the new policy still retries |

The module docstrings of `test_recovery.py` and `recovery.py` were rewritten to state the
new property, so the next reader does not "fix" the guard back.

---

## 8. Everything re-measured after the change

|                   |                                                                                                                                                                                                                                                                          |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Suite             | **1417 passed · 0 failed · 0 skipped · 0 xfailed** (1417 collected), 50 modules                                                                                                                                                                                          |
| R-2 sweep         | tools retried on error text alone: **45 → 0**; registry unchanged at 127 tools, tiers read 68 · low 33 · external 12 · destructive 11 · physical 3                                                                                                                       |
| Acceptance        | `python benchmarks/acceptance_run.py`: 17 of 18 measured, `open VS Code` skipped as a GUI launch; median **4 ms**, sum **1,464 ms**. **No command changed state or audit token** against the previous artifact -- this phase touched recovery and lifecycle, not routing |
| Acceptance caveat | the 81 ms search row is a sandboxed-`$HOME` artifact: the previous published run walked a real home (346–1,217 ms). The root is inside the row's recorded reply, so the two runs stay distinguishable                                                                    |
| Safety matrix     | `python benchmarks/safety_run.py`: **5 of 6 ok**, `turn on heater` still FAIL -- unchanged, still 22-G, and still the owner's permission-policy call                                                                                                                     |
| Gated failure     | live HTTP `failures 1 → 2` with `strategy='none'`, `attempts=0`                                                                                                                                                                                                          |
| Figures           | `assets/build_charts.py` + `build_diagrams.py` regenerate; `chart-suite-growth.svg` ends at the Phase 26 point                                                                                                                                                           |

---

## 9. Carried forward

- **26-A** No job re-dispatcher. `retry()` records the request and says nothing ran;
  a real re-run needs step parameters persisted and re-entry through `policy.evaluate()`.
- **26-B** A fallback's summary is remembered under the fallback tool's name (§6).
- **`ToolSpec.parameters`** is still an in-place-mutable dict; no gate reads it for
  authority, and a `MappingProxyType` fix breaks `/tools` serialisation (§3).
- Everything already open: **22-G** (heater/HVAC executes unconfirmed when uniquely
  resolved -- permission policy), **23-E** (planner's `or "amr_main"` default -- safety
  semantics), microphone/speech TCC not granted (7/9 in the app's self-test), publishing a
  signed binary, **15-F** (arXiv 406), **14-D** (partly-routable sentences), **16-F**
  (security events not in the audit chain), no `read_file` route, no notification tool, no
  `ProjectGraph` delete API, hardware **BLOCKED**, LLM planner, unattended multi-step
  workflows.

---

## 10. Git

Committed and pushed to `main` on `abhijeet1267/jarvis-forge`:

```
77fbfa7  Retry on what may safely run twice, not on how the error sounded
         19 files changed, 1629 insertions(+), 173 deletions(-)
```

CI on that commit -- run `36543621386`, 2m3s, verdict copied from the runner after it
finished rather than predicted:

```
$ gh run view 36543621386 --json conclusion,jobs
conclusion: success
  suite (3.11)  success
  suite (3.12)  success
```

Two steps report `skipped` on 3.12 by design (`Build the desktop bundle`,
`Upload measured benchmark data` run on 3.11 only, added in Phase 25). The runner had no
complaints about this phase's changes: no test moved because of a laptop/runner difference
this time -- the three tests that had to change are the ones listed in §7, and they changed
because they encoded the behaviour being replaced, not because of the environment.
