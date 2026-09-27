# PHASE 13 — DASHBOARD TRUTH-STATE + REAL UI VERIFICATION

Date 2026-09-26. Continues from Phase 12. Documentation was not trusted; the
dashboard file, the response model and the live server were read first.

## 1. Scope

Prove the one thing Phase 12 left open (§13): the real pipeline was truthful but
invisible — `PLANNED / AWAITING CONFIRMATION / EXECUTING / VERIFIED / FAILED /
UNKNOWN / UNAVAILABLE / SIMULATED` appeared **zero times** in
`dashboard/index.html` (verified: exactly one `success` occurrence in the whole
file). Render backend truth in the existing dashboard. No new state machine, no
second planner or permission engine, no LLM, no framework, no build step, no
redesign, no new execution path.

## 2. Actual dashboard audit (§1)

`dashboard/index.html`, 591 lines, one inline `<script>`, no build system.

| question | finding, from the source |
|---|---|
| bootstrap | server injects a single-use code into `<meta name="jarvis-bootstrap">`; the page `POST`s it to `/auth/session` |
| authentication | session token held **in memory only** (`let TOKEN`), sent as `X-JARVIS-Token`; never `localStorage` |
| send | `POST /command` with `{text, source:"dashboard"}` |
| received | full `CommandResponse` JSON |
| displayed before | `data.reply` plus a literal `[pending confirmation CONF-…]` appended when `confirmation_id` is set — **no state at all** |
| truth fields already present | `needs_confirmation`, `confirmation_id`, `decision`, `intent`, `blocked_reason`, `actions[].verification`, `actions[].verified`, `actions[].ok`, `actions[].output.simulated`, `actions[].output.provider_status` — **all already serialised, none added** |
| confirmation state | yes, equivalent: `/confirm/{id}` returns a `CommandResponse` with the same fields |
| reusable rendering | yes: `escapeHtml`, `authHeaders`, `get`, `refresh`, and an existing tone vocabulary `.ok/.warn/.bad/.muted` used by `loadAudit` and `.approve` chips |

Notable: `loadConfirmations()` already renders **Approve/Deny** buttons calling the
real `/confirm/{id}?approve=` endpoint, so the full user flow existed; only the
state display was missing. Nothing had to be added to make the flow reachable.

## 3. Backend fields used (§2 — no new fields created)

`verification` is the only field permitted to produce VERIFIED. Confirmed against
the model, not assumed: `Verification.SUCCESS="success"`, `FAILED="failed"`,
`UNKNOWN="unknown"`; `ActionResult` serialises `ok`, `verified`, `verification`,
`output`, `error`, `message`.

## 4. State mapping (§3)

Implemented as one function, `pipelineState(data)`, inside the page.

| backend condition | dashboard state | evidence |
|---|---|---|
| `needs_confirmation===true` and `confirmation_id` | **AWAITING CONFIRMATION** | live: `intent=write_file`, state `AWAITING CONFIRMATION`, file absent |
| `actions[].verification==="success"` for all, none simulated | **VERIFIED** | live: `verification=success, verified=true` → `VERIFIED`; 25-byte file on disk |
| all success **and** any `output.simulated===true` | **SIMULATED** | real `control_home_device` under `simulate=True` → `SIMULATED`, **not** VERIFIED |
| any `verification==="failed"`, provider reachable | **FAILED** | real `delete_file` on a missing path → `FAILED` |
| any `verification==="failed"` and `output.provider_status` in `unavailable/network_error` | **UNAVAILABLE** | real dead-CalDAV `list_events` → `UNAVAILABLE` |
| any `verification==="unknown"` | **UNKNOWN** | real `unverified()` result → `UNKNOWN` despite `ok=true` |
| no actions, `decision==="BLOCK"` or `blocked_reason` | **BLOCKED** | denied confirmation → `BLOCKED` |
| plan has actions, none executed, not awaiting | **PLANNED** | mapping test |
| request in flight (the page's own state) | **EXECUTING** | set before `fetch`, cleared by the response |
| nothing above applies | **UNKNOWN** | conservative default |

Two states beyond the brief's list, chosen for truthfulness rather than
convenience:

* **BLOCKED** — a refused action is not a `FAILED` attempt, and collapsing a
  correct refusal into "failure" is the same conflation Phases 9-12 kept
  documenting.
* A transport error renders **UNKNOWN**, deliberately *not* FAILED: if the
  connection drops after the request left, the server may have completed the
  action, and "FAILED" would be an unsupported claim about the world.

Explicitly **not** inferred from: HTTP 200, `ok===true`, a reply containing
"success", absence of an exception, or a tool name containing "watch"/"robot".
Each of the first three is now a separate test.

## 5. Real end-to-end test (§5, §6)

Live uvicorn on `127.0.0.1:8793`, `JARVIS_HOME` in `/tmp`, driven over HTTP
exactly as the dashboard does — bootstrap → session → `POST /command` →
`POST /confirm/{id}?approve=true`. No `RulePlanner`, `PlannedAction` or
`write_file` call was made directly.

| step | measured |
|---|---|
| master token in served page | **absent** |
| `POST /auth/session` | `200`, session token issued |
| typed `Create a file called phase13_dashboard_test.txt containing: JARVIS PHASE 13 VERIFIED` | `200`, `intent=write_file`, `needs_confirmation=true` |
| dashboard state at that moment | **AWAITING CONFIRMATION** |
| file exists before approval | **No** |
| after `?approve=true` | `verification=success`, `verified=true` |
| dashboard state after approval | **VERIFIED** |
| reply | `Created phase13_dashboard_test.txt (25 bytes), read it back and it matches.` |

## 6. Independent filesystem verification (§6)

Checked from outside JARVIS, after VERIFIED was rendered:

```console
exists   : True
content  : 'JARVIS PHASE 13 VERIFIED\n'
bytes    : 25
sha256   : 51421265520574af4fce7510…
listing  : -rw-r--r--@ 1 abhijeetmiskin staff 25 Sep 26 16:53 phase13_dashboard_test.txt
```

All writes stayed inside `~/JARVIS_REAL_TEST/`.

## 7. Negative tests (§7, §8)

| case | rendered state | side effects |
|---|---|---|
| `Create a file called ../phase13_escape.txt containing: …` | **UNKNOWN** (never VERIFIED) | refused by the route; `~/phase13_escape.txt` absent |
| `Create a file called /etc/hosts containing: …` | **UNKNOWN** | no modification |
| `~/.ssh/phase13` | **UNKNOWN** | not created |
| confirmation denied (`approve=false`) | **BLOCKED** | file not created |
| confirmation never granted | **AWAITING CONFIRMATION** | file not created |
| anonymous `POST /command` | **UNKNOWN** (`401`) | 0 actions |
| invalid token | `401` | — |
| real partial memory forget (echo surviving) | **UNKNOWN** | asserted not VERIFIED |

**Honest limitation:** the three route refusals render **UNKNOWN**, not BLOCKED,
because a planner refusal returns `intent` + `direct_reply` with no
`decision`/`blocked_reason`. The chip is therefore conservative but less
informative than it could be. Making it BLOCKED needs one backend field, which
§3 discourages and §15 discourages expanding — so it is documented, not
silently patched. §7A's hard requirement (never VERIFIED) is met.

## 8. Simulation / unavailability (§8)

| state | status |
|---|---|
| UNAVAILABLE | **TESTED** — real unreachable CalDAV (`127.0.0.1:9`), needs `calendar_url` **and** user **and** password; a URL alone falls back to the local provider, which genuinely is empty. That distinction initially broke my own test and is now asserted in it. |
| SIMULATED | **TESTED** — real `HomeGateway(simulate=True)`. The backend still records `verification=success` for a simulated actuation (carried from Phase 12, deliberately unfixed); the renderer keys on `output.simulated` and shows SIMULATED anyway, so the UI does not launder the overstatement. The test asserts that backend state is still present and says so if it is ever fixed. |
| FAILED | **TESTED** — real delete of a missing file |
| UNKNOWN | **TESTED** — real `unverified()` result, and the route refusals above |
| PLANNED | **TESTED** against a mapping fixture only — no live route in the current planner produces a plan with no executed action and no confirmation |
| EXECUTING | client-observed in-flight state; not exercised as a captured frame |
| hardware | **HARDWARE UNAVAILABLE** — no watch, robot, home gateway or car. Nothing was connected, simulated-as-real, or claimed physical. |

No fake hardware was created. Unavailable was never converted into success.

## 9. `.ok` authority audit (§11)

Full sweep of `jarvis/`, classified. Nothing was refactored beyond confirming
Phase 12's fixes hold.

| file:line | purpose | class | safe | changed here |
|---|---|---|---|---|
| `scheduler/runner.py:126` | verification gate before `executed` | **scheduling authority** | yes — re-verified, `unverified` job now yields `outcome: 'unverified'` | no |
| `scheduler/runner.py:132` | `executed` vs `unverified` branch after the gate | scheduling authority, correctly ordered | yes | no |
| `orchestrator.py:152` (`_audit_token`) | audit status | **audit authority** | yes | no |
| `orchestrator.py:478-485` | second audit site | audit authority | yes | no |
| `orchestrator.py:302` | stop the plan on hard failure | control flow | yes (UNKNOWN continues, by design) | no |
| `orchestrator.py:471` | choose reply wording | presentation | yes | no |
| `orchestrator.py:561` | whether to record an outcome | control flow (known echo cause) | yes, for this purpose | no |
| `models.py:264,282` | the `verified`/`verification` reconciler | verification authority by design | yes | no |
| `agents/laptop.py:43`, `laptop_center.py:43` | set `verification_note` only | presentation | yes — `verified=True` overwrite removed in Phase 10 | no |
| `coding.py:310,316` | git sub-step control | control flow | yes | no |
| `briefing.py:162,168` | compose a briefing sentence | presentation, **known offline-as-empty leak** | carries forward | no |
| `cli.py:58` | terminal marker | presentation | yes | no |
| `voice/stt.py:50`, `tts.py:41,69` | different `.ok` on voice results | not ActionResult | n/a | no |

**No new authority site was introduced**, and the one place Phase 12 found a
third site still behaves correctly. Frontend adds no authority: it reads fields,
never decides.

**§10 security property verified by execution:** `CommandRequest` accepts only
`text, source, session_id, device_id, user_id, context`. Posting
`{"verified": true, "verification": "success", "decision": "EXECUTE"}` to
`/command` is **silently dropped** — no `verified` in the response, and the
`EXECUTE` decision returned was the backend's own for a low-risk tool. The
renderer likewise ignores a client-shaped `{"verified": true}` payload
(maps to UNKNOWN). The dashboard cannot be talked into a state.

## 10. Regression (§12, §13)

New: `tests/test_phase13_dashboard_truth.py`, **13 tests**, each state derived
from a **real backend call** and mapped by the **page's own `pipelineState`**,
extracted from `dashboard/index.html` at test time and run under Node — so the
test cannot drift from what ships. Node absent → skip, not silent pass.
`test_page_ships_the_vocabulary` fails if a state word stops reaching the file.

Shared-state discipline, per the Phase 12 lesson: every `monkeypatch.setattr`
on the settings singleton, every `reset_home_gateway` and every
`core.tools._handlers[...]` replacement is restored by the fixture or a
`finally`. One test asserts the *count* of memory rows it depends on and fails
loudly if the product changes under it, rather than quietly passing.

```text
focused   tests/test_phase13_dashboard_truth.py      13 passed        3.90 s
full run 1   1156 passed, 0 failed, 1 skipped, 3 xfailed   82.45 s
full run 2   1156 passed, 0 failed, 1 skipped, 3 xfailed   60.78 s
collected    1160        counts identical across runs: YES
Phase 9 baseline for reference: 126 failed / 875 passed / 1 skipped of 1002
```

Runtime differed (cold caches on run 1); counts did not.

## 11. Files modified

`dashboard/index.html` — `.state` CSS reusing existing tone classes; `#state`
element with `#stateChip`/`#stateNote`; `STATE_TONE`, `pipelineState()`,
`renderState()`; wired into `send()` (EXECUTING → result state, transport error
→ UNKNOWN) and `resolveConfirmation()`.

`tests/test_phase13_dashboard_truth.py` — new.

No production Python file was modified in this phase. No backend field was added.

## 12. Remaining blockers — carried forward only (§15)

| | item |
|---|---|
| **P2** | Planner refusals carry no machine-readable state, so BLOCKED renders as UNKNOWN for route-level rejections |
| **P2** | Simulated home/robotic actuations still record `Verification.SUCCESS`; the UI compensates, the record does not |
| **P2** | Hardcoded `23.0 °C` / `battery=84%` still presented as "last known state" |
| **P3** | PLANNED and EXECUTING not exercised from a live flow; no safe route produces them |
| **P3** | No live-browser verification — see below |
| carried | read_file route · notification tool · `POST /memory/{id}/forget` · `_remember_outcome()` · `ProjectGraph` delete API · RT-3 mutable risk tier · R-2 · R-11…R-14 · desktop bundle exits 1 · hardware · external credentials · LLM planner |

## 13. Git state

```text
staged:   0
committed: 0 commits exist (none created here)
pushed:    never
branch:    main   (no HEAD commit; entire tree untracked)
```

No git write operation of any kind was performed. Because there is still no
commit, `git diff` cannot show this phase's change set — carried forward as a
process risk.

## Browser

```text
LIVE BROWSER DRIVE: NOT PERFORMED
```

A Playwright MCP browser *is* available in this session, so §14's "no
mechanism exists" branch does not apply. The navigation was attempted and was
**blocked by a policy requiring explicit authorization** for browser automation
in this turn. It was not retried or worked around. Everything claimed above was
therefore produced by driving the **actual served page and API over HTTP** and
by executing the **page's own extracted JavaScript** — not by observing a rendered
browser. No live-browser session is claimed anywhere in this report.
