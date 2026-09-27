# Phase 16 — Complete command accountability

Report date: 2026-09-26 → 2026-09-27 (the work crossed midnight). Continues
Phases 9-15.

---

## 1. Scope, and one thing about the brief

The mission: every user command must have a truthful, auditable terminal outcome,
including the branches where execution never happens. The brief's pipeline names
AUTHENTICATION → PLANNER → ROUTE/INTENT DECISION → PERMISSION → CONFIRMATION →
EXECUTION → VERIFICATION → AUDIT → RESPONSE, and requires an appropriate record at
each terminal branch.

**The brief arrived truncated.** It ends mid-rule in §2 HARD RULE:

> "Do NOT label such"

Everything after §2's opening sentence is missing too. I asked once and was told to
continue, so the rule was interpreted in the only safe direction available: a
decision that never executed must **not** be recorded with an execution-shaped
token. No new token in this phase reuses `success`, `failure`, `unverified` or
`recovered`, and the property is pinned by a test that was proven to discriminate
(§6). If the missing sentence meant something narrower or different, the naming is
the one thing to revisit — everything else here is mechanical.

No new capability, no LLM, no visual redesign, no architecture change.

---

## 2. What was actually missing (measured, not assumed)

`handle()` has four no-execution returns. Two of them audited and two did not:

| Branch | Line | Before |
| --- | --- | --- |
| guard refusal on raw phrasing | `orchestrator.py:227` | audited `blocked` |
| `plan.direct_reply` — route-level refusal, "I don't have that capability", unrecognised goal | `:256` | **no row at all** |
| `plan.needs_clarification or not plan.actions` | `:271` | **no row at all** |
| per-action BLOCK / CONFIRM | `:282`, `:296` | audited `blocked` / `awaiting_confirmation` |

Phase 15 measured the size of the hole over the plan's own acceptance list: **5 of
18 commands reached a decision and left no trace.** A refused command was therefore
less accountable than a command the safety guard rejected.

---

## 3. Changes

### 3.1 The two silent branches now record a decision

```python
# plan.direct_reply
self.audit.record(intent=request.text, result="no_action", risk="none",
                  device=..., session_id=..., request_id=response.request_id,
                  detail={"stage": "planner", "reply": response.reply[:200]})

# plan.needs_clarification or not plan.actions
self.audit.record(..., result="clarification", risk="none", ...,
                  detail={"stage": "planner", "question": response.reply[:200]})
```

Chosen against the ledger's existing grain (`success · failure · unverified ·
blocked · awaiting_confirmation · denied · expired · recovered`) rather than a new
taxonomy. `tool` stays empty — a refusal must not name a tool that never ran.
`risk="none"` states that no risk was taken. The user's own sentence is recorded,
which the guard branch already did, so this adds no new class of exposure, and
`redact_audit` still runs before persistence.

`.ok` was not touched anywhere in this phase.

### 3.2 16-B — the ledger's `request_id` column was wrong or empty almost everywhere

Found while proving the fix, not looking for it. `_audit_action` wrote
`request_id=request.session_id`, so **every executed action's row held a `SESS-`
value in the request column** — 13 of the 18 acceptance rows — while five other
`audit.record` sites (guard, recovered, expired, denied, pre-execute-blocked,
confirmed-executed) wrote nothing at all. The user is answered with a `REQ-` id, so
the ledger could not be joined to the reply it described, for the majority of rows.

Fixed by passing `response.request_id` down: `_audit_action` gained a `request_id`
parameter, its two call sites hand it down, and seven record sites now carry it
consistently. `session_id` still holds the session id, which is what it always
meant. No API or test consumer relied on the old (wrong) value.

---

## 4. 16-A — the clarification branch is unreachable from the shipped planner

Probing it for a test produced a measurement, not a green path: for **all ten**
no-tool sentences tried (`compress this`, `delete`, `copy`, `move this`,
`transfer`, `design this system`, `what is my car`, `remember`, `schedule`, `find`),
every route that yields no tool also yields a note, and `_to_plan` maps a note to
`direct_reply`. `needs_clarification` stays `False`.

So `clarification` is an audit token and `NEEDS DETAIL` a dashboard state for a
branch the rules planner cannot reach — they exist for a different planner. That is
correct behaviour and honest labelling, but Phase 15's report implied two live
states where there is one, so it now carries an **additive correction**
(`PHASE15_ACCEPTANCE_SWEEP_REPORT.md` §16) rather than a silent edit. The branch is
still audited, proven by an engine-level probe that constructs the Plan directly
and restores `core.planner.parse` in `finally`.

---

## 5. 16-C — a pre-existing test-harness leak, found by accident

Running modules in a hand-picked order made
`test_api.py::test_health_reports_each_peripheral_honestly` fail on
`peripherals.car.checked is False` ("health must not probe the car"). Cause:
`tests/test_pipeline.py::test_car_reports_disconnected_honestly` sends "check my car
fuel" through the pipeline, which **builds the process-wide `CarGateway` singleton**
and leaves it marked as probed, with nothing restoring it. **This predates Phases
14-16** — the canonical alphabetical run simply happens to execute `test_api` first,
so the contamination was invisible rather than absent. Exactly the shared-state class
the Phase 12 harness bug was.

Fixed at the source with a `finally` reset, and the same leak in my own Phase 15
car test was closed with a `car_offline` fixture that resets in both directions.

Three measurement-artifact notes, recorded rather than quietly corrected (§6 and
§7 of the Phase 15 report): Phase 15's `after[:delta or 1]` printed the previous
command's audit row for zero-delta commands; this phase's proof script first
reported `open VS Code` as unaudited because `MAX(rowid)` is `NULL` on an empty
table and `rowid > NULL` matches nothing.

---

## 6. TDD record — what failed first, and what did not

Written test-first; no production code was written before its test failed, except
where a test is explicitly labelled a pin.

Red → green, for the right reason:

| Test | First failure |
| --- | --- |
| `test_route_level_refusal_is_audited` | `assert (0 - 0) == 1` — no record |
| `test_clarification_branch_is_audited` | `assert (1 - 1) == 1` — no record |
| `test_executed_row_is_linked_to_the_response` | `'' == 'REQ-…'` / SESS- value |
| `test_guard_row_is_linked_to_the_response` | `'' == 'REQ-3d636462fe5e'` |
| `test_confirmation_row_is_linked_to_the_response` | `'' == 'REQ-5f3ad55ffdc8'` |

Pins that passed immediately, and how they were shown to discriminate: two of my
first-cycle tests already constrained the token, so rather than claim credit I ran a
mutation check — flipping the new record's `result` from `no_action` to `success`
made **5 tests fail** (`test_decision_rows_never_claim_an_execution_outcome` across
five commands), then was reverted and the file returned to green. The hash-chain and
no-double-audit tests are likewise pins over behaviour `AuditLog` already had, kept
because Phase 16 is precisely the phase that could have broken them.

---

## 7. Live proof over the real HTTP path

Fresh server, fresh scratch home, all eighteen acceptance commands on one session via
the dashboard bootstrap path, ledger read directly from `action_logs` by rowid
delta:

```
commands: 18   audit rows: 18   commands leaving no record: 0
rows without a hash: 0
newest row links to its predecessor: True
distinct result tokens in the ledger: ['failure', 'no_action', 'success']
request_id mismatched: NONE
chain verify: {'ok': True, 'checked': 36}     (on the earlier two-pass ledger)
```

| Command | Intent | verification | rows | result | links to response |
| --- | --- | --- | --- | --- | --- |
| open VS Code | open_app | success | 1 | success | yes |
| find my latest project report | search_files | success | 1 | success | yes |
| show me my laptop battery | laptop_battery | success | 1 | success | yes |
| transfer this file to my phone | transfer | — | 1 | **no_action** | yes |
| what is my phone battery | phone_battery | — | 1 | **no_action** | yes |
| plan my day | daily_schedule | success | 1 | success | yes |
| schedule two hours for project work tomorrow | schedule_task | — | 1 | **no_action** | yes |
| draft an email to my teammate | draft_email | failed | 1 | failure | yes |
| send it | send_email | — | 1 | **no_action** | yes |
| remember that this is my AMR project | remember | success | 1 | success | yes |
| what did I work on yesterday | recall | success | 1 | success | yes |
| design this system | unknown | — | 1 | **no_action** | yes |
| research this idea | search_papers | failed | 1 | failure | yes |
| check my car fuel | car_telemetry | failed | 1 | failure | yes |
| check my car charge | car_telemetry | failed | 1 | failure | yes |
| give me today's location summary | location_summary | failed | 1 | failure | yes |
| turn on study mode | home_routine | failed | 1 | failure | yes |
| stop all physical actions | emergency_stop | success | 1 | success | yes |

Every command's decision is now a hash-chained, predecessor-linked, response-joinable
record. The Phase 13/15 rule still holds: nothing that did not execute carries an
execution token.

---

## 8. 16-F — the AUTHENTICATION branch, deliberately left out of the ledger

§1's pipeline includes AUTHENTICATION as a terminal branch, so it was measured:

```
3 × anonymous POST /command -> 401, 401, 401
application log lines 'rejected unauthenticated request'   : 3
action_logs delta from those 3 requests                     : 0
```

Failed authentication **is** accounted for — logged, attributable by source, and
rate-limited — but it is not in the tamper-evident chain, and this phase did not
put it there. The reason is a real risk, not inertia: an unauthenticated caller
writing into the hash-chained ledger can grow it at up to the rate limit
(600/min ≈ 36 000 rows/hour) with attacker-controlled text in `intent`, which turns
an accountability record into a storage-exhaustion vector and poisons the very chain
it is meant to protect.

Two safe designs, if the user wants authentication in the ledger — pick one and it
is a small phase:
1. a bounded, non-user-text summary row per window (count + source, no raw text);
2. full rows only for authenticated-but-unauthorised attempts, with anonymous
   rejections kept in the application log as today.

This is the one §1 branch not closed, and it is closed-able only by a decision that
is the user's.

---

## 9. 16-D — still true, deliberately untouched

`plan.direct_reply` still sets `response.decision = PolicyDecision.EXECUTE` for a
command that executed nothing (`orchestrator.py:258`). The audit row now says
`no_action`, so the ledger is right, but the in-band response still calls it EXECUTE.
Changing it needs a `PolicyDecision` value that does not exist yet
(`ALLOW_NOTHING`? `NO_ACTION`?) — that is a vocabulary decision touching every
consumer of `decision`, including the dashboard's BLOCK check, and it did not belong
in a phase whose §2 was cut off. Recorded, not guessed.

---

## 10. Regressions

`tests/test_phase16_accountability.py` — **15 new tests**: the two branches audited
at all; the token never claiming execution (5 commands, mutation-proven);
`request_id` linkage for executed, guard, confirmation, denial and decision rows;
`session_id` preserved; refusal rows naming no tool; hash + predecessor link; the
branches that already audited still auditing **exactly once**; and the measured fact
that the rules planner never reaches the clarification branch.

```
Phase 16 alone            : 15 passed
wide regression (1218-test context: pipeline, api, phase12/15/16, security,
                verification-authority)      : 210 passed
complete suite, run 1     : 1222 collected - 1218 passed, 0 failed, 1 skipped,
                            3 xfailed - 20.48s
complete suite, run 2     : identical - 21.94s
baseline before this phase: 1219 collected / 1215 passed  →  +15 tests, nothing
                            pre-existing changed, nothing pre-existing edited
```

Two existing test files changed for correctness, not to make tests pass:
`tests/test_pipeline.py` (16-C leak) and `tests/test_phase15_acceptance.py` (same
leak in my own car test). No assertion was weakened or removed.

---

## 11. Files modified

| File | Change |
| --- | --- |
| `jarvis/core/orchestrator.py` | `no_action` and `clarification` audit records on the two silent branches; `_audit_action(request_id=…)`; `request_id=response.request_id` at 7 record sites |
| `tests/test_phase16_accountability.py` | new, 15 tests |
| `tests/test_pipeline.py` | car gateway reset in `finally` (16-C) |
| `tests/test_phase15_acceptance.py` | `car_offline` fixture resetting both ways |
| `audit/PHASE15_ACCEPTANCE_SWEEP_REPORT.md` | additive §16 correction (15-D closed; NEEDS DETAIL unreachable; harness artifacts) |
| `audit/PHASE16_ACCOUNTABILITY_REPORT.md` | this file |

## 12. Carried forward

New: **16-D** `decision=EXECUTE` on a non-executing reply · **16-F** authentication
failures not ledgered (pending the user's choice) · `no_action`/`clarification` are not yet mapped by the dashboard's audit card, which
colours anything that is not `success`/`blocked`/`denied` amber (`index.html:566`) —
it shows the exact token as text, so nothing is misrepresented, but a decision and a
failure look the same colour there.

Unchanged from earlier phases: simulated actuations still recorded as
`verification=success` (14-E) · **14-D** partly-unroutable sentence answers one half
and never mentions the refused half · **15-E** memory echo rows answer dated
questions · **15-F** arXiv 406 · **15-H** which calendar store was read · **15-J**
21.6 s full-filesystem search · no `read_file` route · no notification tool ·
`POST /memory/{id}/forget` overclaims · `ProjectGraph` delete API · RT-3 · R-2 ·
R-11-R-14 · desktop bundle exits 1 · hardware **BLOCKED** · external credentials ·
LLM planner · multi-step unattended workflows.

## 13. Browser

Not used. The proof is HTTP plus direct reads of `action_logs`, and the renderer's
rules are executed from the shipped file by node inside the tests. **LIVE BROWSER
DRIVE: NOT PERFORMED.**

## 14. Git state

Still branch `main` with **no commits**; `git rev-list --count HEAD` fails with
`unknown revision or path`; every top-level entry untracked. Nothing from Phases
10-16 is a diff or a reviewable object.

* staged: no · committed: no · pushed: no. No git write operation was performed.

## 15. Cleanup

Server stopped. Scratch homes `/tmp/p15home*`, `/tmp/p16home*` left for inspection.
`~/.jarvis-forge` untouched (isolated `JARVIS_HOME` throughout; its `api_token` still
dates from 2026-09-19). Proof scripts `/tmp/p15_sweep.py`, `/tmp/p16_proof.py` and
the raw JSON remain for re-running. VS Code was really launched twice by command 1 —
quit it if that is unwanted.
