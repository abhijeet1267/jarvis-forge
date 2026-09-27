# PHASE 10 — TRUTH AND AUTHORITY HARDENING

Started 2026-09-26. Input: `audit/PHASE9_FINAL_REPORT.md` and its 13 phase artifacts.
Production changes are traceable to named Phase 9 findings; nothing else was edited.

## A. Baseline (exact Phase 9 state, re-measured before any edit)

| | value |
|---|---|
| Repository | `/Users/abhijeetmiskin/AppData/jarv/jarvis-forge` — **not a git repository** |
| Suite | **126 failed, 875 passed, 1 skipped** / 1002 collected |
| `jarvis/tools/base.py:16` | `from ..core.models import ActionResult` (unfixed) |
| Agents / tools / paths | 15 / 126 / 100 unique (106 routes) |
| Server | not listening on 8765 |
| Real data dir | `~/.jarvis-forge` — **found already polluted by Phase 9 runs**; see §F |

Reproductions re-run green-before-fix for: `/auth/token` anonymous 200 + live token;
`/dashboard` 200 with the token embedded; simulated-watch `CONFIRM` executing a deletion;
`revoke()` losing to a same-second `grant()`; `emergency_stop` returning
`SUCCESS / cancelled_actions=0`.

## B. Changes

### §1 — repository safety baseline
- Verified: `git rev-parse` → fatal, no `.git` at repo, parent, or nested. `git init`
  performed under explicit authorisation (brief §1 + operator decision). Branch `main`,
  **0 commits, 0 staged** throughout.
- **No `.gitignore` existed** anywhere (repo, parent, global). Created one, reported as its
  own change: excludes `.venv/`, `__pycache__/`, `*.py[cod]`, `.pytest_cache/`, `dist/`,
  `app/build/`, `.env`, `*.key`, `*.pem`, `.DS_Store`.
  Measured effect: **4,266 candidate files → 185**. Verified no `.venv`/`__pycache__`/`.pyc`/
  `dist`/`app/build`/`.env` path survives into the candidate set.
- §18 credential scan: `.env.example` values are all empty (`JARVIS_LLM_API_KEY=`,
  `JARVIS_API_TOKEN=`, `JARVIS_EMAIL_PASSWORD=`, `JARVIS_CALENDAR_PASSWORD=`,
  `JARVIS_CAR_API_TOKEN=`). The live secret is `~/.jarvis-forge/api_token`, mode 0600,
  outside the tree — not committable.

### §2 — artefact correction
The brief names `audit/PHASE9_MEMORY_AUDIT.csv`. It does not exist; the real artefact is
`audit/PHASE9_MEMORY_AUDIT.md`. Used that; did not invent a `.csv`.

### P0-1 → §3 — the authorised one-line fix
`jarvis/tools/base.py:16`, one line, import-list only (confirmed programmatically):

```
- from ..core.models import ActionResult
+ from ..core.models import ActionResult, Verification
```

Pre: `ok()` and `unverified()` raised `NameError`. Post: `ok() → SUCCESS`,
`unverified() → UNKNOWN`, `fail() → FAILED`.
Suite: **126 failed / 875 passed → 3 failed / 998 passed / 1 skipped**, 15.71 s.
The 3 residuals were Phase 9's stale assertions (fixed under §12 below). The `+1 pass / −1 skip`
difference from Phase 9's clean-copy figure is the desktop-bundle test running instead of
standing down because `dist/` is present — not a code difference.

### P0-1 → §4 — verification made authoritative
The systemic finding: `verification` was write-only while `.ok` decided everything.

| file | change | finding |
|---|---|---|
| `jarvis/core/models.py` | `ActionStatus` gains `UNVERIFIED = "unverified"` — neither the failure it was not nor the success it did not prove | P0-1 |
| `jarvis/core/orchestrator.py` | audit status token derived from `result.verification`, not `result.ok`. Existing `success`/`failure` tokens kept verbatim so `AuditLog.failures()` and stored records read the same; `unverified` is a new third state | P0-1 |
| `jarvis/core/orchestrator.py` | new `_action_status()` helper maps verification → step status; job status becomes `failed` / `unverified` / `done` instead of `done` on any `.ok` plan | P0-1 |
| `jarvis/core/orchestrator.py` | **`Verification` added to the models import list.** Omitting it would have reproduced the exact P0-1 defect in a new file — caught by checking imports after editing, not at runtime | P0-1 |
| `jarvis/core/recovery.py` | retry and fallback gates changed from `if <result>.ok:` to `if <result>.verification is Verification.SUCCESS`; `Verification` imported | P0-1, R-4 |
| `jarvis/agents/laptop.py`, `jarvis/agents/laptop_center.py` | removed `result.verified = True`. The platform note is still recorded; the agent no longer stamps an observation it never made. This was the defect `tools/base.py:90-99` documents as fixed, reintroduced one layer up for 25 tools | P1-4 / R-7 |

Falsifiability proved, not assumed: the recovery test drives one genuine failure then an
honest `UNKNOWN`, and asserts **2 handler invocations** (so the retry really ran),
`recovered = 0`, `strategy != "retry"`, and no `"Recovered automatically"` in the reply.
Observed before the fix was `recovered=1, strategy=retry, note='succeeded on retry 1'`.

### P0-2 → §5 — master token disclosure
Chosen design: **scoped single-use bootstrap + exchange** (the brief's preferred option).
Owned by a delegated agent; independently re-verified by me against a live server.

| file | change |
|---|---|
| `jarvis/api/app.py` | `/auth/token` now `SameOriginProtected` and returns a bounded **session** credential, never the master token; new `POST /auth/session` redeems a bootstrap behind a mandatory origin gate; `/dashboard` injects a 60 s single-use bootstrap instead of the master token; in-memory bounded (512) credential store |
| `dashboard/index.html` | meta tag `jarvis-token` → `jarvis-bootstrap`; page exchanges at `/auth/session` and keeps the session token in memory only |

Measured after fix:
```
GET /auth/token   (no Origin, no token)  -> 403 {"detail":"Same-origin request required."}
GET /dashboard                           -> 200, 24,003 B, master-token occurrences: 0
master token still works for legitimate clients: /health 200; without token 401
bootstrap reuse                            -> 403 {"detail":"Bootstrap refused."}
anonymous response scan over / /health /docs /openapi.json /dashboard /auth/token
                                           -> 0 occurrences of the master secret on all 7 routes
```

**Residual, confirmed by me (the agent reported it honestly and I reproduced it):** a local
process can still `GET /dashboard`, scrape the bootstrap, and `POST /auth/session` with
`Origin: http://127.0.0.1:8765` (the configured origin) → **200 + working session token**,
which then authorised `POST /command`. Over a shared loopback socket no HTTP-layer signal
separates a browser from a local process that imitates one. The exposed artefact changed from
an indefinite master secret to a 60-second, single-use, in-memory, revocable session. Closing
the remainder requires a design decision (require the master token for destructive routes, or
out-of-band provisioning) touching `jarvis/security/auth.py` — **not made in this phase.**
Against the brief's "do not replace it with another privileged credential that is equally
exposed": the replacement is materially *less* exposed, but it is still a credential a local
process can obtain, and I am recording that rather than claiming the invariant is fully met.

### P0-4 → §6 — watch authority escape
| file | change |
|---|---|
| `jarvis/watch/gateway.py` | new `bears_authority()`, `delivery_is_real()`, `reply_refusal()`; `reply()` refuses an answer to an authorising item whose delivery was not produced by a real transport |

Predicate — refused when the item carries a `confirmation_id` or `command` **and** any of:
not paired · simulate on · transport is `SimulatedTransport` · `delivered_via == "simulated"` ·
`delivered_via` empty. No item-state string alone, no tool name, no `"watch"` literal.
Verified by re-running the Phase 9 exploit: a simulated companion's `CONFIRM` is now refused,
the victim file survives, `POST /watch/reply` returns 400.
`jarvis/tools/watch.py` now labels simulated hand-offs ("handed to the simulated companion,
not shown on a real watch") instead of claiming a screen.
20 new tests; the 65 pre-existing watch tests still pass.

### P1-1 → §7 — confirmation revalidation
| file | change |
|---|---|
| `jarvis/security/policy.py` | new `pre_execute_check()` re-running the refusal gates (params, device existence/permission/reachability, path guard, revocation). Deliberately does **not** re-run the risk classifier — its CONFIRM verdict is what the user already answered, so re-asking would loop |
| `jarvis/core/orchestrator.py` | `resolve_confirmation()` calls it before executing; on refusal replies "That is no longer allowed, so I did nothing: …", records `result="blocked"` with `detail.stage="pre_execute_check"`, and runs nothing |

11 tests: revocation-after-confirmation, device-offline-after-confirmation, protected path
reached by editing the stored record, malformed stored params, audit attribution, plus a
positive control and explicit preservation of replay, substitution, expiry and 128-bit IDs —
the four controls Phase 9 passed and §7 forbids paying for the fix.

### P1-2 → §8 — revocation tie
| file | change |
|---|---|
| `jarvis/security/policy.py` | `is_granted()`/`_is_revoked()` now share one `_latest_permission()` helper ordered by **`rowid DESC`**, not `granted_at DESC`. `granted_at` defaults to `datetime('now')` — one-second granularity — so a grant and its revocation tied and `LIMIT 1` returned the grant, permanently. Removed the duplicated query body |

10 tests including ten grant/revoke pairs forced inside a single clock second (each must end
revoked), a test asserting the two rows genuinely share one `granted_at` value so the
tie-premise cannot silently rot, repeated revoke, revoke-without-grant, per-device isolation,
and the 1.2 s-offset path Phase 9 used to isolate the mechanism.

### P0-3 → §9 — emergency stop truthfulness
Determined first, per the brief, whether a real stop path exists: **it does not.**
`jarvis/robotics/gateway.py:18-24` states the global e-stop lives on the robot's own safety
controller, outside JARVIS; no gateway exposes a stop method. So the fix reports reality rather
than inventing a mechanism.

| file | change |
|---|---|
| `jarvis/tools/system.py` | `emergency_stop()` now cancels real in-flight JARVIS jobs via `ctx.core.tasks` and counts them; reports `physical_stop="not_available"` with the reason; states plainly that physical actuators were **not** stopped by this command and directs to the robot's own e-stop; `verified=` reflects observation; `safety_controllers` no longer asserts "running" |
| `jarvis/tools/system.py` | spec description corrected — it previously read "Immediately stop all physical actions" |

8 tests, including one that creates a running job, asserts the job's DB status becomes
`cancelled`, and one that stubs cancellation to refuse and asserts `verified is False`.
The confirmation exemption for this tool is preserved deliberately and is itself asserted.
`tests/test_watch.py::test_a_routine_confirm_engages_the_emergency_stop` previously passed only
because the reply contained the fabricated "Emergency stop engaged"; it now asserts the honest
reply and forbids the physical claim.

### P0-6 → new finding from the Phase 9 addendum
`verify_chain()` returned at the **first** bad record, so one poisoned row voided verification
of everything after it. On the user's real ledger this reported `ok=false, checked=0` across
461 records.

| file | change |
|---|---|
| `jarvis/security/audit.py` | walks the whole chain, re-syncs from each stored hash, and returns `checked`, `total`, `breaks`, `first_bad_log_id` alongside the original `ok`/`reason`/`at` keys |
| `tests/test_security.py` | new `pristine_chain` fixture snapshots and restores `action_logs` byte-for-byte, so a tamper test cannot leave poison in a shared or real store |

Root cause, corrected diagnosis: `tests/test_security.py:289-306` inserts a row
`LEGACY-TEST`, seals the chain, then `UPDATE`s it to `result='forged'` **and never removes
it**. Measured on the preserved snapshot before → after:

```
ok=False checked=0 of 461   ->   ok=False checked=456/461 breaks=5 first_bad_log_id=LEGACY-TEST
```

My Phase 9 addendum first attributed this to a legacy *unhashed* JSONL record. That was wrong —
`verify_chain` reads `action_logs`, not `audit.jsonl`, and all 461 rows carry a hash. Corrected
in the addendum as **C-2a** before any fix was written, so the record shows the misdiagnosis and
the correction rather than a clean linear story.

### §12 — stale assertions
Phase 9's three `KeyError: 'verified'` tests now read the real `ActionResult` field.
`test_control_simulate_reports_simulated` asserts the current truth and carries an explicit
`OPEN (Phase 10 §12)` comment recording that a simulated actuation still records
`Verification.SUCCESS`; the assertion was not quietly flipped to bless that.

## C. Verification

```
Phase 9 baseline (as found)            126 failed,  875 passed, 1 skipped   / 1002   22.07 s
after §3 one-line fix                    3 failed,  998 passed, 1 skipped   / 1002   15.71 s
current, excluding 2 in-flight files     0 failed, 1077 passed, 1 skipped   / 1078   16.57 s
```

New Phase 10 tests: revocation 10 · confirmation revalidation 11 · emergency stop 8 ·
verification authority 7 · watch authority 20 · token disclosure 19.
`tests/test_security.py` 53 passed. `test_home`/`test_robotics` stale assertions fixed.

## D. Security regression — Phase 9 PASSes retained

| Phase 9 control | status |
|---|---|
| protected paths · symlink escape · traversal · aliased params | PASS → still PASS |
| command allowlist · argv-only exec (no `shell=True`) | PASS → still PASS |
| confirmation replay · substitution · expiry · 128-bit IDs | PASS → still PASS (asserted again in the new revalidation file) |
| scheduler ceiling · scheduler no-self-approval | PASS → still PASS |
| emergency stop stays ungated | PASS → still PASS (now asserted) |
| auth enforcement on destructive routes · CORS default-deny · DNS-rebinding Host refusal · rate limiting · malformed input handling | PASS → still PASS |
| audit chain tamper detection | PASS → still PASS, and now reports coverage instead of aborting |

No Phase 9 PASS regressed to a failure. Two Phase 9 **PASSes were found to be misdiagnoses of
the environment rather than of the code** (chain verified on a throwaway store) and are
recorded in the addendum.

## E. Remaining defects

| ID | finding | state |
|---|---|---|
| P0-5 | memory deletion: `forget_memory` addresses 1 of 2 storing rows | **in flight** (delegated) |
| P1-5 | offline-as-empty in calendar + IMAP | **in flight** (delegated) |
| §12 | simulated home/robotics actuations still record `Verification.SUCCESS`; hardcoded `23.0 °C` / `battery=84%` still presented as "last known state" | **not started** — documented in-test, not blessed |
| §13 | `allowed_devices=['laptop_main']` on all 126 specs is descriptive, not an enforcement boundary; standing grants can reach `delete_file`/`command_robot_motion`; `delete_file` declares `reversible=True` while `permanent=true` does `rmtree` | **not started** — needs a scope decision, brief forbids redesign |
| RT-3 | risk tier mutable at runtime and trusted by the classifier | **not started** |
| R-2 | retry guard sits at risk ≥ 3, so 44 tools auto-retry on an error-text regex | **not started** |
| R-11..R-14 | recovery notes claim "not retried" after a retry ran (now asserted, not fixed); confirmation/scheduler failures write no rows; `/tasks/{id}/retry` fabricates `running` | **partly asserted, not fixed** |
| P0-2 residual | a local process can still obtain a bounded session via scrape + forged Origin | **open by design decision** |
| — | `gateway.status()["online"]` still counts `delivered_via == "simulated"` as having contacted the watch, and `/health` republishes it | **not started** (owned by app.py surface) |

## F. Environment limitations and the data-integrity incident

**No physical hardware or live account exists.** No watch, no robot bridge, no home gateway,
no car adapter, no calendar or mail account. Every "hardware" result here is software-level:
transports are queue-only, the built-in simulator, or closed local ports. **No claim in this
file validates physical device behaviour, and nothing was sent anywhere.** Microphone and
Screen Recording are macOS-permission-denied to the unbundled `python` host.

**Phase 9 wrote to the user's real `~/.jarvis-forge`.** Some Phase 9 subagent runs did not
propagate `JARVIS_HOME`. Residue, measured and **left in place by operator decision**:

| table | audit-day rows added | pre-existing rows that survived |
|---|---|---|
| `action_logs` | 299 | 162 |
| `sessions` | 265 | 134 |
| `confirmations` | 166 | 26 |
| `messages` | 550 | 331 |
| `failures` | 35 | 4 |
| `graph_nodes` | 25 | 0 |
| `projects` | 15 | 1 |

Plus 299 records appended to `audit.jsonl` and six `privacy_delete(scope=memories)` calls that
removed 174 memory rows. **No user memory was lost:** all 152 deleted `MEM-` ids carved from
the file carry `2026-09-25` timestamps; zero pre-audit memory records exist. The deleted
memories were the audit's own seeds.

Probe `delete_file` attempts targeted real paths `/etc/hosts`, `~`, `~/.ssh/id_rsa`; the path
guard blocked every one. All executed deletions hit only `/tmp/jarvis-*` fixtures.

Preserved, read-only, source unmodified:
`/tmp/jarvis-preserved-20260926/jarvis.db.snapshot` and `audit.jsonl.snapshot`, sha256
`2b98bc9ffd397ec8d2b3e90c9f173a15`, byte-identical to source, `PRAGMA integrity_check: ok`.
Nothing else should write to that directory, since the 152 carved records live in slack pages
that further writes could overwrite.

Every Phase 10 command in this session used `JARVIS_HOME=$(mktemp -d)` or an explicit
`/tmp` home, and it is stated as constraint #1 in every delegated brief.

## G. Truthfulness status of claims in this report

| claim | status |
|---|---|
| the one-line fix restores `ok()`/`unverified()` | **REAL** — executed |
| P0-2 master token no longer disclosed anonymously | **REAL** — measured |
| P0-2 a local process can still obtain a bounded session | **REAL** — reproduced |
| P0-4 watch escape closed | **REAL at the software layer** — no physical watch exists |
| P1-1 / P1-2 / P0-3 / P0-6 fixes | **REAL** — executed, with falsifiable tests |
| home/robot/car/watch connectivity | **UNAVAILABLE** — nothing configured |
| simulated home/robotic actuations | **SIMULATED**, but still recorded as SUCCESS — unfixed, documented |
| "last known state" values in home/robotics | **SIMULATED constants presented as history** — unfixed |
| physical emergency actuation stopping | **NOT AVAILABLE** — now reported as such |
| residue still in `~/.jarvis-forge` | **REAL and left deliberately** — operator decision |
| §10 / §11 delegated items | **IN FLIGHT — not verified by me** |

## H. Git state at hand-off

```
git initialized: YES (branch main)
commits:         0
staged files:    0
pushed:          never
.gitignore:      created (reported in §B)
```

Per brief §17 nothing was staged and no commit was created.
