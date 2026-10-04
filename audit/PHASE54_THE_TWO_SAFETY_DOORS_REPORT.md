# Phase 54 — the two safety doors that had logic and no handle

| | |
|---|---|
| Phase | 54 |
| Date | 2026-10-04 |
| Starting commit | `35c9abf` (Phase 53) |
| Ending commit | `67d9530`, then `377e79f` (§11a correction) — filled in at the Phase 56 close-out; §9 promised it and did not record it |
| Tests before | 2129 passed · 0 failed · 0 skipped |
| Tests after | **2155 passed · 0 failed · 0 skipped** (72.6 s) |
| Tools before / after | 141 / **141** — this phase added no capability |
| Planner routes before / after | 51 / **51** |
| HTTP surface | 113 objects / 107 paths → **117 / 111** (4 new paths) |
| Confirmation-gated | **28 of 141 — unchanged** |
| Push status | **nothing pushed; local commit only** |

Gaps B and C of the operating prompt: an emergency stop reachable only by typing a sentence,
and a revocation mechanism with no caller at all.

---

## 1. Measured before editing

* `emergency_stop` is a real PHYSICAL-tier tool, confirmation-free, and honest about what it
  cannot do (the robot's own safety controller is outside this process). It had **no route** —
  so the phone, which §30 of the build prompt names first for a stop, could not pull it.
* `PermissionEngine.revoke(tool, device)` writes an explicit denial row. **No product surface
  called it** (measured again this phase: the only `.revoke(` callers are device-level
  `PairingManager.revoke`). The permission engine had one door and no handle.
* One detail worth having checked rather than assumed: `LifecycleReport` defines `__bool__`, so
  `emergency_stop`'s existing `if tasks.cancel(...)` was *not* a truthiness bug. It was still
  changed, for the reason in §3.

## 2. What was built

| surface | what it does |
|---|---|
| `POST /emergency_stop` | calls the **registered tool** through the running core — no second stop implementation — asks for no confirmation, and writes a ledger row |
| `GET /permissions/policy` | the explicit grants and denials the engine currently honours |
| `POST /permissions/{tool}/revoke` | refuses an unknown tool (404), refuses `emergency_stop` itself (409), records the denial, and **re-reads the policy** to report whether the capability is now blocked |
| `POST /permissions/{tool}/restore` | removes the denial so the tool returns to its *declared* policy |
| `dashboard/phone.html` | an Emergency stop control |
| `dashboard/index.html` | an Emergency stop control, and a **Policy** column in the tool list with Revoke / Restore |

No new tool, no new planner route, no new state store.

## 3. The distinction this phase exists for: a report is not a result

`emergency_stop` used to count a job as stopped when `tasks.cancel()` returned an accepted
report. The report says the request was accepted; only the row says the job will not run. The
loop now re-reads the row and sorts on **that**:

```python
tasks.cancel(job_id)
after = str((tasks.get(job_id) or {}).get("status") or "")
if after == "cancelled": stopped.append(job_id)
else:                    still_running.append(job_id)
```

`test_the_stop_reports_state_it_read_back_not_state_it_requested` forces the failure: the
cancel is monkeypatched to accept *and roll the row back*, and the stop must then report
`verified: False` with the job listed as stuck. Under the old code it claimed success.

## 4. Restore is not grant

The obvious implementation of "un-revoke" is `grant(tool)`. It was not used, because
`grant` writes `granted=1`, and Phase 49a's rule is that a standing grant **downgrades CONFIRM
to EXECUTE** for reversible tools. Un-revoking would therefore have handed the capability more
authority than it had before the user ever revoked it.

`restore` deletes the denial rows instead, so the tool returns to whatever its own spec
declares. Proven live: after restore, `delete /tmp/p54t/victim.txt` came back as **CONFIRM** —
not EXECUTE, and not BLOCK — and the response carries `standing_grant: false`. A test pins
`is_granted(...) is False` after a restore.

## 5. Revoking the stop is refused

`emergency_stop` cannot be revoked: 409, no row written, and a `denied` ledger entry. A door
that can be locked from the outside is not a safety door, and this is the one place where
"the caller is authorised" is not sufficient — the panel renders it as *safety interlock*
rather than offering the button.

## 6. Alias safety, kept

Revocation stores the canonical name and `_latest_permission` matches every spelling, so
`revoke open_app` binds `open_application` — and neither touches `close_app`. Both are tested,
which is the Phase 49a invariant expressed as a product action rather than only as a unit
assertion.

## 7. Live, on an isolated instance

`JARVIS_HOME=$(mktemp -d)`, port taken from an ephemeral bind after confirming it was free.
Nothing was written to the user's app, and their instance was checked alive afterwards.

```
POST /emergency_stop            (no token)          -> 401
POST /permissions/not_a_tool/revoke                 -> 404
POST /permissions/emergency_stop/revoke             -> 409 "emergency_stop is the stop itself;
                                                       revoking it would remove a safety door."

"delete /tmp/p54t/victim.txt"                       -> CONFIRM  (before any revocation)
POST /permissions/delete_file/revoke  -> {"revoked": true, "state": {"blocked": true,
                                                         "standing_grant": false}}
"delete /tmp/p54t/victim.txt"                       -> BLOCK
     reply: "I cannot do that: permission for delete_file has been revoked."
GET  /permissions/policy -> [{"tool":"delete_file","granted":0,...}]

POST /permissions/delete_file/restore -> {"restored": true, "denials_removed": 1,
                                          "state": {"revoked": false, "standing_grant": false}}
"delete /tmp/p54t/victim.txt"                       -> CONFIRM again, confirmation_id present

-- the stop, against a real running job (1 succeeded step, 1 pending step) --
POST /emergency_stop -> verified: True | cancelled: 1 | stuck: []
   "1 JARVIS job(s) cancelled. Physical actuators were NOT stopped by this command --
    no robot bridge or safety-controller interface is reachable from JARVIS."
sqlite read-back     -> ('JOB-P54LIVE', 'cancelled')          <- independent of the tool
audit rows           -> emergency.stop: success
                       tool.revoke: denied, success; restore_tool: success
/tmp/p54t/victim.txt still on disk                            <- nothing was deleted
```

## 8. Browser

Both pages changed, so both were driven in a real browser against the isolated instance.

* **Phone, 375 × 812** — Emergency stop present, `onclick` handler attached, page references
  `/emergency_stop`, **0 console errors**.
* **Panel, 1440 × 900** — Emergency stop present and wired; the tool table now has a
  **Policy** column with **140 Revoke controls** (141 tools minus the interlock) and
  `emergency_stop` rendered as *safety interlock*; **0 console errors**. The Recent Actions
  card independently showed the live run's rows: `emergency_stop success`,
  `delete_file blocked`, `restore_tool success`.

**What was not done:** the two buttons were **not clicked**. The session's permission
classifier blocked both the phone stop click and the panel Revoke click as actions with real
side effects needing explicit confirmation, and that refusal was respected rather than worked
around — the click was not re-attempted through `browser_evaluate`. The endpoints behind them
are proven live in §7, and the wiring is proven by reading the rendered DOM, but no click-through
is claimed.

## 9. Defects found in this phase's own work

Reported because the point is the truth of the code, not the appearance of it.

1. **A Phase 53 defect, found by a Phase 54 test.** `self_state.py` counted finished steps with
   `("success", "done")`. Neither string is a step status in this codebase — the enum value is
   **`succeeded`**. So `get_current_task` always reported "0 of N step(s) done", and
   `stop_current_task`'s "steps that already ran" list was always empty. Its Phase 53 live proof
   had inserted rows with the literal `"success"`, which is why the suite and the live run both
   agreed with the bug. Fixed against the real vocabulary, with the constants named.
2. **`GET /permissions/policy` selected a column that does not exist** (`created_at`; the table
   has `granted_at`). Caught by its own test on first run.
3. **A required request body made the button unusable**: `RevokeBody` with a mandatory field
   returned 422 to a bare `POST`, which is exactly what a browser sends. Made optional.
4. **A hard-coded `#fff`** on the new stop button, caught by this repository's own design-token
   guard (Phase 39c) — replaced with `var(--on-accent)`.
5. **Five of my own test-harness errors**, including one where a bulk text-editing script
   deleted the `def` line of the helper it was inserting calls for, and three rounds of mangled
   indentation. The lesson is procedural and it is being recorded rather than repeated: scripted
   multi-site edits into Python are how I keep breaking my own tests, and the fix is targeted
   edits plus `py_compile` before running anything.
6. **The shared session database again**: a device-less denial from one test bound another
   test's device check — correct product behaviour (Phase 49a's asymmetric rule), wrong test
   isolation. The tests now clear the policy table explicitly.

## 10. Numbers, re-measured

| | before | after | how |
|---|---|---|---|
| Test suite | 2129 passed | **2155 passed · 0 failed · 0 skipped** (72.6 s) | `JARVIS_HOME=$(mktemp -d) .venv/bin/python -m pytest -q` |
| Phase 54 tests | 0 | **26** | `tests/test_phase54_the_two_safety_doors.py` |
| Test modules | 80 | **81** | `ls tests/test_*.py \| wc -l` |
| Phase reports | 55 | **56** (this file) | `ls audit/*.md \| wc -l` |
| Lines | 67,266 — 40,911 source · 26,355 tests | **67,882 — 41,174 source · 26,708 tests** | `wc -l` |
| Tools | 141 | **141** | registry — this phase added routes, not capabilities |
| Planner routes | 51 | **51** | unchanged |
| HTTP surface | 113 / 107 paths | **117 / 111 paths** | `len(app.routes)` |
| Confirmation-gated | 28 | **28** | unchanged; no gate was loosened |

## 11. Open items

* **54-A** The stop cancels *queued JARVIS work*. It still reaches no physical actuator, and
  says so. The menu-bar and lock-screen surfaces §30 names do not exist yet (no native shell —
  Phase 58).
* **54-B** A stop is not yet interruptible *mid-step*: a tool already running is not killed,
  only the remaining steps are prevented from being dispatched. The message says "steps that
  already ran are not undone", which is accurate but easy to read as "everything halted".
* **54-C** Revoke is per capability and per device, with no expiry. A denial stays until
  restored; there is no "revoke for 10 minutes", and no bulk revoke.
* **54-D** The panel offers Revoke on all 140 non-interlock capabilities, including ones whose
  revocation would break the panel itself (e.g. `get_recent_activity` empties the card that
  would show it). Deliberately not special-cased — a hidden rule about which revocations are
  "allowed" is exactly the kind of gate that later gets quietly widened — but worth revisiting
  as UX.
* **54-E** No click-through in a browser for either new control (§8). The endpoints are proven;
  the buttons are proven wired, not proven pressed.
* **54-F** Phase 53's `succeeded` defect was found here, not by Phase 53's own tests, because
  its live proof hand-wrote the wrong status string. Any Phase 53 surface that compares step
  statuses deserves the same re-check; `get_current_task` and `stop_current_task` were the two
  that existed and both are fixed.

## 11a. A correction about the delivered-app checks

The close-out checks against the delivered app were described as read-only. **They were not all
read-only.** Two were POSTs: the unauthenticated stop (refused at 401, nothing written) and the
attempt to revoke `emergency_stop`, which is refused at 409 — and the refusal path writes a
ledger row by design. Measured afterwards on the user's instance:

```
tool.revoke | revoke_tool | denied | 2026-10-04T13:49:24Z
```

One audit row, no policy change (`/permissions/policy` still shows only the pre-existing
September rows). A refused attempt to remove a safety door being recorded is the correct
behaviour, but calling the check "read-only" was wrong, and whether to keep that row in the
real history is now a user decision alongside the five `forged` rows already listed.

## 12. Next

Phase 55 — a real web-search capability for `search Google for X`, which is §6's last gap and
the only remaining one that needs a capability rather than a route. It must not be answered by
opening google.com.
