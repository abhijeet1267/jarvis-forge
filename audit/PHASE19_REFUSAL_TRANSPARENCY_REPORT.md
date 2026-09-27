# Phase 19 — Refusal transparency

Report date: 2026-09-27. Continues Phases 9-18.

---

## 1. Scope

No brief. Two carried-forward findings are the same defect seen from two ends, and
they were taken together:

* **14-D** — a sentence whose second half could not be routed was answered with only
  the first half, and nothing said the rest was dropped.
* **17-A** — `no_action` covered four different facts (nothing understood, capability
  missing, route refused, detail requested) because the planner knew which one it had
  produced and the response did not say.

No new capability, no new execution path, no LLM, no authorisation change. The
frontend still cannot authorise anything: the new fields change labels only.

---

## 2. Measured before the change

```
"what is the time and then create a file called ../p19_escape.txt containing: nope"
   intent=time   actions=['get_time']
   reply: It is 13:01 on Sunday, 27 September 2026.

"what is the time and then frobnicate the warp core"
   intent=time   actions=['get_time']
   reply: It is 13:01 on Sunday, 27 September 2026.
```

The interesting part is that the reason **already existed**. `RulePlanner._anchored`
produces a full explanation -- "*lands outside my file workspace, so I will not create
it by name*" -- and `_composed` saw it, returned `None`, and the fallback threw the
note away with the clause. Phase 19 mostly recovers information the system already
had and discarded.

---

## 3. Changes

### 3.1 Two fields on `Plan` (`jarvis/core/models.py`)

* `unhandled: List[str]` — each clause this plan does not cover, **carrying the
  planner's own reason**: a refusing route contributes its note; a clause nothing
  matched is handed back as `I don't know how to "<clause>".`. No reason is invented.
* `reply_kind: str` — `not_understood | capability_missing | declined | ""`. It
  describes a non-execution only; `""` means a plan was built, and the renderer must
  not read anything into it.

### 3.2 The planner reports them (`jarvis/core/planner.py`)

`_composed()` now returns `(plan, dropped)` instead of `plan | None`, scans **every**
clause rather than bailing on the first unroutable one, and `parse()` attaches the
dropped reasons to whichever plan the fallback produces. `_to_plan()` classifies its
three exits (`declined`, `capability_missing`) and `parse()`'s no-match fallback is
`not_understood`. Composition still requires all clauses to route, so the Phase 14
all-or-nothing safety property is unchanged.

### 3.3 The response and the ledger say it (`jarvis/core/orchestrator.py`)

`_note_unhandled()` appends, for each dropped clause:

> And I did not do that part: *<the planner's reason>*

It is called at all five post-planning returns — direct reply, clarification,
per-action BLOCK, CONFIRM, no-agent, and the final composition — so a dropped clause
cannot be hidden by which branch happened to fire. The same list is written into the
audit record (`detail.unhandled` on the two no-execution rows, and as an `unhandled`
parameter on `_audit_action` for executed and blocked steps), so the chain shows a
partially answered request, not just the screen. No schema change; `detail` is JSON.

### 3.4 The dashboard distinguishes the non-answers (`dashboard/index.html`)

`pipelineState()` reads `plan.reply_kind` and maps it to **NOT RECOGNISED**,
**NOT AVAILABLE**, **NOTHING EXECUTED** (plus the existing NEEDS DETAIL), each with
its own tone. `plan` is now read once at the top because the executed branch needs it
too (see 19-A). A forged `reply_kind` can only ever relabel; the `VERIFIED` path is
still reached exclusively from `actions[].verification === "success"`.

---

## 4. 19-A — a defect this phase created, caught by its own proof

The first live run over HTTP showed the escape sentence rendering **VERIFIED**: once
the dropped clause was named in the reply, the state chip still looked only at the
actions that *did* run, so "here is the time, and I refused the file" was labelled as
complete verification. Fixing 14-D without this would have made the page *more*
misleading than before it.

`VERIFIED IN PART` now applies when every executed step verified **and**
`plan.unhandled` is non-empty, and a real failure still outranks it. Written
test-first again (2 red, then green), and proven live:

```
escape sentence          page=VERIFIED IN PART   ledger=success
frobnicate the warp core page=VERIFIED IN PART   ledger=success
fully-composed sequence  page=VERIFIED           ledger=success   <- not a blanket downgrade
```

---

## 5. Live proof (real server, dashboard bootstrap path, ledger from `action_logs`,
### the shipped `pipelineState` executed by node over each real response)

| Command | `reply_kind` | page state | ledger |
| --- | --- | --- | --- |
| time and then create `../p19_escape.txt` | — | VERIFIED IN PART | `success` + `detail.unhandled` |
| time and then frobnicate the warp core | — | VERIFIED IN PART | success + clause recorded |
| design this system | `not_understood` | NOT RECOGNISED | `no_action` |
| transfer this file to my phone. | `declined` | NOTHING EXECUTED | `no_action` |
| time, and then what is my battery | — | VERIFIED | `success` |

Reply the user actually gets for the first row:

> It is 13:18 on Sunday, 27 September 2026. And I did not do that part:
> ../p19_escape.txt lands outside my file workspace
> (/Users/abhijeetmiskin/JARVIS_REAL_TEST), so I will not create it by name. If you
> really mean that location, give me the full path and I will ask before writing.

And the refusal stayed a refusal: no file appeared outside the workspace.

---

## 6. TDD record

`tests/test_phase19_refusal_transparency.py` — **17 tests**, written first.

* Red → green, watched: 12 initially failed (refused clause named in reply, unroutable
  clause named, clause present in audit detail, all three `reply_kind` classifications,
  the dashboard mapping, the shipped-vocabulary check), then 2 more for
  `VERIFIED IN PART` discovered at §4.
* Green throughout, as guards: nothing dropped when every clause routes, a
  single-clause sentence has an empty `unhandled`, the refused file still is not
  created, a real failure outranks a dropped clause.
* One test of mine was wrong and is recorded as such: I asserted a payload with no
  `direct_reply`, no kind and no actions should read `NOTHING EXECUTED`. That has the
  page asserting a fact the backend never stated; the conservative answer is
  `UNKNOWN`, and the corrected test now also pins that a **forged** `reply_kind`
  cannot produce a state either.
* One test was rewritten mid-phase for the same reason: `capability_missing` was
  first "tested" by a placeholder expression that asserted nothing. Replaced with a
  real `_to_plan` call.

**One unreproduced observation, recorded rather than hidden:** during this phase a run
of `tests/test_phase16_accountability.py` reported `1 failed, 12 errors` with an
`ImportError` at `jarvis/core/__init__.py:377`. Three immediate re-runs and every
subsequent full-suite run (four of them) were clean, including twice at the end of the
phase. I could not reproduce it and have not found a cause; it is not listed as fixed.

---

## 7. Existing test changed — one, and it was my own

`tests/test_phase15_acceptance.py::test_unknown_route_still_reads_as_unknown_not_something_executed`
permitted `("UNKNOWN", "NOTHING EXECUTED")` and now also accepts `NOT RECOGNISED`. The
test's purpose, restated in it, is that an unrecognised command can never render as
verified work; that assertion is unchanged and the new state is strictly more precise.
No other existing test was touched, and nothing was skipped, xfailed or deleted.

---

## 8. Regressions

```
Phase 19 alone          : 17 passed
complete suite, run 1   : 1280 collected - 1276 passed, 0 failed, 1 skipped,
                          3 xfailed - 23.00s
complete suite, run 2   : identical - 25.91s
baseline before phase   : 1263 collected / 1259 passed   ->  +17 tests
```

---

## 9. Files modified

| File | Change |
| --- | --- |
| `jarvis/core/models.py` | `Plan.unhandled`, `Plan.reply_kind` |
| `jarvis/core/planner.py` | `_composed` returns `(plan, dropped)` and scans every clause; `_unrecognised`; `reply_kind` on the three `_to_plan` exits |
| `jarvis/core/orchestrator.py` | `_note_unhandled` at all five post-planning returns; `unhandled` in audit detail at both no-execution records and via `_audit_action` |
| `dashboard/index.html` | `plan` hoisted; NOT RECOGNISED / NOT AVAILABLE / NOTHING EXECUTED from `reply_kind`; `VERIFIED IN PART`; new tones |
| `tests/test_phase19_refusal_transparency.py` | new, 17 tests |
| `tests/test_phase15_acceptance.py` | permitted states widened (its own assertion unchanged) |

---

## 10. What is still not distinguished

**19-B.** A refusal and a request for more detail both arrive as
`reply_kind="declined"`, because both are produced by a route returning a note with no
tool, and the only way to separate them would be to read the note's punctuation -- the
guessing this program has repeatedly refused. `NEEDS DETAIL` therefore still fires only
for `plan.needs_clarification`, which the rules planner does not currently reach
(Phase 16 finding 16-A). Distinguishing them is a planner-side decision about what each
route *means*, not a renderer change.

## 11. Carried forward

Closed here: **14-D**, **17-A** (as far as it can go without guessing), and **19-A**
found and fixed in the same phase.

Still open: **16-F** authentication not ledgered (awaiting the user's choice) ·
**15-E** memory echo rows answering dated questions · **15-F** arXiv 406 · **15-H**
which calendar store was read · **15-J** 21.6 s full-filesystem search · no `read_file`
route · no notification tool · `POST /memory/{id}/forget` overclaims · `ProjectGraph`
delete API · RT-3 · R-2 · R-11-R-14 · desktop bundle exits 1 · hardware **BLOCKED** ·
external credentials · LLM planner · multi-step unattended workflows · 19-B above.

## 12. Git and browser

Not committed. `c1664ed` and `df4583a` remain the only commits; this phase's work is
uncommitted in the working tree, and Phases 18 and 19 are both outside the repository's
history. Staged: no · committed: no · pushed: no.

Browser not used; the page's logic is executed from the shipped file by node over real
backend responses. **LIVE BROWSER DRIVE: NOT PERFORMED.**

Servers stopped; scratch homes `/tmp/p19home*` left for inspection; `~/.jarvis-forge`
untouched.
