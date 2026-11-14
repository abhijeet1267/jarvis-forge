# Phase 14 — Autonomous Workflows, Truthfully Reachable

Report date: 2026-09-26. Continues Phases 9-13. Written from measurements taken
against the repository as it stood, not from prior documentation.

---

## 1. Scope

`JARVIS_Forge_Full_Working_Plan.md:2117` defines this milestone as the goal loop:

```
Goal -> Plan -> Execute -> Observe -> Verify -> Recover -> Report
```

No written Phase 14 brief was supplied; the objective was taken from the project's
own plan and executed under the standing Phase 9-13 discipline: prove the next
thing through the real user-facing path, render server truth, add no second
authority layer, no LLM, no new execution path.

Explicitly out of scope, as briefed by earlier phases and unchanged here: the
read-file route, notification tool, memory-deletion redesign, hardware, desktop
bundle, RT-3/R-2/R-11-R-14, external credentials.

Two naming notes, so nothing is confused later:

- The audit-driven phase numbers in these briefs (9-14) are **not** the plan's own
  phase numbers (the plan's Phase 13 is Robotics, Phase 14 Autonomous Workflows).
- The plan's worked example - "Prepare my AMR Review 2", inspect a project, find
  the latest CAD, build a slide draft - was **not** attempted. It needs read and
  document-generation capabilities that do not exist, and the read route is not
  authorized. What was done instead is the level of autonomy the current system
  can actually support and be honest about.

---

## 2. What was actually there (measured first)

Four of the seven stages already existed and are proven by Phases 10-12: Execute,
Verify, Recover, Report. Two findings explained why the loop had never run:

**Finding 14-A - the multi-step loop was unreachable.** `RulePlanner._to_plan`
(`jarvis/core/planner.py:250` as it stands after this phase; `:203` when measured)
assigned `plan.actions = [PlannedAction(...)]` - one action, structurally, for all
38 routes. Measured across seven compound
phrasings ("create a file called a.txt containing: hi and then remember that
I created a.txt", "open VS Code and start a timer for 5 minutes", "prepare my AMR
Review 2", ...), the maximum action count produced was always 1. Consequently
the job loop in `handle()` (now `orchestrator.py:274-383`) - create job, update
each step, aggregate `done`/`failed`/`unverified` - was dead code from any typed
sentence. All line numbers in this section describe the state **before** the
changes in §3.

**Finding 14-B - and it was dishonest when reached.** A synthetic two-action plan
whose second step required confirmation produced:

```
JOB-57d8cb3a705f status= running
   step get_time    -> succeeded
   step write_file  -> pending
CONFIRMATIONS pending: ['pending']
```

`tasks.*` was written only inside `handle()`. The BLOCK, CONFIRM and no-agent
early returns never terminalized the job, and `resolve_confirmation()` never
touched it at all. So a goal that stopped read as `running` indefinitely, and
after the approval expired it stayed that way permanently with a step marked
`pending` even once the action had run and been verified. The two steps could
also not be addressed separately: `update_step` matched on step **name**, so a
plan naming the same tool twice moved both rows at once.

**Finding 14-C - invisible either way.** `dashboard/index.html` contained zero
references to `/tasks`. The backend exposed `GET /tasks` and
`POST /tasks/{job_id}/{action}` and nothing rendered them.

---

## 3. Changes made

### 3.1 A stated sequence becomes a plan (`jarvis/core/planner.py`)

`parse()` was split so the rule chain is reusable (`_first_intent`), and
`_composed()` was added. It splits only on explicit conjunctions:

```
_SEQUENCE = /\s+and\s+then\s+ | \s*,\s*and\s+then\s+ | \s*,\s+then\s+/i
```

Composition is **all-or-nothing**: every clause must independently match an
existing tool-backed route, otherwise the sentence falls back to unchanged
single-route parsing. This adds no new capability - it joins routes that already
existed - and each step still passes through `PermissionEngine` on its own.

A bare "then" is deliberately excluded. The first version included it and
measured this:

```
Create a file called note.txt containing: first line then second line
```

split into two clauses, which both truncated the content the user asked for and
invented a step. The trap is pinned by
`test_sequence_marker_inside_content_stays_content`.

### 3.2 Step state becomes truthful (`jarvis/core/tasks.py`, `db/database.py`, `orchestrator.py`)

- `update_step` takes `seq` so duplicate step names cannot move each other.
- New `attach_confirmation` / `step_for_confirmation` / `step_statuses` /
  `refresh_status`, backed by one nullable column, `task_steps.confirmation_id`,
  added through the existing additive migration path and to the fresh-schema DDL.
  The correlation has to live in the database because the approval arrives in a
  later, unrelated request.
- `job_status_for()` is the single aggregation rule. A plan whose sequence
  stopped before its last step is `incomplete`, never `done`.
- Every early return in `handle()` now terminalizes the job, and
  `resolve_confirmation()` settles the step it unblocked via `_settle_step()`.

### 3.3 One approval buys one action

Approving a held step executes exactly that step. It does not resume the
sequence: steps behind it were never offered to the user, so continuing would be
JARVIS granting itself authority it was not given. The reply says so - _"1 later
step(s) in that sequence have not run; ask me to continue when you want them"_ -
and the job stays `incomplete` with those steps `pending`.

### 3.4 Presentation (`jarvis/api/app.py`, `dashboard/index.html`)

- `CommandResponse.job_id` (a correlation id, no verdict) and `GET /tasks/{job_id}`.
- The page gained `STEP_STATE`, `JOB_STATE`, `stateTone()` and `renderWorkflow()`,
  reading the job back from the backend after `/command` and after `/confirm`.
  No state is inferred client-side; the CSS reuses the existing chip tones.

---

## 4. State mapping

| Backend condition                     | Step/job state shown                  | Evidence                                                                   |
| ------------------------------------- | ------------------------------------- | -------------------------------------------------------------------------- |
| step `verification === "success"`     | `VERIFIED`                            | `_action_status()` keys on verification, not `.ok` (`orchestrator.py:139`) |
| approval pending on this step         | `AWAITING CONFIRMATION`               | live: job `awaiting_confirmation`, step 1 `awaiting_confirmation`          |
| refused by policy                     | `BLOCKED`                             | live engine-level probe; job aggregates to `failed`                        |
| handler answered, effect not observed | `UNKNOWN`                             | `ActionStatus.UNVERIFIED` -> `UNKNOWN`                                     |
| step never reached                    | `NOT RUN`, job `WORKFLOW PARTLY DONE` | live: `[(0,succeeded),(1,succeeded),(2,pending)]`, job `incomplete`        |
| all steps verified                    | job `WORKFLOW COMPLETE`               | live: two-step goal, job `done`                                            |

`pipelineState()` from Phase 13 is unchanged: `VERIFIED` still comes only from
`actions[].verification === "success"`.

---

## 5. Real end-to-end test (live HTTP, dashboard bootstrap path)

Server: real uvicorn on `127.0.0.1:8899`, `JARVIS_HOME=/tmp/p14home` (scratch),
`file_workspace` at its real default `~/JARVIS_REAL_TEST`.

```
1. GET  /dashboard                      -> 200, single-use bootstrap (43 chars)
2. POST /auth/session                   -> 200, session token
3. POST /command  "what is the time and then create a file called
   phase14_workflow.txt containing: JARVIS PHASE 14 WORKFLOW VERIFIED"
      -> 200 | intent=workflow | job_id=JOB-b315404865cc
         needs_confirmation=true | actions returned: 1
4. GET  /tasks/JOB-b315404865cc
      -> status=awaiting_confirmation
         seq 0 get_time   -> succeeded  "It is 18:34 on Saturday, 26 September 2026."
         seq 1 write_file -> awaiting_confirmation  "waiting for your approval"
   file on disk BEFORE approval: False
5. POST /confirm/CONF-...?approve=true
      -> "Created phase14_workflow.txt (34 bytes), read it back and it matches."
6. GET  /tasks/JOB-b315404865cc
      -> status=done
         seq 0 get_time   -> succeeded
         seq 1 write_file -> succeeded
   file on disk AFTER approval: True
```

Step 1 genuinely executed and step 2 genuinely waited. Neither state was
inferred from the HTTP status code.

---

## 6. Independent filesystem verification (outside JARVIS)

```
$ ls -l ~/JARVIS_REAL_TEST/phase14_workflow.txt
-rw-r--r--@ 1 abhijeetmiskin staff 34 Sep 26 18:34 phase14_workflow.txt
$ cat
JARVIS PHASE 14 WORKFLOW VERIFIED
$ wc -c
34
$ shasum -a 256 phase14_workflow.txt
b8ee544df15e976ad0f878e251fcb3824cccd3772b4e75008b6497207219a549
$ printf 'JARVIS PHASE 14 WORKFLOW VERIFIED\n' | shasum -a 256
b8ee544df15e976ad0f878e251fcb3824cccd3772b4e75008b6497207219a549
```

The file and an independently computed hash of the expected bytes agree, which is
the evidence behind the `VERIFIED` step state - not `result.ok`, not HTTP 200.

---

## 7. Negative tests (live, over the same HTTP path)

| Case                                                                                            | Result                                                                                                                           |
| ----------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| Traversal as step 2 (`create a file called ../phase14_escape.txt containing: SHOULD NOT EXIST`) | composition refused (that clause yields a refusal, not a tool), fell back; `job_id` empty; `~/phase14_escape.txt` does not exist |
| Workflow halted and never approved                                                              | job `awaiting_confirmation`, steps `[(0,succeeded),(1,awaiting_confirmation)]`, file absent                                      |
| Anonymous `POST /command`                                                                       | 401                                                                                                                              |
| Anonymous `GET /tasks`, `GET /tasks/{id}`                                                       | 401, 401                                                                                                                         |
| Wrong token                                                                                     | 401                                                                                                                              |
| Caller-supplied `job_id` in the request body                                                    | ignored; response `job_id` is `""` - a client cannot point the page at someone else's job                                        |
| Protected absolute path as a later step                                                         | engine-level probe only (the route cannot produce it): step `blocked`, job `failed`, `/etc/phase14-hostile.conf` not created     |

**Finding 14-D (carried forward, see §12).** In the traversal case the user was
told the time and never told the second half of the sentence was refused. Nothing
unsafe happened, but the reply is incomplete. This is pre-existing behaviour for
partly-unroutable sentences; making composition all-or-nothing kept it from
getting worse and did not fix it.

---

## 8. Simulation / unavailability

- SIMULATED state: unchanged from Phase 13 - rendered from
  `actions[].output.simulated`, and the known backend overstatement (a simulated
  actuation recording `verification=success`) is **still carried forward**.
- UNAVAILABLE: unchanged from Phase 13 - rendered from `output.provider_status`.
- Multi-step over simulated hardware: **NOT TESTED**. No composed route reaches a
  simulated actuator in a way that would add evidence, and Phase 9-12 hardware
  remains blocked. No fake hardware was created to exercise a state.
- Workflow autonomy while unattended (scheduler-driven): **NOT PART OF THIS
  PHASE**. `SchedulerRunner.run_job` already re-evaluates permissions per run and
  its `executed`/`unverified` split was fixed in Phase 12; it runs one action per
  job, so Phase 14's multi-step work does not change it.

---

## 9. `.ok` authority sweep (repeat of Phase 12 §17, focused on the new path)

| Site                                 | Purpose                               | Authority class                        | Verdict                                                                                                             |
| ------------------------------------ | ------------------------------------- | -------------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| `orchestrator.py:215`                | audit `detail.ok`                     | audit detail, alongside `verification` | safe, unchanged                                                                                                     |
| `orchestrator.py:356`                | `if not result.ok:` triggers recovery | recovery                               | safe: success inside recovery is gated on `verification` (Phase 10)                                                 |
| `orchestrator.py:523`                | confirmed-action reply wording        | presentation                           | safe, unchanged                                                                                                     |
| `orchestrator.py:537`                | audit `detail`                        | audit detail                           | safe: authoritative field is `_audit_token(result)`                                                                 |
| `orchestrator.py:622`                | `_compose_reply` line per action      | presentation                           | safe; **note**: an unverified step can still read positively in prose while the chip says UNKNOWN - carried forward |
| `scheduler/runner.py:128-137`        | executed / unverified / failed        | scheduling                             | fixed in Phase 12, still keyed on `Verification.SUCCESS` first                                                      |
| `dashboard/index.html` `res.ok`      | HTTP status gates parsing             | presentation                           | safe: the failure branch renders UNKNOWN, never VERIFIED                                                            |
| `dashboard/index.html:633` `data.ok` | /voice/speak card                     | presentation                           | safe                                                                                                                |

New authority introduced by Phase 14: `job_status_for()`, keyed on step statuses
that `ActionStatus` derives from `verification`. **No new `.ok`-derived success
site was added.** No existing `.ok` site was weakened.

---

## 10. Regression

`tests/test_phase14_workflows.py` - 25 new tests, added alongside existing ones,
none modified or removed. They cover: composition and its two refusals, argument
fidelity per step, the halted-job fix, the one-approval rule, the denied path,
the blocked path, the aggregation table, duplicate step names, `job_id`
correlation over the API, 404, anonymous refusal, forged `job_id`, and the
page's own state tables executed out of `dashboard/index.html` by node.

The planner override in the engine-level blocked test is restored in `finally`;
the session-scoped `core` fixture makes that mandatory (the Phase 12 harness
defect) and the test names it.

Focused run: `test_phase14_workflows + test_phase13_dashboard_truth +
test_phase12_command_path + test_planner + test_scheduler + test_api` ->
**195 passed**.

Complete suite, two runs:

```
run 1: 1185 collected - 1181 passed, 0 failed, 1 skipped, 3 xfailed - 19.52s
run 2: 1185 collected - 1181 passed, 0 failed, 1 skipped, 3 xfailed - 19.01s
identical: yes (only wall-clock differs)
```

Previous baseline was 1156 passed / 1160 collected, so +25 tests and no change in
any pre-existing result.

---

## 11. Files modified

| File                              | Change                                                                                                                                     |
| --------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| `jarvis/core/planner.py`          | `_SEQUENCE`, `parse()` split, `_first_intent()`, `_composed()`                                                                             |
| `jarvis/core/orchestrator.py`     | job per-step `seq` updates, terminalize on every early return, `_settle_step()`, `response.job_id`                                         |
| `jarvis/core/tasks.py`            | `update_step(seq=)`, `attach_confirmation`, `step_for_confirmation`, `step_statuses`, `refresh_status`, `job_status_for`                   |
| `jarvis/core/models.py`           | `CommandResponse.job_id`; `Job.status` documented value list                                                                               |
| `jarvis/db/database.py`           | `task_steps.confirmation_id` in DDL and in the additive migration map                                                                      |
| `jarvis/api/app.py`               | `GET /tasks/{job_id}`                                                                                                                      |
| `dashboard/index.html`            | step CSS, `<ol id="steps">`, `STEP_STATE`, `JOB_STATE`, `stateTone()`, `renderWorkflow()`, wired into `send()` and `resolveConfirmation()` |
| `tests/test_phase14_workflows.py` | new, 25 tests                                                                                                                              |

No historical Phase 9-13 report was edited.

---

## 12. Remaining blockers (carried forward, none addressed here)

- **14-D** partly-unroutable sentence: the routable half is answered and the
  refused half is never mentioned. Needs a "clauses this plan did not cover"
  field on `Plan`/`CommandResponse`, not a renderer guess.
- **14-E** simulated actuations still record `verification=success` in the audit
  ledger; the UI masks it via `output.simulated` (Phase 13 finding, unchanged).
- **14-F** `_compose_reply` prose can read positively for an unverified step
  while the state chip says UNKNOWN.
- Route refusals at the planner surface as `UNKNOWN`/plain reply rather than a
  distinct `BLOCKED` state (Phase 13 carry-forward; still needs one backend field).
- No `read_file` route; no notification tool; `POST /memory/{id}/forget`
  overclaims; `_remember_outcome()` echo; `ProjectGraph` delete API; RT-3 mutable
  risk tier; R-2 broad auto-retry; R-11-R-14; desktop bundle exits 1; hardware
  integration (Phase 9-12: BLOCKED); external calendar/mail credentials; LLM
  planner.
- Multi-step **unattended** workflows (a composed goal driven by the scheduler)
  are not implemented: `ScheduledJob` carries one `action`.
- No resumption of a halted sequence after approval, by design (§3.3). Removing
  that would need a per-step authorization trail, not just a loop.

---

## 13. Browser verification

A real browser navigation to `http://127.0.0.1:8899/dashboard` **did** occur and
returned the served page (title: `JARVIS Forge — Control Dashboard`). No further
browser interaction was permitted: the attempt to type into the command field and
the attempt to read console messages were both refused by the runtime's policy
classifier.

**LIVE BROWSER DRIVE: NOT PERFORMED - navigation only; interaction blocked by
policy.** The end-to-end proof in §5 is HTTP over the same dashboard bootstrap
path, and the page's own state tables are executed from the shipped source by node
in §10's tests. No browser-based visual confirmation of the step list is claimed.

---

## 14. Git state

`git status --porcelain` lists every top-level entry as untracked (`jarvis/`,
`tests/`, `dashboard/`, `audit/`, `requirements.txt`, ...). `git rev-list --count
HEAD` fails with `unknown revision or path not in the working tree` on branch
`main`: the repository has **no commits**, so nothing in this phase - or in
Phases 10-13 - exists as a diff or a reviewable object.

- staged: no
- committed: no
- pushed: no
- No git write operation was performed in this phase.
