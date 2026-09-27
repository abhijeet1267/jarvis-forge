# Phase 15 — The plan's own acceptance test, run for real

Report date: 2026-09-26. Continues Phases 9-14.

---

## 1. Scope, and how it was chosen

No Phase 15 brief was supplied, and **the project plan has no Phase 15**:
`JARVIS_Forge_Full_Working_Plan.md` ends at `## Phase 14 --- Autonomous Workflows`
and then switches to numbered sections, finishing at `# 60. Immediate Next Step`.
The plan does, however, name the next milestone twice in a way that is directly
testable:

* `# 57. Final Build Order` — item 25 is *Autonomous workflows* (Phase 14), item 26
  is *Advanced recovery*.
* `# 59. First Commands to Implement` — eighteen sentences, with the instruction:
  *"Implement these first and use them as the acceptance test."*
* `# 58. Definition of Done` — the 26 properties those commands are meant to
  exercise.

Phase 15 was therefore taken to be: **stop asserting, and run the acceptance test
against the real pipeline.** That is measurement rather than feature work, which is
also the right posture while `Advanced recovery` (the literal next build-order
item) has no brief and would otherwise be invented scope.

Two things were explicitly *not* done: no new capability was added, and no
carried-forward item from the do-not-expand list (read route, memory-deletion
redesign, hardware, desktop bundle, RT-3/R-2/R-11-R-14, LLM planner) was
implemented.

---

## 2. Method

A real uvicorn server on `127.0.0.1:8899` with `JARVIS_HOME=/tmp/p15home` (scratch;
`~/.jarvis-forge` was never written — its `api_token` still dates from 2026-09-19).
All eighteen commands were sent in plan order over `POST /command`, on **one
session id**, through the dashboard's own bootstrap → `POST /auth/session` → session
token path, so conversational context was exercised rather than assumed. For each
command: HTTP status, `intent`, `decision`, `needs_confirmation`, per-action
`verification`, `output.simulated`, `output.provider_status`, the reply, and the
audit-ledger delta. Harness: `/tmp/p15_sweep.py`, raw data
`/tmp/p15-sweep.json` and `/tmp/p15-sweep-after.json`.

Side effects were checked **outside** JARVIS: `pgrep` for the launched application,
`pmset -g batt` for the battery figure, `GET /memory/search` for the stored fact.

---

## 3. What the eighteen commands actually did (before the fixes)

| # | Command | Routed to | verification | Reply (abridged) |
| --- | --- | --- | --- | --- |
| 1 | open VS Code | `open_app` | success | "Opening Visual Studio Code…" — **process confirmed running** |
| 2 | find my latest project report | `search_files` | success | "could not find any file matching 'project report.' under /Users/…" (21.6 s) |
| 3 | show me my laptop battery | `battery_status` | success | "100% (fully charged)" — `pmset` agrees |
| 4 | transfer this file to my phone | — | — | refuses to guess the file (correct) |
| 5 | what is my phone battery | — | — | "no phone is paired with this laptop yet" (correct) |
| 6 | plan my day | `daily_schedule` | success | "Your today is clear" · `provider_status=real_empty` |
| 7 | **schedule two hours for project work tomorrow** | **`list_schedules`** | **success** | **"You have nothing scheduled."** |
| 8 | draft an email to my teammate | `draft_email` | failed | "I do not have a contact called 'teammate'" (correct) |
| 9 | send it | — | — | "There is no draft waiting to send" (correct: #8 failed) |
| 10 | remember that this is my AMR project | `remember_fact` | success | `MEM-ec646dbc4b12` retrievable via `/memory/search` |
| 11 | what did I work on yesterday | `search_memory` | success | answered with a **same-session echo row** |
| 12 | design this system | `unknown` | — | honest fallback prompt |
| 13 | research this idea | `search_papers` | failed | "arXiv could not be reached: HTTP 406" |
| 14 | check my car fuel | `get_car_telemetry` | **success** | **"I could not read those car values…"** |
| 15 | check my car charge | `get_car_telemetry` | **success** | same |
| 16 | give me today's location summary | `get_location_summary` | **success** | **"not available: location tracking is disabled"** |
| 17 | turn on study mode | `run_home_routine` | **success** | **"gateway is offline… Nothing was changed."** |
| 18 | stop all physical actions | `emergency_stop` | success | honest: physical actuators NOT stopped (Phase 10) |

**Six commands reported success for something that did not happen.** Five are the
same shape — a reply that refuses, negates or states "nothing was changed", sitting
on `verification=success`, audited as `success`, and rendered by the dashboard's
own rules as a **VERIFIED** chip. Command 7 is worse than cosmetic: a request to
*create* was answered by *listing*, so the user was told the schedule list was empty
and the state was VERIFIED, and no schedule exists.

---

## 4. Fixes

### 15-A — a read that read nothing is not verified
`jarvis/tools/car.py`, `jarvis/tools/system.py`, `jarvis/tools/home.py`.

Each of the three refusing branches now passes `verification=Verification.FAILED`
plus `provider_status="unavailable"`, following the Phase 11 provider-status
convention rather than inventing a second vocabulary.

`.ok` was deliberately **left true**: the handler did complete, and did report
honestly. Two distinctions had to stay separate — which is what the whole Phase 9
thread was about — and keeping `.ok` true also kept every existing assertion about
"the call worked" valid: **no existing test was modified in this phase.**

It also avoids a second problem. `get_car_telemetry` and `get_location_summary` are
in `IDEMPOTENT_TOOLS` (`core/recovery.py:52`), so switching them to `fail()` would
have made recovery retry an unreachable car on every command. That is R-2's
territory and was not touched.

### 15-B — "schedule X" is a creation request
`jarvis/core/planner.py`. The word *schedule* alone used to satisfy the
manage-existing-schedules guard, so any sentence beginning `schedule …` was turned
into `list_schedules`. A `create_requested` test now routes imperative
"schedule <something>" to the create branch, which asks *when* rather than
inventing a job. Two deliberate limits:

* Listing, cancelling, pausing and the existing `every …` / `remind me …`
  creation phrasings route exactly as before (parametrised test 15-B guards it).
* An imperative-schedule sentence that reaches a time but has no stated content is
  **not** filed as a bare reminder named `reminder` — it asks what the time is for.
  That pre-existing nameless-reminder path is left alone for sentences that always
  reached it (`every day at 9`), because this phase did not measure it.

### 15-C — an honest refusal is not UNKNOWN either
`dashboard/index.html`. Five of the eighteen commands produced a correct, explicit
answer and rendered **UNKNOWN**, which told the user the system was unsure when it
had actually said *no*. `pipelineState()` now reads two fields the backend already
sends — `plan.needs_clarification` → **NEEDS DETAIL**, `plan.direct_reply` →
**NOTHING EXECUTED**. No new backend field, no inference from reply wording: a
refusal and a clarifying question both arrive as `direct_reply`, so both get the
conservative shared label and the reason is pinned in a test
(`test_a_question_is_not_guessed_at_from_reply_wording`).

---

## 5. After the fixes — the same eighteen commands re-run

```
same    open VS Code                        open_app         success -> success
same    find my latest project report       search_files     success -> success
same    show me my laptop battery           battery_status   success -> success
same    transfer this file to my phone      transfer         -       -> -
same    what is my phone battery            phone_battery    -       -> -
same    plan my day                         daily_schedule   success -> success
CHANGED schedule two hours for project wor  list_schedules   success -> schedule_task   -
same    draft an email to my teammate       draft_email      failed  -> failed
same    send it                             send_email       -       -> -
same    remember that this is my AMR proje  remember         success -> success
same    what did I work on yesterday        recall           success -> success
same    design this system                  unknown          -       -> -
same    research this idea                  search_papers    failed  -> failed
CHANGED check my car fuel                   car_telemetry    success -> failed
CHANGED check my car charge                 car_telemetry    success -> failed
CHANGED give me today's location summary    location_summary success -> failed
CHANGED turn on study mode                  home_routine     success -> failed
same    stop all physical actions           emergency_stop   success -> success
```

Audit ledger, measured per command afterwards: the car read now writes
`result='failure'`. Commands 7 re-runs to a clarification with `tools=[]` and
`verification` empty, so nothing can be read as executed.

## 6. Measurement error in my own harness, recorded

The sweep table's `aud=` column for commands with a **zero** audit delta was wrong.
The script did `after[: delta or 1]`, so for the five no-action commands
(4, 5, 9, 12, and 7 after the fix) it printed the *previous* command's audit row.
Verified separately: a planner-level refusal or clarification adds **0** rows.
The JSON files still carry the artifact; the numbers in §5 and the ledger claim in
§7 come from the direct re-measurement, not from that column.

---

## 7. New finding, not fixed — route-level refusals are not in the ledger

The guard-block path and the confirmation path both `audit.record(...)`. The
planner's own refusal and clarification paths (`orchestrator.py:229-243`,
`direct_reply` and `needs_clarification`) return **without any audit entry**, so a
user who asks JARVIS to delete something and is refused leaves no tamper-evident
trace — while a refusal from the safety guard does. Measured: 0 rows added for all
five no-action commands. `messages` does hold the assistant reply, so it is
recoverable, but it is outside the hash chain. Recorded as **15-D**, not repaired:
adding an audit line to those returns is a one-line change with a real consequence
(the chain records user prompts), so it deserves its own phase and its own tests.

---

## 8. Other findings

| ID | Finding | Evidence | Action |
| --- | --- | --- | --- |
| 15-E | `_remember_outcome()` writes an `"User asked: … Result: …"` echo per command, and `search_memory` can surface an echo as memory. Answering *"what did I work on yesterday"* with an undated same-session echo is a wrong answer to a dated question. | 13 echo rows in the sweep home; run 1's #11 reply | **not fixed** — on the do-not-expand list |
| 15-F | `search_papers` gets HTTP 406 from arXiv, so the *web research* half of DoD #15 does not work. Likely a request-header issue, not an outage. | reply text, twice | not fixed — outside this phase's truth-state scope, recorded |
| 15-G | `search_files` keeps the sentence's terminal period in the query (`'project report.'`). Tested both spellings against a directory containing `project report.txt`: **both match**, so this is cosmetic reply wording, not a defect. | in-process probe | nothing to fix |
| 15-H | `daily_schedule` reads JARVIS's local calendar when no account is configured, and the reply ("Your today is clear") does not say which store it read. `real_empty` is accurate *of that store*. | `provider_status=real_empty`, `calendar_provider.py:779` | recorded, Phase 11's decision stands |
| 15-I | "open VS Code" executed with no confirmation. It is a risk-tier choice, not a truth defect, and Phase 11 opened that door deliberately. | ledger row | recorded |
| 15-J | A full-filesystem `search_files` took 21.6 s of the sweep's 22.6 s total. | `ms` column | recorded |

---

## 9. Definition of Done (§58), by evidence

| DoD item | State | Basis |
| --- | --- | --- |
| natural voice commands | NOT TESTED this phase | no microphone in this environment; not fabricated |
| conversational context | PARTIAL | one session used; referential commands ("this file", "send it") resolve by *policy*, not from prior turns |
| select correct agent | WORKS | 18/18 routed to a real owner |
| select correct device | WORKS (narrow) | everything ran on `laptop_main`; other devices reported unreachable honestly |
| structured tools | WORKS | 1207 collected tests |
| confirmation when appropriate | WORKS | Phase 12/14 proof; none of the 18 needed it |
| execute laptop tasks | **WORKS, verified outside JARVIS** | VS Code process running; battery 100% matches `pmset` |
| communicate with a phone | UNAVAILABLE | no device paired |
| smartwatch as interface | NOT TESTED this phase | Phase 10 authority work |
| control approved home devices | UNAVAILABLE (now recorded truthfully) | gateway offline → `failure` |
| read car telemetry | UNAVAILABLE (now recorded truthfully) | no adapter → `failure` |
| access calendar | PARTIAL | local store only, no account configured |
| draft/send email with confirmation | PARTIAL | no contact/credentials configured; both refused honestly |
| location summaries | **NOT IMPLEMENTED** | no location subsystem exists; the tool is a stub, now reporting unavailable |
| search web and project files | PARTIAL | files yes; arXiv 406 (15-F) |
| design engineering projects | NOT ROUTED | "design this system" → `unknown` |
| maintain project memory | WORKS | stored fact retrieved over HTTP |
| schedule future tasks | PARTIAL (was falsely succeeding) | asks *when* now; cadence-only support |
| run multi-step workflows | WORKS | Phase 14 |
| recover from failures | PASS (prior phase) | Phase 10 |
| verify action completion | **WORKS for these five sites after 15-A** | ledger `failure` re-measured |
| log every important action | PARTIAL | route-level refusals unaudited (15-D) |
| work partially offline | WORKS | 12 of 18 commands answered with no network |
| stop physical operations safely | WORKS WITH LIMITS | honest: physical stop unavailable (Phase 10) |
| protect private data | PASS (prior phase) | 401s observed again in this run's server log |
| revoke permissions | PASS (prior phase) | Phase 10 |

---

## 10. Regressions

`tests/test_phase15_acceptance.py` — 22 new tests, no existing test modified or
removed. They cover: the three offline-read tools (`ok` true, verification failed,
provider status unavailable), the same three through the real pipeline with the
shipped renderer proving **not VERIFIED** and specifically **UNAVAILABLE**, the
audit ledger no longer recording `success` for them, command 7's routing and its
"nothing filed" consequence (counted as a **delta** — the session-scoped `core`
fixture means an absolute count is somebody else's job), the four schedule
readings that must *not* move, the nameless-reminder guard, four refusal/clarification
renderings, the deliberate refusal to guess between question and refusal, the
Phase 13 invariants re-checked, and the shipped page containing the new states.

Two of these tests were wrong before they were right and are recorded here rather
than quietly fixed: `NEEDS DETAIL` was asserted for command 7, which actually
arrives as `direct_reply` (hence `NOTHING EXECUTED`, and now a test that the
renderer must not guess otherwise), and the absolute `scheduled_jobs == 0` count,
which passed alone and failed in the full suite for exactly the shared-fixture
reason above.

```
focused (Phase 15 + 14 + 13 + car/home/planner/scheduler/pipeline/recovery/
         provider-status/watch/robotics): 399 passed, then 22/22 for Phase 15
complete suite, run 1: 1207 collected - 1203 passed, 0 failed, 1 skipped, 3 xfailed - 34.55s
complete suite, run 2: 1207 collected - 1203 passed, 0 failed, 1 skipped, 3 xfailed - 22.64s
identical: yes (wall clock only)
```

Baseline before this phase: 1185 collected / 1181 passed. +22 tests, no pre-existing
result changed, and **no pre-existing test edited**.

---

## 11. Files modified

| File | Change |
| --- | --- |
| `jarvis/tools/car.py` | `Verification` import; offline no-readings branch now FAILED + `provider_status` |
| `jarvis/tools/system.py` | `Verification` import; `get_location_summary` now FAILED + `provider_status` |
| `jarvis/tools/home.py` | `Verification` import; offline routine now FAILED + `provider_status` |
| `jarvis/core/planner.py` | `create_requested` guard on the manage-schedules branch, gate widened, nameless-reminder refusal |
| `dashboard/index.html` | `NEEDS DETAIL` / `NOTHING EXECUTED` states and their tones, read from `plan.needs_clarification` / `plan.direct_reply` |
| `tests/test_phase15_acceptance.py` | new, 22 tests |

`audit/PHASE14_*.md` and earlier reports were not edited.

---

## 12. Carried forward

New this phase: **15-D** route-level refusals unaudited · **15-E** memory echo rows
answer dated questions · **15-F** arXiv 406 · **15-H** which calendar store was read ·
**15-J** 21.6 s full-filesystem search.

Unchanged from earlier phases: simulated actuations recorded as `verification=success`
(14-E; the *UI* still masks it, and this phase did not fix the ledger) · route
refusals rendering without a distinct BLOCKED state · no `read_file` route · no
notification tool · `POST /memory/{id}/forget` overclaims · `ProjectGraph` delete API ·
RT-3 mutable risk tier · R-2 broad auto-retry · R-11-R-14 · desktop bundle exits 1 ·
hardware integration **BLOCKED** · external calendar/mail credentials · LLM planner ·
multi-step unattended workflows · **14-D** a partly-unroutable sentence still answers
one half and never mentions the refused half.

## 13. Browser

Not used in this phase: the acceptance path is HTTP, and the renderer's rules are
executed from the shipped file by node inside the tests. Live browser interaction
remains blocked by the runtime's policy classifier, as recorded in Phase 14.
**LIVE BROWSER DRIVE: NOT PERFORMED.**

## 14. Git state

`git status --porcelain` still lists every top-level entry as untracked;
`git rev-list --count HEAD` still fails with `unknown revision or path`, i.e.
branch `main` with **no commits**. Nothing in Phases 10-15 is a diff or a
reviewable object.

* staged: no · committed: no · pushed: no — no git write operation was performed.

## 15. Cleanup

Servers stopped. Scratch homes `/tmp/p15home`, `/tmp/p15home2` left for inspection
(delete at leisure); `~/.jarvis-forge` untouched. No file was written outside a
scratch directory or `~/JARVIS_REAL_TEST` in this phase. VS Code was really launched
by command 1 and was still running at the end of the phase — quit it manually if
that is unwanted.

---

## 16. Additive correction — found while implementing Phase 16 (2026-09-26)

Two statements in §4/§9 above were too generous, and the correction belongs here
rather than in a rewrite:

1. **`NEEDS DETAIL` is not reachable from the shipped planner.** §4 introduced it
   for `plan.needs_clarification`, and the mapping itself is correct, but measured
   over ten no-tool sentences (`compress this`, `delete`, `copy`, `transfer`,
   `design this system`, `what is my car`, `remember`, `schedule`, `find`, `move
   this`) **every** route that produces no tool also produces a note, and
   `_to_plan` turns a note into `plan.direct_reply`. So `needs_clarification` is
   `False` for all of them and every real no-execution answer renders
   `NOTHING EXECUTED`. `NEEDS DETAIL` is a correct mapping for a branch that the
   rules planner cannot currently reach. Pinned by
   `tests/test_phase16_accountability.py::test_rule_planner_never_reaches_the_clarification_branch`.
   No user-visible behaviour was wrong because of this — the conservative label was
   chosen deliberately — but §4 implied two live states where there is one.

2. **Finding 15-D is closed, and the §6 measurement error was not the only harness
   defect of that kind.** 15-D (route-level refusals add zero audit rows) was
   repaired in Phase 16: the acceptance list now produces 18 audit rows for 18
   commands, measured against `action_logs` directly, and every command leaves a
   hash-chained, predecessor-linked record.

   While re-measuring it, the same *class* of error surfaced twice more, both in
   test/harness code rather than in JARVIS:
   * `tests/test_pipeline.py::test_car_reports_disconnected_honestly` left the
     process-wide car gateway built and probed, which broke
     `test_api.py::test_health_reports_each_peripheral_honestly` ("health must not
     probe the car") whenever that order occurred. **Pre-existing**, invisible only
     because alphabetical order runs `test_api` first. Now reset in `finally`.
   * My own Phase 15 `test_car_telemetry_offline_is_not_verified` had the same
     leak. Now wrapped in a `car_offline` fixture that resets both ways.
   * The Phase 16 proof script initially reported one command as unaudited because
     `MAX(rowid)` is `NULL` on an empty table and `rowid > NULL` matches nothing —
     the ledger's first entry always looked like no entry. Corrected to
     `COALESCE(MAX(rowid), 0)`.
