# Phase 18 — A simulated actuation is not a verified world change

Report date: 2026-09-27. Continues Phases 9-17.

---

## 1. Scope

No brief; this was the highest-value item on the carried-forward list, and the only
one the last four phases had all pointed at the same way. Since Phase 10 §12, every
report has recorded the same fact and declined to fix it:

> a simulated actuation records `verification=SUCCESS`, so the hash-chained audit log
> says an effect was established, under a reply that says no real device was reached.

Phase 13 fixed the **symptom** in the dashboard by reading `output.simulated` and
showing SIMULATED, which has since meant the UI was the only honest copy of the fact
while the tamper-evident record was wrong — the inverse of the arrangement every
phase in this program has insisted on ("the frontend renders server truth"). Closed
here. No new capability, no hardware, no simulator added.

---

## 2. Measured before the change

With the simulators on, through the real handlers:

| Tool                   | `ok` | verification | `output.simulated` | ledger token |
| ---------------------- | ---- | ------------ | ------------------ | ------------ |
| `control_home_device`  | True | success      | True               | `success`    |
| `run_home_routine`     | True | success      | True               | `success`    |
| `run_experiment`       | True | success      | True               | `success`    |
| `command_robot_motion` | True | success      | True               | `success`    |

Each of those replies contains the words "simulated; no real device was reached" or
"no real robot was reached". A grep confirmed the blast radius is exactly these four
tools: they are the only handlers in `jarvis/tools/` that put a top-level `simulated`
into `output`, which is what makes the fix below precise rather than blanket.

---

## 3. Changes

### 3.1 The four actuations report what happened

`jarvis/tools/home.py`, `jarvis/tools/robotics.py` — the final `ok()` of each site now
passes `verification=(Verification.UNKNOWN if <simulated> else None)`. `.ok` stays
true (the handler completed and reported honestly), `output.simulated` stays true,
and the non-simulated paths are untouched because `None` preserves `ok()`'s existing
derivation. `Verification` had to be added to `robotics.py`'s imports — the class of
mistake Phase 10 made once already, caught here by checking rather than assuming.

### 3.2 The ledger gets its own token

`JarvisCore._audit_token` maps an UNKNOWN-verification result carrying
`simulated=True` to **`simulated`** — neither `success` nor `failure` nor the borrowed
`unverified`. `unverified` would mean "we do not know whether the world changed";
here the system knows: nothing in the real world changed. The token is derived in the
one function both audit sites already share, so the interactive plan and confirmation
resolution cannot drift apart.

The fail-safe default of that function was preserved deliberately: while adding the
UNKNOWN branch I first wrote the tail as `return "success"`, which inverts its
conservatism for any value that is not one of the three enum members. Corrected to
`return "unverified"` before running anything.

### 3.3 The dashboard no longer depends on the overstatement

`pipelineState()` reached SIMULATED only through `allSuccess && simulated`, so the fix
above would have silently downgraded every simulation to UNKNOWN. The ordering is now
explicit and failure-first:

```
UNAVAILABLE → FAILED → SIMULATED → UNKNOWN → VERIFIED
```

A real failure therefore outranks a simulated success, so SIMULATED cannot become a
new way to hide one (`test_a_real_failure_still_outranks_a_simulated_one`). The audit
card gained `simulated: "warn"`, so the new token is not coloured like an error.

---

## 4. What was deliberately NOT changed

**The simulated car read.** `JARVIS_CAR_SIMULATE=1` yields
`verification=SUCCESS` and it stays that way: reading a labelled simulator is a real
read of that simulator, the values are marked `(simulated)` per reading, and the
reply ends "This is a simulated vehicle, not a real car." Calling it unverified would
assert the read did not happen, which is false. **A read and an actuation are
different claims and get different rules** — pinned by
`test_a_simulated_car_read_is_still_a_successful_read` so the distinction stays a
decision rather than an oversight.

Also untouched: the watch/lab simulators' _delivery_ semantics (Phase 10's
`bears_authority` / `delivery_is_real`), and hardware itself, which remains
**BLOCKED**. No fake device was created to exercise a state.

---

## 5. Live proof over HTTP

Real server on `127.0.0.1:8899`, `JARVIS_HOME=/tmp/p18home` (scratch),
`JARVIS_HOME_SIMULATE=1 JARVIS_ROBOT_SIMULATE=1`, dashboard bootstrap → session →
`POST /command` (approving where the tool asks), ledger read from `action_logs`, and
the page's **own shipped `pipelineState`** executed over each real response by node:

| Command                      | `verification` | ledger      | page      |
| ---------------------------- | -------------- | ----------- | --------- |
| turn on the study desk lamp. | `unknown`      | `simulated` | SIMULATED |
| run study mode.              | `unknown`      | `simulated` | SIMULATED |
| send amr main home.          | `unknown`      | `simulated` | SIMULATED |

```
distinct ledger tokens: ['awaiting_confirmation', 'simulated']
rows where a simulated tool still recorded success: 0
```

Three independent copies of the fact now agree: the reply text, the ledger, and the
dashboard.

---

## 6. TDD record

`tests/test_phase18_simulation_truth.py` — **12 new tests**, written first. 8 of 12
failed before the change, for the right reason:

- 4 parametrised/solo actuation assertions ("wrote success … for a change its own
  reply says never reached a real device"),
- the audit-token test,
- the ledger-through-the-pipeline test,
- the job-step status test,
- the dashboard test (SIMULATED had to stop depending on `allSuccess`).

Two passed immediately as **guards, not proofs**: the car-read distinction (§4) and
the failure-outranks-simulated ordering — both assert behaviour that must survive the
change rather than behaviour the change introduces, and that is stated rather than
blurred.

One test was **wrong before it was right, and the measurement caught it**: I wrote the
scheduler assertion expecting the job to _park_ at `awaiting_approval`, reasoning that
every simulated tool is confirmation-gated so the runner's verification split was
unreachable. Running it showed the job **executes** and returns
`outcome='unverified'` — a better result than I argued for, and now the test asserts
what the code does. The false assumption is recorded here rather than edited out.

---

## 7. Existing tests changed — both of them, and why

Two pre-existing tests failed, and both were **markers pointing at this exact defect**,
not tests of intended behaviour:

1. `tests/test_phase13_dashboard_truth.py::test_simulated_actuation_is_not_shown_as_verified`
   asserted `verification == "success"` behind the message _"the backend
   overstatement this test guards against has been fixed; this test can now be
   simplified rather than deleted."_ Simplified, as directed: it now asserts
   `!= "success"` and still proves the renderer says SIMULATED.
2. `tests/test_home.py::test_control_simulate_reports_simulated` carried
   _"OPEN (Phase 10 §12, not yet fixed): a simulated actuation records
   verification=SUCCESS here … that assertion belongs with the gateway change, not
   smuggled in here."_ This phase **is** that gateway change, so the OPEN is closed
   with the assertions it named (`verified is False`, `verification is UNKNOWN`,
   audit token `simulated`).

No other existing test was modified. Nothing was deleted, skipped or xfailed to get
the suite green.

---

## 8. Regressions

```
Phase 18 alone        : 12 passed
complete suite run 1  : 1263 collected - 1259 passed, 0 failed, 1 skipped, 3 xfailed - 20.16s
complete suite run 2  : identical - 20.96s
baseline before phase : 1251 collected / 1247 passed   →  +12 tests
```

Two runs identical; the only non-green outcomes are the pre-existing 1 skip and 3
xfails.

---

## 9. Files modified

| File                                     | Change                                                                                                            |
| ---------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| `jarvis/tools/home.py`                   | simulated branches of `control_home_device`, `run_home_routine` → `Verification.UNKNOWN`                          |
| `jarvis/tools/robotics.py`               | `Verification` import; simulated branches of `command_robot_motion`, `run_experiment` → UNKNOWN                   |
| `jarvis/core/orchestrator.py`            | `_audit_token` returns `simulated`; fail-safe default preserved                                                   |
| `dashboard/index.html`                   | `pipelineState` ordering (FAILED before SIMULATED, SIMULATED without `allSuccess`); `auditTone` gains `simulated` |
| `tests/test_phase18_simulation_truth.py` | new, 12 tests                                                                                                     |
| `tests/test_phase13_dashboard_truth.py`  | carried-FORWARD assertion simplified as its own message directed                                                  |
| `tests/test_home.py`                     | Phase 10 §12 OPEN closed                                                                                          |

---

## 10. Carried forward

Closed here: **14-E** (and therefore the "OPEN" note from Phase 10 §12, the task-list
item "Simulation truthfulness", and the divergence Phase 13 recorded between the UI
and the ledger).

Still open: **16-F** authentication not ledgered (awaiting the user's choice) ·
**17-A** `no_action` conflates refusal / clarification / missing capability ·
**14-D** a partly-routable sentence answers one half and never mentions the refused
half · **15-E** memory echo rows answering dated questions · **15-F** arXiv 406 ·
**15-H** which calendar store was read · **15-J** 21.6 s full-filesystem search · no
`read_file` route · no notification tool · `POST /memory/{id}/forget` overclaims ·
`ProjectGraph` delete API · RT-3 · R-2 · R-11-R-14 · desktop bundle exits 1 ·
hardware **BLOCKED** · external credentials · LLM planner · multi-step unattended
workflows.

## 11. Git and browser

Not committed: the last commit was authorised for that work only. Working tree is
dirty with this phase's seven files, on top of `c1664ed` / `df4583a`. Staged: no ·
committed: no · pushed: no.

Browser not used — the proof is HTTP plus the shipped renderer executed by node.
**LIVE BROWSER DRIVE: NOT PERFORMED.**

Scratch home `/tmp/p18home` left for inspection; server stopped; `~/.jarvis-forge`
untouched.
