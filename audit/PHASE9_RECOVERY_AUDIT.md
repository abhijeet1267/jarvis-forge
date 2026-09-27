# PHASE 9 — RECOVERY AUDIT (execution-verified)

Scope: the recovery chain end to end —
`primary tool fails -> retry -> fallback -> escalation -> failure recording -> user response`.

Files audited: `jarvis/core/recovery.py`, `jarvis/core/orchestrator.py`,
`jarvis/core/models.py`, `jarvis/core/tasks.py`, `jarvis/agents/base.py`,
`jarvis/agents/laptop.py`, `jarvis/agents/laptop_center.py`, `jarvis/tools/base.py`,
`jarvis/security/policy.py`, `jarvis/security/risk.py`, `jarvis/security/audit.py`,
`jarvis/scheduler/runner.py`, `jarvis/db/database.py`, `jarvis/api/app.py`, all of
`jarvis/tools/*.py` specs, `tests/test_recovery.py`, `tests/test_pipeline.py`.

Mode: **read-only on production code.** No file under `jarvis/`, `app/` or `tests/`
was modified. The only repository write is this file.

Central property under attack: **a failure must never be able to present itself as a
success.**

---

## 0. How this was executed (reproducibility)

* Every run used `JARVIS_HOME=$(mktemp -d)` and `HOME=$(mktemp -d)`. The user's real
  `~/.jarvis-forge` was never opened. One side effect of an experiment moved a scratch
  file (`/tmp/p6scratch/fallback/victim.txt`) into `_trash_dir()`; that directory is
  `Path.home()/.Trash` (`jarvis/tools/files.py:44-46`), so `HOME` was redirected for
  every run after it. Verified afterwards: `~/.Trash/victim.txt` → `exists: False`.
* Runner: `/Users/abhijeetmiskin/AppData/jarv/jarvis-forge/.venv/bin/python` (3.11.16).
* As-shipped experiments import the real repo (`sys.path.insert(0, repo)`), so they
  measure the code exactly as it stands today.
* Policy/behaviour experiments that need working tools use a throwaway copy:

  ```bash
  rsync -a --exclude .venv --exclude __pycache__ --exclude .pytest_cache --exclude dist \
    /Users/abhijeetmiskin/AppData/jarv/jarvis-forge/ /tmp/p6shadow/
  # /tmp/p6shadow/jarvis/tools/base.py:16 -> from ..core.models import ActionResult, Verification
  ```

  That one line is the only difference between `/tmp/p6shadow` and the repo.
  Registry facts measured from the copy: **126 tools**, risk distribution
  `{READ:68, LOW:33, EXTERNAL:11, DESTRUCTIVE:11, PHYSICAL:3}`, **24** with
  `requires_confirmation=True`.
* Runtime injection (replacing a handler, or one entry of the `ALTERNATIVES` dict) was
  done **in-process only**, inside `/tmp/p6scratch/*.py`. No repo file was patched to
  inject. Every injected element is named in the experiment.
* Scratch scripts: `e1_real_baseline.py`, `e1b_mutation_vs_report.py`,
  `e1c_confirm.py`, `e1d_effect_vs_report.py`, `e1e_destructive_effect_reported_failure.py`,
  `e2_retry_matrix.py`, `s1_unverified_recovery.py`, `s2_double_apply.py`,
  `s3_fallback.py`, `s3b_permission_bypass.py`, `s4_loops_persistence.py`,
  `s5_success_without_observation.py`, `s6_stamp_and_real_recovery.py`,
  `s6b_end_to_end_false_recovery.py`, `s6c_end_to_end_false_recovery.py`,
  `s7_degraded_sources.py`, `s8b_consolidation.py`, `s9_shipped_fallbacks.py`.
* Verdict vocabulary: **PASS** (property held), **FAIL** (property broken),
  **UNVERIFIED** (code evidence only, not executed).

### The chain, with a hop at each step

| # | hop | file:line | what it actually does |
|---|---|---|---|
| 1 | handler raises | `jarvis/tools/base.py:16,103,110,117` | `Verification` unimported → every `ok()` raises `NameError` (known P0) |
| 2 | swallow | `jarvis/agents/base.py:58-63` | `except Exception` → `fail(tool, f"{tool} raised: {exc}")` — the raw Python message becomes the tool's *error text*, i.e. input to the retry classifier |
| 3 | trigger | `jarvis/core/orchestrator.py:259-266` | `if not result.ok:` → `recovery.recover(action, ctx, agent.execute, spec, result)` |
| 4 | retry decision | `jarvis/core/recovery.py:117-134` | never retry `risk>=3` or tag `safety`; **always** retry `risk==0`/`IDEMPOTENT_TOOLS`; otherwise retry if the *error string* looks transient |
| 5 | retry loop | `jarvis/core/recovery.py:157-181` | `max_retries=2` (`:111`), exponential backoff `0.2·2^(n-1)` (`:159`), accepts `retry_result.ok` as recovery (`:169`) |
| 6 | fallback | `jarvis/core/recovery.py:192-215` | runs `ALTERNATIVES[tool]` with the **primary's own params**, **no** `policy.evaluate`, accepts `alt_result.ok` (`:209`) |
| 7 | escalation | `jarvis/core/recovery.py:218-225` | writes a note into the outcome; **never** asks the user, **never** sets `needs_confirmation` |
| 8 | failure record | `jarvis/core/recovery.py:228-257` → `jarvis/db/database.py:261-272` (`failures` table) | single writer; exceptions swallowed at log-level `info` (`:256-257`) |
| 9 | status | `jarvis/core/orchestrator.py:283-297` | `ActionStatus.SUCCEEDED if result.ok`; job `"done"` if every step SUCCEEDED |
| 10 | audit | `jarvis/core/orchestrator.py:120-146` (`:133`), `:272-281` | status token derived from **`result.ok`**, never from `result.verification` |
| 11 | memory | `jarvis/core/orchestrator.py:451-460` | stores `"{tool}: {summary}"` of the *final* result only |
| 12 | response | `jarvis/core/orchestrator.py:462-483` | recovery visible **only** when `recovered` is true (`:481-482`) |
| 13 | confirmation path | `jarvis/core/orchestrator.py:390-407` | executes approved tools and **bypasses hops 3-12 entirely** (no recovery, no failure record) |
| 14 | scheduler path | `jarvis/scheduler/runner.py:122-129` | executes and records `executed/failed`; **no recovery, no failure record** |
| 15 | user "retry" | `jarvis/core/tasks.py:108-124` ← `jarvis/api/app.py:587-600` | flips step status only; nothing re-executes |

---

## 1. EXP-01 — As-shipped: where does the swallowed `NameError` go?

Setup: real repo, `JARVIS_HOME=$(mktemp -d)`, wrapper counting `Agent.execute`,
`RecoveryEngine.recover`, `record_failure`.
`./.venv/bin/python /tmp/p6scratch/e1_real_baseline.py`

Observed (literal):

```
max_retries = 2 base_delay = 0.2

=== query: 'what time is it' ===
wall_ms            : 207
handler calls      : 2
    ('get_time', False, 'failed', "get_time raised: name 'Verification' is not defined")
    ('get_time', False, 'failed', "get_time raised: name 'Verification' is not defined")
recover() calls    : 1  {'recovered': False, 'strategy': 'escalated', 'attempts': 1,
                         'tried': ['retry#1'],
                         'note': "... — I could not recover automatically (not retried: the error is not transient)"}
failures table rows: 1
recovery_notes     : []
REPLY              : "That did not work: get_time raised: name 'Verification' is not defined"
action_logs by result: {'failure': 2}
failure_stats      : {'total_failures': 2, 'total_recovered': 0, 'recovery_rate': 0.0}
```

`get_time` is `risk=READ`, so `is_retryable` returns `True` unconditionally
(`recovery.py:130`) — one re-execution happens — then `looks_transient` is False so the
loop breaks (`:178-181`).

Verdict: the P0 does **not** manufacture a false success at this hop (the conversion is
`ok=False` in every case). It does three other things: it makes recovery re-run handlers
for nothing, it feeds raw interpreter text into the error-string classifier, and it
produces failure records whose note is factually wrong (§6, R-3).

---

## 2. EXP-02 — As-shipped: the effect lands, the record says it failed

Setup: real repo, real handlers, correct parameters, `JARVIS_HOME`/`HOME` in temp.

```
./.venv/bin/python /tmp/p6scratch/e1d_effect_vs_report.py
copy_file      risk=EXTERNAL     reported ok=False verification=failed   effect_landed=True
move_file      risk=EXTERNAL     reported ok=False verification=failed   effect_landed=True
delete_file    risk=DESTRUCTIVE  reported ok=False verification=failed   effect_landed=True
compress_path  risk=LOW          reported ok=False verification=failed   effect_landed=True
```

Full pipeline, through the confirmation flow (`e1e_destructive_effect_reported_failure.py`):

```
planner 'delete /tmp/p6scratch/eff2/precious.txt' -> [('delete_file', {'path': '.../precious.txt'})]
  victim exists AFTER : False (False => the delete DID run)
  reported ok         : False
  REPLY               : "That failed: delete_file raised: name 'Verification' is not defined"
  failures rows       : 0
  audit tail          : {'tool':'delete_file','result':'failure','risk':'confirmed','confirmation':1}
```

And risk-tier behaviour of recovery on the as-shipped build (`e1c_confirm.py`):

```
A remember_fact (risk LOW)  executions: 1   reported ok: False  memory rows written: 2
B copy_file  (risk EXTERNAL) approved -> executions: 1   failures rows: unchanged
C delete_file (risk DESTRUCTIVE) approved -> executions: 1  failures rows: unchanged
```

Verdict: **FAIL (R-1, P0-consequence).** The P0's real recovery-system effect is the
*inverse* of a false success — every mutating tool performs its effect and is then
reported, audited and metric-ed as a failure — with two aggravating facts: (a) the
recovery engine is not consulted at all on the confirmation path, so these failures are
never written to `failures` (row count stayed 0 through two destructive failures), and
(b) the audit chain, `/metrics`, the briefing and `explain_failures` therefore describe a
system whose destructive half does not exist. The dangerous user-side consequence is a
manual re-run of an action that already happened (a second `delete`/`move`/`copy`),
outside every retry guard. **Note:** the planner also mis-captures paths
(`copy file X to Y` → `source='file X'`, `delete the file Y` → `path='the file Y'`), which
is a separate defect observed while building these cases.

---

## 3. EXP-03 — Retries are bounded (and where the bound is)

Setup: shadow copy; `RecoveryEngine` driven with a counting executor that always fails;
real registry specs; `./.venv/bin/python e2_retry_matrix.py`, `s6c_end_to_end_false_recovery.py`.

Policy sweep over all 126 real specs (transient error `connection timed out after 30.0s`):

```
READ         tools= 68 auto-retried= 68
LOW          tools= 33 auto-retried= 33
EXTERNAL     tools= 11 auto-retried= 11
DESTRUCTIVE  tools= 11 auto-retried=  0
PHYSICAL     tools=  3 auto-retried=  0
count of non-read tools that will be re-executed = 44  (of which risk>=2: 11, all 11 require confirmation)
```

Measured re-executions (real specs, `max_retries=2`, `base_delay=0.2`):

```
tool                     tier         re-execs attempts        tried          wall_ms
get_time                 READ                2        2        retry#1,retry#2    604.0
run_home_routine         LOW                 2        2        retry#1,retry#2    603.9
copy_file                EXTERNAL            2        2        retry#1,retry#2    603.8
delete_file              DESTRUCTIVE         0        0        retry:skipped        0.3
command_robot_motion     PHYSICAL            0        0        retry:skipped        0.3
emergency_stop           PHYSICAL            0        0        retry:skipped        0.3
```

Bound is real: worst case per action is `1 initial + 2 retries + |ALTERNATIVES|`,
i.e. ≤ 3 executions and ≤ 0.6 s of backoff. `attempts` is a *retry* counter and excludes
the initial execution, so `attempts=N` means `N+1` real executions.

Not clamped: `RecoveryEngine(max_retries=1000)` → **1001 executions** measured
(`42.2 ms` of injected-zero handlers). Nothing in production passes that argument
(`orchestrator.py:71` uses the default), and there is no `Settings`/env key for it
(`grep max_retries jarvis/config.py .env.example` → no match), so the constant is
`jarvis/core/recovery.py:111` only.

Verdict: **PASS** on boundedness, **PASS** on "no loop" (§7). Worst-case blocking for one
`EXTERNAL` tool whose real subprocess timeout is 30 s (`tools/system.py:213`) is
30.0 + 0.2 + 30.0 + 0.4 + 30.0 = **90.6 s inside a single `handle()` call** — arithmetic
on the shipped constants, not measured (labelled UNVERIFIED in §9).

---

## 4. EXP-04 — Destructive actions are NOT blindly retried… above tier 3

`is_retryable` (`recovery.py:124-134`) draws the line at `RiskLevel.DESTRUCTIVE` = **3**.
The registry puts several effect-producing tools **below** it, so they are retried
whenever the error *text* merely looks transient (`TRANSIENT_PATTERNS`, `:33-48`,
matched against `error + summary`, `:104`):

| tool | declared risk | spec | non-idempotent effect a second execution repeats |
|---|---|---|---|
| `move_file` | **EXTERNAL(2)** | `tools/files.py:522-524` | destroys the source; the test suite believes it is DESTRUCTIVE |
| `copy_file` | EXTERNAL(2) | `files.py:509-511` | overwrites `destination` again |
| `create_event` | EXTERNAL(2) | `calendar.py:402-405` | duplicate calendar event (CalDAV path, `integrations/calendar_provider.py:426,452` 20 s timeouts) |
| `update_event` | EXTERNAL(2) | `calendar.py` | second write on possibly-new state |
| `schedule_task` | EXTERNAL(2) | `scheduler.py:246-249` | duplicate schedule row |
| `send_to_device` | EXTERNAL(2) | `phone.py:606-609` | duplicate file pushed to the phone |
| `register_phone` | EXTERNAL(2) | `phone.py:563-566` | duplicate pairing record / rotated secret |
| `run_allowlisted_command` | EXTERNAL(2) | `system.py:310-312` | re-runs an arbitrary allowlisted binary; `powerful_commands` (`config.py:117`) makes this `pip`/`npm install` |
| `apply_code_change` | EXTERNAL(2) | `coding.py:412-415` | patch applied twice |
| `compress_path` | **LOW(1)** | `files.py:548-550` | truncates and rewrites the archive |
| `control_home_device`, `run_home_routine`, `open_app`, `close_app`, `vibrate_watch`, `send_watch_notification`, `remember_fact`, `create_project`, `add_paper`, `draft_email`, `take_screenshot` | LOW(1) | `tools/*.py` SPECS (33 tools, all `requires_confirmation=False`) | physical actuation, notifications and persistent writes repeated up to 3× with no approval step at all |

Proof that the retry path is reachable with a **real** error string, every hop on real
code (`s2_double_apply.py`):

```
--- hop1: real run_command timeout (tools/base.py:62-73) ---
  returncode=124 stderr='command timed out after 0.5s'
--- hop2: the string run_allowlisted_command would report (system.py:213-217) ---
  error text  : 'sleep exited with 124: command timed out after 0.5s'
  looks_transient() = True
--- hop3: is_retryable on the REAL spec ---
  spec risk = EXTERNAL, requires_confirmation=True
  is_retryable = True
--- hop4: recover() with a counting executor returning that same result ---
  re-executions=2  attempts=2  tried=['retry#1', 'retry#2']  wall=604 ms
```

`hop1` is literally the string a timed-out command produces, and a timeout is the one
error where "nothing changed" is *unknown*: the child was killed mid-work (`base.py:70-73`).
The policy inverts that: `recovery.py:32-33` comments "the world probably has not changed"
for `timed out`, while `move_file`/`copy_file`/`run_allowlisted_command` failures
(`files.py:279`, `:304`, `system.py:217`) embed that exact substring on their way out of a
partially-applied effect.

Real double-apply with a persistent, observable effect (only the error is injected; the
write is the production handler):

```
--- hop5: real handler + observable persistent effect, error injected ---
  remember_fact risk=LOW requires_confirmation=False
  handler invocations        : 3
  memories rows before/after : 0 -> 4  (+4)
  wall                       : 618 ms
--- control: same handler, non-transient error ---
  handler invocations=1  memories +2
```

Note the reply in that run was `'That did not work: connection reset by peer'` — the user
was told the action failed while its side effect had been applied three times.

The suite's own guarantee is vacuous: `tests/test_recovery.py:23-29` defines a `_Spec`
stand-in and `:57-68` asserts `("move_file", RiskLevel.DESTRUCTIVE)` — a risk the real
`move_file` spec does not have — and `:75-79` asserts as *correct* that
`run_allowlisted_command` (EXTERNAL) is retried on `connection refused`. All 22 recovery
tests pass on the broken build (`pytest tests/test_recovery.py -q` → `22 passed`), so
none of them touch `tools/base.py`, the registry or the orchestrator.

Verdict: **FAIL (R-2, P1)** for risk 1-2; **PASS** for risk>=3 and for the `safety` tag
(0 re-executions measured for `delete_file`, `command_robot_motion`, `emergency_stop`).
Also **FAIL** as a *test-coverage* finding: the destructive-retry guarantee is asserted
against fabricated specs, not the 126 real ones.

---

## 5. EXP-05 — The fallback stage: dead in the shipped config, and unauthorised when alive

**(a) Shipped fallbacks can never succeed** (`s9_shipped_fallbacks.py`, zero injection):

```
read_file -> search_files:
   tried=['retry#1','alternative:search_files'] attempts=2 recovered=False strategy=escalated
   alternative verbatim result: ok=False error="invalid parameters: search_files() got an unexpected keyword argument 'path'"
search_files -> list_directory:
   alternative verbatim result: ok=False error="invalid parameters: list_directory() got an unexpected keyword argument 'query'"
```

`ALTERNATIVES` (`recovery.py:72-78`) copies the primary's params verbatim
(`:201-207`), but no declared pair shares a parameter name. The "alternative tool" stage
documented at `recovery.py:3` is **inert for every tool in the registry** — 100% of
fallback attempts in these runs ended in `invalid parameters`.

**(b) What a working fallback does instead** — one entry injected at runtime
(`ALTERNATIVES['compress_path'] = ['delete_file']`, `s3b_permission_bypass.py`), driven
through the real planner, real policy, real recovery, real handlers:

```
primary  compress_path : LOW confirmation: False
fallback delete_file   : DESTRUCTIVE confirmation: True
PermissionEngine verdict for that delete: CONFIRM - level 3 (destructive) action requires confirmation

victim exists before: True
victim exists AFTER : False
reply               : 'Moved victim.txt to Trash.\n\n(Recovered automatically — compress_path: recovered using delete_file)'
actions reported    : [('delete_file', True, 'success')]
plan step status    : [('compress_path', 'succeeded')]
confirmations rows  : 1 -> 1 (no approval was ever requested)
audit tail          : {'tool':'compress_path','result':'success','risk':'low','confirmation':0}
                      {'tool':'compress_path','result':'recovered','risk':'recovered','confirmation':0}
failures: {'tool':'compress_path','recovered':1,'strategy':'alternative','attempts':3,'note':'recovered using delete_file'}
```

Three separate defects compose here:
1. **No authorisation.** `recover()` calls `execute()` directly (`:168`, `:208`). Everything
   `PermissionEngine.evaluate` gates — `validate_params` (`policy.py:114`), device
   allowed/offline (`:119-130`), the protected-path guard (`:134`), risk → CONFIRM
   (`:145`) — is skipped for the alternative. The measured verdict for the action that
   ran was `CONFIRM`; the `confirmations` table gained no row.
2. **Semantic substitution.** The user asked to *compress*; the system *deleted* and
   reported `succeeded` for step `compress_path`. The alternative is not a
   same-meaning operation and nothing in `RecoveryOutcome` records that the goal changed.
3. **Provenance laundering.** `orchestrator._audit_action` logs `tool=action.tool`
   (`:137`) while `response.actions` carries `tool=delete_file` (`:285`). The audit chain
   says a LOW-risk `compress_path` succeeded; the destructive `delete_file` that actually
   ran appears in no audit row.

The same shape applies to the "simulator vs real device / live vs cached" question. What
is honest today (verified by executing the handlers, `s7_degraded_sources.py`):
`get_car_status` appends *"This is the last state the car reported, not a live read"*
(`tools/car.py:94-97`), `control_home_device` says *"simulated; no real device was
reached"* (`home.py:252`), `search_memory` puts `retrieval.mode=keyword_only` in output
(`memory/hybrid.py:100`).

What is not: `search_email` silently reads a **local maildir** when no IMAP account is
configured (`integrations/email_provider.py:504-511`) and reports
`'I found no messages matching …'` with `verification=success`; the fact that a different
store answered is only in `output.provider`. A user asking "any email from Bob?" is told
"none" about a mailbox that was never contacted. **FAIL (R-15, P2).**

---

## 6. EXP-06 — Can a failed verification become a success? Yes, four ways

Every case below was executed. `verification` is the tri-state at `models.py:210-224`.

**R-4 (P1) — recovery accepts `ok` as proof.** `recovery.py:169` and `:209` set
`recovered = True` from `retry_result.ok` alone. `Verification` is not even imported in
`recovery.py` (`:28`), so the module docstring's promise at `:12-14` ("only reported as
recovered if the retry or alternative actually returned a verified success") is
unimplementable in that file. End-to-end with the **unmodified** production handler
(`s6b_end_to_end_false_recovery.py`, only the first failure injected; `turn on the lamp` →
`control_home_device`, risk LOW):

```
  handler calls      : 2   wall=213ms
  ok / verified      : True / False
  verification       : unknown
  output.delivered   : False  offline=True
  step status        : ['succeeded']
  recovery_notes     : ['control_home_device: succeeded on retry 1']
  REPLY              : 'Your home gateway is offline, so I could not on Study desk lamp.
                        No device was switched.\n\n(Recovered automatically — control_home_device: succeeded on retry 1)'
  failures row       : {'tool':'control_home_device','recovered':1,'strategy':'retry','attempts':1}
  audit row          : {'result':'success','risk':'low'} + {'result':'recovered','risk':'recovered'}
  memory written     : 'User asked: turn on the lamp. Result: … No device was switched.'
  failure_stats      : {'total_failures':1,'total_recovered':1,'recovery_rate':1.0}
```

Same shape with `run_home_routine` where the retry result is produced by the **default**
`ok()` path, so the record is even stronger (`s6c_end_to_end_false_recovery.py`):

```
  recovered: True  strategy: retry  note: 'succeeded on retry 2'
  result.ok: True  verified: True  verification: success
  message handed to the user as 'recovered': "Your home gateway is offline, so I could not
      run 'study'. It would affect: … Nothing was changed."
  failures row: recovered=1 strategy=retry attempts=2
```

An action that changed **nothing** is stored as `verification=SUCCESS`, audited
`success`, marked `succeeded`, counted as a successful recovery, and pasted into the
long-term memory record. **This is the answer to the phase question: a failure becomes a
false success whenever the retry lands on a `ok=True / delivered=False` branch.**

**R-5 (P1) — `ok()` defaults to `Verification.SUCCESS` without observation.**
`base.py:102-103`: `verification = Verification.SUCCESS if verified is not False else UNKNOWN`.
An AST scan of `jarvis/tools/*.py` finds **171 `ok(...)` sites; only 16 pass
`verified=`/`verification=`, so 155 claim "external effect observed" by default.** 13 of
them say in their own message that the thing did not happen (executed scan, `s5_…py`), and
four were confirmed live:

```
  get_location_summary  ok=True verified=True verification=success
      'Location history for today is not available: location tracking is disabled.'
  get_car_status        ok=True verified=True verification=success
      'Your car adapter is not connected, so I cannot read live telemetry yet (…).'
  get_car_telemetry     ok=True verified=True verification=success
      'I could not read those car values (…).'
  search_memory         ok=True verified=True verification=success
      "I have nothing stored about 'anything' yet."
```

Consequence for the audit layer: `ActionResult.audit_result` (`models.py:265-273`) exists
specifically so "a hardware no-op" stays out of the log as a success, and **no production
file calls it** (`grep -rn audit_result jarvis/` → only the definition; only
`tests/test_pipeline.py` uses it). `_audit_action` uses `result.ok` instead
(`orchestrator.py:133`). **FAIL (R-5b, P2): the anti-false-success mechanism is dead code.**

**R-7 (P1) — agents overwrite `verified`.** `jarvis/agents/laptop.py:38-40` and
`jarvis/agents/laptop_center.py:41-43`:

```python
if result.ok and not result.verification_note:
    result.verification_note = f"executed on {ctx.platform} ({action.device})"
    result.verified = True
```

Executed (`s6_stamp_and_real_recovery.py`): a handler returning
`ok=True, verified=False, verification=unknown` (exactly what `files.py:281-288` produces
when `dst.exists()` is False, and what `unverified()` at `base.py:115-117` is for) comes
out of `LaptopAgent.execute` as `verified=True`:

```
  handler returned : ok=True verified=False verification=unknown
  after LaptopAgent: ok=True verified=True verification=unknown note='executed on macos (laptop_main)'
  unverified()     : ok=True verified=True verification=unknown
```

Because `ActionResult.model_config` is `{}` (no `validate_assignment` — measured), the
reconciler at `models.py:241-263` does **not** re-run, so the record is left in a state its
own docstring says is impossible (`models.py:245-248`: "a handler cannot claim success in
one field and uncertainty in the other"). This affects the 17 `laptop` + 4 `laptop_center`
tools — every file operation, `run_allowlisted_command`, `take_screenshot`.
`/api/stream` publishes the poisoned field: `api/app.py:543 "verified": result.verified`.

**R-8 (P3) — `verified=True` at construction is enough to mint `SUCCESS`.**
`models.py:254-256`: passing `verified=True` with no observation yields
`verification=success` (measured: `ActionResult(ok=True, tool='t', verified=True)` →
`success`). Every test helper in `tests/test_recovery.py:19-20` uses exactly that shape,
which is why the suite cannot see R-4/R-5/R-7.

**No `ok=True` literal exists anywhere else in `jarvis/`** (`grep -rn "ok=True" jarvis/` →
only `tools/base.py:105`), so there is no hard-coded success assignment to report; the
false successes are all produced by the four field-level holes above.

---

## 7. EXP-07 — Loops, budgets, persistence, visibility

`./.venv/bin/python s4_loops_persistence.py` (shadow, persistently failing tool):

```
=== A: 5 repeated turns on a persistently failing tool ===
  turn 1: execs=3 wall= 613ms failures_rows=1 notes=[] reply='That did not work: connection timed out'
  turn 2: execs=3 wall= 610ms failures_rows=2 …
  turn 5: execs=3 wall= 610ms failures_rows=5 …
=== B: 3-action plan, every step failing transiently ===
  executions: 9   step statuses: ['failed','failed','failed']
  reply: '1. That did not work: connection timed out\n2. …\n3. …'
=== C: is the escalation note ever shown to the user? ===
  reply mentions recovery/escalation: False
  stored note: 'connection timed out — I could not recover automatically'
=== D: failures table dropped, then a tool fails ===
  handle() survived a missing failures table: True
  reply: 'That did not work: connection timed out'      # record silently lost
  raised: OperationalError no such table: failures       # …but the READ path is unguarded
=== E: confirmation path (where the 24 gated tools actually run) ===
  executions during approved delete: 1   reported ok: False
  failures rows: 0 -> 0
=== F: TaskManager.retry() ===
  retry() returned: True   status: failed -> running
  steps: [('get_time','pending'),('battery_status','pending')]
  0.3s later status: running (nothing re-executes the job)
  executions observed while 'running': 0
```

Measured conclusions:

* **No loops.** 3 executions per action per turn, constant across turns (610-613 ms,
  matching `0.2 + 0.4` backoff). Budget is per *action*, so a 3-step failing plan costs
  9 executions; there is no per-turn or per-session cap. Nothing re-enters `recover()`
  from inside a handler (`grep core.handle jarvis/tools/` → no matches), and
  `run_schedule_now` (`tools/scheduler.py:204`) is EXTERNAL, so its retries re-run a whole
  scheduled job — see R-2.
* **Failure records do persist** on the direct path: exactly one `failures` row per
  `recover()` call (`recovery.py:236-255`, single writer — `grep -rn "INSERT INTO failures"`
  → `recovery.py:238` only), including `recovered`, `strategy`, `attempts`, `note`,
  `params` JSON and `created_at`. Schema at `db/database.py:261-272`.
* **Silent loss.** `record_failure` swallows any exception at `log.info`
  (`recovery.py:256-257`); with the table missing, one failure was recorded nowhere and
  nothing surfaced — while `failure_stats()`/`recent_failures()` (`:259-295`), used by
  `/api/metrics` (`api/app.py:614-618`) and the briefing (`core/briefing.py:108-118`),
  raise. Writes fail silently; reads fail loudly. **P3.**
* **Recovery is not visible when it fails.** `recovery_notes` is appended only in the
  `outcome.recovered` branch (`orchestrator.py:267-271`), and `_compose_reply` prints it
  only if non-empty (`:481-482`). The escalation sentence "I could not recover
  automatically (not retried: …)" exists only in the DB. The user sees one line of error
  text and no indication that retries or a fallback were attempted, how many times, or
  that the system gave up. **FAIL (R-9, P2).**
* **The documented "Ask user" stage is not implemented.** `recovery.py:3` promises
  `… -> Ask user -> Record failure`; `recover()` has no channel to the orchestrator
  (`grep needs_confirmation jarvis/core/recovery.py` → no match), `strategy` `"gave_up"`
  is named in a comment (`:86`) and never assigned, and the measured
  `needs_confirmation` after a non-recovered failure is `False`. Escalation is a
  write-only log line.
* **The user-triggered retry is a phantom.** `tasks.retry()` (`core/tasks.py:108-124`),
  exposed as `POST /tasks/{id}/retry` (`api/app.py:587-600`), flips failed steps to
  `pending`, sets the job to `running`, erases the failure text (`SET detail = ''`) and
  re-executes nothing — measured 0 executions, status stuck at `running`. A job that
  failed is reported as recovered/running with the evidence of why it failed deleted.
  **P2.** (If an executor is ever added, note that it would replay failed steps with no
  `policy.evaluate` call.)

---

## 8. Findings register

| ID | Sev | Finding | Evidence |
|---|---|---|---|
| R-1 | **P0 (consequence of the known import bug)** | Every mutating tool applies its effect and is then reported/audited as a failure; on the confirmation path (where all 24 gated tools run) the recovery engine is never invoked, so those failures are never recorded in `failures` at all. Audit, `/metrics`, briefing and `explain_failures` describe a system whose destructive half is invisible; the natural user response is to re-run a destructive action that already happened. | `tools/base.py:16,103`; `agents/base.py:58-63`; `orchestrator.py:259-266` vs `:390-407`; EXP-02: `effect_landed=True / reported ok=False` for `copy_file`, `move_file`, `delete_file`, `compress_path`; `failures rows: 0 -> 0` |
| R-2 | **P1** | The never-retry rule starts at risk 3, so **44 tools below that line are automatically re-executed up to 2 extra times**, including 11 EXTERNAL non-idempotent ones (`move_file`, `copy_file`, `create_event`, `schedule_task`, `send_to_device`, `register_phone`, `apply_code_change`, `update_event`, `run_allowlisted_command`, `run_schedule_now`, `run_workspace_tests`) and LOW physical/persistent ones (`control_home_device`, `run_home_routine`, `vibrate_watch`, `send_watch_notification`, `compress_path`, `remember_fact`). The gate is a *regex on the error string*, and the string a killed subprocess produces — `command timed out after 30.0s` — is exactly the case where the effect is unknown. Measured 3 executions and +4 persistent memory rows where the control (non-transient error) gave 1 and +2. | `recovery.py:124-134,33-48,104`; `files.py:509-524`, `calendar.py:402-405`, `scheduler.py:246-249`, `phone.py:563-609`, `coding.py:412-415`, `system.py:310-312`; `tools/base.py:70-73`; EXP-04 hops 1-5 |
| R-3 | **P2** | Failure records state the opposite of what happened: `skip_reason` set inside the retry loop (`:180`) is rendered with the `"(not retried: …)"` prefix (`:220`) whenever *any* retry happened. Measured `attempts=1, tried=['retry#1'], note='… (not retried: the error is not transient)'`. `attempts` also excludes the initial execution, so a double-applied destructive action is logged with a number one lower than reality. | `recovery.py:178-181,219-225,251`; EXP-01, EXP-07 |
| R-4 | **P1** | `recovered` is decided by `.ok` alone (`:169`, `:209`); `Verification` is not imported in `recovery.py:28`. A retry/fallback that lands on a `ok=True, delivered=False` branch is announced as `(Recovered automatically — … succeeded on retry 1)` while its own sentence reads "No device was switched" / "Nothing was changed", is marked `succeeded`, audited `success`, remembered in memory and counted in `recovery_rate` (measured 1.0 from one no-op). Directly contradicts `recovery.py:12-14`. | `recovery.py:12-14,28,169,209`; `orchestrator.py:267-297,451-460,481-482`; `home.py:222-249,285-300`; EXP-06 |
| R-5 | **P1** | `ok()` defaults to `Verification.SUCCESS` with no observation (`base.py:102-103`): 155 of 171 `ok()` sites claim an observed external effect, 13 of them in messages that say the effect did not happen (verified live for `get_location_summary`, `get_car_status`, `get_car_telemetry`, `search_memory`). `unverified()` (`:115-117`) has zero call sites outside `base.py`. | `tools/base.py:81-117`; EXP-06 (AST scan + live) |
| R-5b | **P2** | The field built to stop a no-op being logged as success is never used: `ActionResult.audit_result` (`models.py:265-273`) has no production caller; `_audit_action` derives the token from `result.ok` (`orchestrator.py:133`). | `grep -rn audit_result jarvis/`; `orchestrator.py:120-146` |
| R-6 | **P1** | The fallback stage authorises itself. `recover()` calls `execute()` for the alternative (`:208`) with no `policy.evaluate`, so parameter validation, device/allowed/offline checks, the protected-path guard and the CONFIRM gate are all bypassed; params are copied from the primary unchanged (`:201-207`), so goal and payload both change silently. Measured: with one `ALTERNATIVES` entry injected, a DESTRUCTIVE `delete_file` (policy verdict `CONFIRM`) executed with 0 new confirmation rows, and the audit row recorded `tool=compress_path result=success risk=low`. Latent only because the two shipped pairs are unusable (R-10). | `recovery.py:192-215`; `policy.py:107-159`; `orchestrator.py:137,272-286`; EXP-05(b) |
| R-7 | **P1** | `agents/laptop.py:38-40` / `laptop_center.py:41-43` overwrite a handler's honest `verified=False` with `True` for any `ok` result, and — because `ActionResult.model_config` has no `validate_assignment` — leave `verified=True` alongside `verification=unknown`, the exact inconsistency `models.py:245-248` claims to make impossible. 21 tools affected, including all file operations and `run_allowlisted_command`; the poisoned value is streamed to clients (`api/app.py:543`). | EXP-06 measurement |
| R-8 | **P3** | `verified=True` without observation is promoted to `Verification.SUCCESS` at construction (`models.py:254-256`), and that is the shape every recovery test uses (`tests/test_recovery.py:19-20`), so the suite structurally cannot detect R-4/R-5/R-7. All 22 recovery tests pass on a build where every tool call fails. | EXP-04, EXP-06 |
| R-9 | **P2** | Recovery is invisible exactly when the user needs it: notes are appended only on success (`orchestrator.py:267-271,481-482`); nothing reports "retried N times / tried X / gave up"; the `escalate` stage never asks the user (`needs_confirmation` measured False), and strategy `gave_up` is never assigned. The documented chain `… -> Ask user -> Record failure` has no "ask" implementation. | `recovery.py:3,86,218-225`; EXP-07(C) |
| R-10 | **P2** | Both shipped fallback pairs are parameter-incompatible and therefore dead: `read_file→search_files` and `search_files→list_directory` end in `invalid parameters: … unexpected keyword argument`. Every recovery attempt that reached the fallback stage failed there in all runs. The "alternative tool" stage advertised in `recovery.py:3` and `README.md` does not exist in practice. | `recovery.py:72-78,201-207`; EXP-05(a) |
| R-11 | **P2** | `POST /tasks/{id}/retry` (`api/app.py:587-600` → `core/tasks.py:108-124`) reports a successful retry, sets the job to `running`, resets steps to `pending`, erases `detail`, and re-executes nothing (measured 0 executions, permanently `running`). A failed job's state is rewritten to look recovered with its evidence removed. | EXP-07(F) |
| R-12 | **P2** | The 11 EXTERNAL tools that require confirmation are exactly the ones recovery never guards: `resolve_confirmation` (`orchestrator.py:390-407`) runs the action with no recovery, no `failures` record and no `verification` check, so the retry-safety analysis for that tier is vacuous while the tier is also unmeasured. The scheduler path (`scheduler/runner.py:122-129`) likewise bypasses the whole chain. | `orchestrator.py:390-407`; EXP-02, EXP-07(E) |
| R-13 | **P3** | `record_failure` swallows every exception at `log.info` (`recovery.py:256-257`), so failure records can vanish with no signal (measured: 1 failure, 0 rows, normal reply), while the read side (`failure_stats`/`recent_failures`) is unguarded and raises — `/api/metrics` and the briefing break on the same condition. | EXP-07(D) |
| R-14 | **P3** | A successful fallback replaces the primary's result in the plan step and in memory (`orchestrator.py:268,283-285,451-460`), so memory records `read_file: <a directory listing>` — a false fact that future planning retrieves. Also `_compose_reply` appends the recovery sentence to the whole reply, so in a multi-action plan unrelated steps inherit it (`:474-482`). | `orchestrator.py:267-285,451-460,474-482` |
| R-15 | **P2** | Fallback silently changes the data source while keeping the user-facing claim: `search_email` answers from a local maildir when no IMAP account is configured (`integrations/email_provider.py:504-511`) and says "I found no messages matching X" with `verification=success`; the substitution is only in `output.provider`. (Car/home/memory degradation *is* disclosed — `tools/car.py:94-97`, `home.py:252`, `hybrid.py:100` — so this is one gap, not a pattern.) | EXP-05(b)/S7 |
| R-16 | **P4** | Cosmetic/robustness: `run_command`'s timeout is 20 s by default but callers pass 30 s with no overall turn budget (`tools/base.py:49-53`, `system.py:213`); `IDEMPOTENT_TOOLS` (`:52-69`) is a hand-maintained list of 16 names, all currently risk 0 (verified) but with no invariant enforcing that; `_watch_confirmation_note`'s grammar bug in generated prose (`orchestrator.py:420-449`) and "could not on Study desk lamp" from `home.py:225` produce unreadable user text. | EXP-03, EXP-06 |

### What held (explicitly, so the audit is not one-sided)

* Retries **are** bounded: `max_retries=2` at `recovery.py:111`, measured 2 re-executions
  maximum, 0 for risk>=3, and `0.2 + 0.4 = 0.6 s` backoff per failing action (613 ms walls).
* The 14 tools at risk 3-4 were re-executed **0 times** in every measurement
  (`delete_file` DESTRUCTIVE, `command_robot_motion` and `emergency_stop` PHYSICAL were
  driven directly; the policy sweep shows all 11 DESTRUCTIVE + 3 PHYSICAL specs return
  `is_retryable == False`, including the `safety` tag rule at `recovery.py:126-127`). The
  rule at `recovery.py:124-127` works exactly as written for its own threshold — the
  problem is where the threshold sits, not whether it is enforced.
* No runaway loop across turns or across a plan; growth is linear (3 execs/turn, 1 row/turn).
* Failure records **are** persisted on the direct path, with correct `recovered`/`strategy`/
  `attempts`/`note` columns, and `failure_stats` aggregates them correctly.
* Honest degraded-source prose exists for car (`tools/car.py:94-97`), home/robotics
  simulated and undelivered branches (`home.py:222-249`, `robotics.py:166-195`), and hybrid
  retrieval (`memory/hybrid.py:100`).
* The scheduler refuses to escalate (`scheduler/runner.py:66-109`): risk ceiling enforced,
  policy re-evaluated every run, CONFIRM verdicts parked rather than auto-approved.

---

## 9. UNVERIFIED (stated, not claimed)

1. **90.6 s worst-case blocking** for one EXTERNAL tool with a real 30 s subprocess timeout
   is arithmetic on `recovery.py:111,159` + `system.py:213`; not measured, because forcing a
   real timeout would have meant editing the shadow timeout.
2. **Duplicate calendar event / duplicate device file on retry.** Only the policy decision
   and the re-entry count were measured (`create_event`, `send_to_device` are retry-eligible
   and were re-entered). A live CalDAV/phone target was not available.
3. **`run_allowlisted_command` re-running a non-idempotent binary.** Shipped
   `allowed_commands` (`config.py:96-116`) are read-only, and `powerful_commands` is empty
   by default (`:117`), so the double-apply was demonstrated with `sleep` + a shortened
   injected timeout, not with a real `pip`/`npm` mutation.
4. **Reachability of R-6 in the shipped configuration.** `ALTERNATIVES` currently maps only
   risk-0 tools, so no shipped config path reaches an unauthorised destructive fallback;
   the missing authorisation check was proven by runtime injection of one dict entry.
5. **Whether the user's real `~/.jarvis-forge` already contains `failures` rows** produced by
   the P0. Not inspected (read-only mandate; the real home was never opened).
6. Voice/watch/dashboard rendering of `recovery_notes` beyond `reply` was not traced; only
   `api/app.py:537-546` (`verified`, `summary`) and `orchestrator._compose_reply` were read.
7. `TaskManager.retry`'s dashboard wiring (whether the UI polls `/tasks` and shows `running`)
   was not exercised.

---

## 10. Order in which these should be fixed

1. Land the `Verification` import (`tools/base.py:16`) — without it every measurement above
   is of a system whose tools never run, and R-1 is live today.
2. Make recovery and the orchestrator read `verification`, not `ok`: gate
   `recovery.py:169,209` on `retry_result.verification is Verification.SUCCESS`, import
   `Verification` in `recovery.py:28`, and switch `orchestrator.py:133` to
   `result.audit_result` (R-4, R-5b).
3. Move the never-retry line to "non-idempotent", not "risk>=3": require an explicit
   `idempotent: bool` on `ToolSpec` and default it to `False` for every write/motion tag
   (R-2); treat `timed out` as *outcome unknown*, therefore never auto-retried.
4. Delete or repair `ALTERNATIVES`: if the stage stays, it must call
   `self.policy.evaluate(alt_action, …)` before executing and must carry a
   `goal_preserved: bool` that the response text has to state (R-6, R-10).
5. Remove the `result.verified = True` overwrite in `agents/laptop.py:38-40` and
   `laptop_center.py:41-43`, or set `validate_assignment=True` on `ActionResult`
   (R-7, R-8).
6. Make `ok()` fail closed: no `verified=`/`verification=` argument ⇒
   `Verification.UNKNOWN` (R-5), then work the 155 sites up deliberately.
7. Implement the "ask user" stage — set `needs_confirmation`/emit a question when
   `strategy == "escalated"` — and always surface `tried`/`attempts` in the reply when
   recovery ran (R-3, R-9).
8. Run the recovery chain (at minimum `record_failure`) inside `resolve_confirmation` and
   `SchedulerRunner.run_job`, so gated and scheduled failures reach the `failures` table
   (R-12).
9. Either make `tasks.retry()` re-dispatch the job through the permission engine or remove
   the endpoint, and stop it erasing `detail` (R-11).
10. Add registry-driven tests: for every `ToolSpec` in `build_default_registry()` assert
    `is_retryable(...) is False` unless the spec declares itself idempotent, and add a
    recovery test whose retry result is `ok=True, verified=False` asserting
    `recovered is False` (R-8).
