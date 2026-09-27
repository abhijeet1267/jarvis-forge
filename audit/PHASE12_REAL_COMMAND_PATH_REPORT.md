# PHASE 12 — REAL COMMAND PATH + APP END-TO-END VALIDATION

Date 2026-09-26. Result: **the end-to-end typed-command path is proven.**
Per §24 STOP CONDITION the phase ends here; no calendar, mail, watch, robot, home
or car work was attempted. Phase 9 and Phase 11 reports untouched.

## 1. Baseline carried in (Phase 11, re-confirmed not assumed)

1133 collected · 1129 passed · 0 failed · 1 skipped · 3 xfailed. Real
create/read/modify/delete/system-info verified outside JARVIS; deletion
`EXECUTED / VERIFICATION LIMITED` (TCC blocks `~/.Trash`); microphone
permission-denied; no account connected; hardware blocked; simulated watch
cannot authorise; confirmation revalidates; `write_file` exists and reads the
disk back; `write_file` reports `UNKNOWN` when it cannot verify.

The one open gap Phase 11 named — no planner route to `write_file` — is what
this phase closed.

## 2. Planner change (§1 audit first, then the smallest route)

**Traced, not guessed.** Before any edit, the actual routing was measured:

```text
"Create a file called hello.txt containing: JARVIS REAL TEST"  -> intent=unknown, no tool
"Append this line to hello.txt: REAL END-TO-END VERIFIED"      -> intent=unknown, no tool
"read hello.txt"                                               -> intent=unknown, no tool
"list contents of ~/JARVIS_REAL_TEST"                          -> list_directory (route exists)
"Delete a file called hello.txt"                               -> delete_file   (route exists)
```

So `write_file` and `append` had **no rule at all** in the 36-rule
`RulePlanner._rules` list, and the fallback (`intent="unknown"`, fixed
apology string, no action) was already safe — no dangerous tool was reached by
accident. That is why nothing was broken; only a route was missing.

Added two narrow rules, in the existing deterministic style, after
`_copy_move` so no earlier rule is displaced:

| rule | sentence | produces |
|---|---|---|
| `_write_file` | `create/make/write/save` + `(a new the text empty)* file` + `called/named/as/to` **`<name>`** + `containing/with/that says` **`<content>`** | `write_file`, `mode=overwrite` |
| `_append_file` | `append … to/into <path>` `:` **`<content>`** | `write_file`, `mode=append` |

No LLM, no second planner, no second permission engine, no new execution path
(§2). `test_planner.py` 27 passed unchanged. Anything that does not match the
two shapes stays `unknown` — this is deliberately not a general file parser.

**Why the route could not be only a regex.** Measured before designing it:

```text
"hello.txt"                 -> /Users/.../jarvis-forge/hello.txt    protected=None
"../escape.txt"             -> /Users/.../AppData/jarv/escape.txt   protected=None
```

A relative name resolved against the **process working directory**, so a typed
sentence would have written into the repository, and `..` escaped it into a
location the OS guard considers unremarkable. The system path guard refuses
*system* locations; it has never known about the CWD. Anchoring was therefore a
safety requirement, not polish.

New setting `file_workspace` (`JARVIS_FILE_WORKSPACE`), default
`~/JARVIS_REAL_TEST` — the §7 sandbox. `_anchored()` resolves every candidate
(symlinks included, via `Path.resolve()`) and refuses anything landing outside
it, so the spoken route is **workspace-only**. The engine's path guard still
runs on whatever is produced; this narrows the route and does not replace the
guard. Writing elsewhere still requires a direct tool call with an explicit
path.

## 3–6. API path, authentication, permission, confirmation

Driven over HTTP on a real uvicorn server with an isolated `JARVIS_HOME`,
through the **same sequence the dashboard page performs** — never by
constructing a `PlannedAction` or calling the tool (§5, §6, §11).

| step | measured |
|---|---|
| anonymous `POST /command` | `401 "Missing or invalid API token."`, **no file created** |
| `GET /dashboard` | bootstrap present, **master token not in body** |
| `POST /auth/session` (same-origin) | `200`, session token, `expires_in=28800` |
| bootstrap replay | `403 "Bootstrap refused."` — single use holds |
| `POST /command` with the typed sentence | `200`, `intent=write_file`, `tool=write_file`, `needs_confirmation=true` |
| before confirming | **file does not exist** |
| `POST /confirm/{id}?approve=true` | `200`, `decision=EXECUTE`, `verification="success"`, `verified=true` |
| reply to user | `Created hello.txt (17 bytes), read it back and it matches.` |

Confirmation was **not** removed or bypassed to simplify the test; the tool is
risk 2 and stays confirm-gated, and the test asserts the route still reaches
`PermissionEngine` (`assert verdict.decision is CONFIRM`).

## 7–9. Real execution, verification, independent filesystem check

All from **outside JARVIS**, after the confirmation:

```console
$ test -f ~/JARVIS_REAL_TEST/hello.txt   -> EXISTS
$ cat      ~/JARVIS_REAL_TEST/hello.txt  -> 'JARVIS REAL TEST'
$ wc -c    ~/JARVIS_REAL_TEST/hello.txt  -> 17
$ shasum -a 256 ~/JARVIS_REAL_TEST/hello.txt -> 1dece51daa392196...
$ ls -la   ~/JARVIS_REAL_TEST/           -> -rw-r--r-- 17 ... hello.txt
```

After the append sentence: `cat` → `'JARVIS_REAL TEST\nREAL END-TO-END VERIFIED'`
(file now 41 bytes across the two-step version).

| route | status |
|---|---|
| create | **PASS** |
| modify / append | **PASS** |
| read | **NOT_IMPLEMENTED** — no `read_file` sentence exists in the planner. Not added: §9 forbids widening the parser to raise a feature count |
| delete | route pre-exists (`_delete` → `delete_file`, risk 3); verified it demands confirmation and creates nothing before approval |

## 10. Audit

```json
{"tool":"write_file","result":"awaiting_confirmation","risk":"external","confirmation":1}
{"tool":"write_file","result":"success","risk":"confirmed","confirmation":1}
```

Both the request and the confirmed execution are recorded, and the status
token came from `verification`, not `.ok`.

## 11. Negative tests through the real API (§14)

| injection | result |
|---|---|
| `Create a file called ../escape.txt containing: X` | **BLOCKED at the route** — *"lands outside my file workspace"*; no action, nothing written |
| `Create a file called /etc/hosts containing: X` | **BLOCKED** |
| `Create a file called ~/.ssh/id_rsa containing: X` | **BLOCKED** |
| `sshlink -> ~/.ssh`, then `Create a file called sshlink/id_rsa …` | **BLOCKED** — resolved through the symlink, refused; `~/.ssh` untouched |
| `sshlink/../ssh_probe.txt` | **BLOCKED** — no stray file appeared |
| `nested/example.txt` (legitimate) | allowed, routed inside the workspace |
| missing confirmation, destructive | `needs_confirmation=true`, file intact; deleting without approving runs nothing |
| invalid token | `401` |
| `Do something completely unsupported.` / gibberish | `intent=unknown`, **no tool** — nothing dangerous reached |

Two harness flaws found and fixed during this work, recorded because they were
mine, not the system's: an earlier "symlink blocked" result was **invalid** —
I had deleted the sandbox, and with it the symlink, before testing. Re-run with
the symlink present, the refusal is real (above). And `file_created=True` in one
negative row was leftover state from the previous success, not a leak.

## 12–13. Dashboard: usable, but §13 is NOT met

The dashboard is a real API client and the served page performs exactly the
bootstrap → session → `/command` sequence reproduced above (Phase 10 executed
that page's own script under Node; this phase replayed the identical HTTP
steps). **A live browser session was not driven in this phase.**

Status vocabulary audit of `dashboard/index.html`: the words
`PLANNED / AWAITING CONFIRMATION / EXECUTING / VERIFIED / UNKNOWN / UNAVAILABLE /
SIMULATED` appear **zero times**; there is a single `success` occurrence. The UI
is chat-shaped — it prints the assistant's reply, which is now truthful, so it
does not display a *false* state. But it has **no explicit pipeline-state
indicator**, so §13's required distinction is unmet. Not fixed: that is UI
design work, and §12 says do not redesign the dashboard.

## 14. `.ok` authority audit (§17)

28 `.ok` sites classified. Most are legitimately not about external effect:
`DISPLAY` (`cli.py:58`, `api/app.py:615/635/695`, `orchestrator.py:471`),
`CONTROL FLOW` on hard failures (`coding/validation.py:113`,
`tools/coding.py:310/316`, `voice/session.py:175`, `orchestrator.py:302/561`),
the model's own reconciler (`models.py:264/282`), and different `.ok`s on
voice dataclasses (`stt.py:50`, `tts.py:41/69`).

**The search found a third authority site that Phase 9, 10 and 11 all missed:**

`jarvis/scheduler/runner.py:124` recorded a scheduled run from `result.ok`.
Proved by execution before the fix — a job whose handler answered honestly that
it observed nothing:

```text
outcome: 'executed'   detail: 'could not read'
```

Fixed to key on `verification`, adding a distinct `unverified` run status:

```text
outcome: 'unverified' detail: 'could not read'      (after)
```

Classified **AUTHORITY**, now consistent with the two orchestrator sites. All
three are covered by regression tests. `briefing.py:162/168` (`.ok` on
battery/status to compose a briefing) is CONTROL FLOW for display text and
remains a known offline-as-empty leak from Phase 11 — deliberately not
refactored here (§17 says do not touch unrelated uses).

## 15. Tests

```text
collected 1147   run 1: 1143 passed, 0 failed, 1 skipped, 3 xfailed   17.93 s
                  run 2: 1143 passed, 0 failed, 1 skipped, 3 xfailed   17.10 s
new file tests/test_phase12_command_path.py: 14 passed
```

No existing test was altered to make it pass. **Three tests I introduced**
initially broke the suite (`test_battery_command`,
`test_audit_result_is_a_status_token_not_a_message`,
`test_session_runs_command_after_wake_word`), green in isolation and red in the
full run: my scheduler test replaced a handler on the session-scoped `core` and
never restored it. That is the shared-state hazard Phase 9 flagged as PERF-4 and
Phase 10 pinned in the revocation suite — committed by me this phase, caught by
running the suite rather than only the new file, and fixed with a `finally`
restore plus a comment naming the trap.

## 16. Security regression (§16)

Re-run green, unmodified: paths · symlink escape · traversal · aliased params ·
command allowlist · argv-only exec · confirmation replay · substitution ·
expiry · 128-bit ids · revocation-at-resolution · watch authorisation (20) ·
authentication · master-token non-disclosure (19) · rate limit · CORS · Host
enforcement · audit-chain integrity and tamper detection · verification authority
(8) · emergency-stop truthfulness (8). **No Phase 9/10/11 PASS regressed.**

The new route adds a control (workspace containment) and removes none; the
permission engine still gates every action it produces.

## 17. Files modified

Production:
`jarvis/config.py` (+`file_workspace`), `jarvis/core/planner.py` (+`os`, `Path`,
`get_settings` imports; `_CREATE_FILE`, `_APPEND_TO`, `_anchored`,
`_write_file`, `_append_file`; two entries in `_rules`),
`jarvis/scheduler/runner.py` (verification-keyed outcome + `unverified` status).

Tests: `tests/test_phase12_command_path.py` (new, 14).
Reports: this file. `write_file` and the two orchestrator audit sites changed in
Phase 11, not here.

## 18. Remaining blockers

| sev | item |
|---|---|
| **P1** | §13 dashboard state vocabulary absent — no VERIFIED/UNKNOWN/SIMULATED/UNAVAILABLE indicator anywhere in the UI |
| **P2** | No `read_file` route (deliberate scope limit) |
| **P2** | Live-browser dashboard run not exercised this phase; page script proven by replay only |
| **P2** | Scheduler: the third site is fixed, but `briefing.py` still renders unreachable providers as "Nothing scheduled today." / "No unread mail." |
| **P3** | `delete_file` "Moved to Trash" still `EXECUTED / VERIFICATION LIMITED` — no in-process proof of trash arrival |
| carried | simulated home/robotics still record `Verification.SUCCESS`; hardcoded `23.0 °C`/`84%` still presented as last-known; `POST /memory/{id}/forget`, `_remember_outcome()`, `ProjectGraph` delete API; RT-3 mutable risk tier; R-2 broad auto-retry; R-11…R-14; P0-2 bounded-session residual; desktop bundle exits 1 |
| process | **0 commits**, so no diff or review record exists for any Phase 10–12 change |

Notification remains **NOT IMPLEMENTED** (§19) — no fabricated test.
