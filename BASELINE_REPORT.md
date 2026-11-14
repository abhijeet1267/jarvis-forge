# JARVIS Forge — Phase 0 Baseline Audit

**Phase:** 0 (audit only)
**Date:** 2026-09-25, ~08:20 local
**Auditor:** engineering agent, read-only pass
**Rule observed:** no production code was modified to produce this document. This file is the only artifact written.

Method note: every number below was measured in this session. Where a claim came
from a subagent's code reading, I re-verified it myself before writing it here,
and the two live findings are quoted with their actual command output.

---

## 1. Baseline

| Item                         | Measured value                                                                              |
| ---------------------------- | ------------------------------------------------------------------------------------------- |
| Repository root              | `/Users/abhijeetmiskin/AppData/aisys/jarvis-forge`                                          |
| **Git revision**             | **none — this is not a git repository** (`git rev-parse` → `fatal: not a git repository`)   |
| Working tree state           | no VCS; `dist/` contains a built copy of `app/launcher.py`                                  |
| Python                       | 3.11.16 (project venv `./.venv`)                                                            |
| Declared deps                | fastapi, uvicorn[standard], pydantic, pydantic-settings, httpx; dev: pytest, pytest-asyncio |
| Tests collected              | **976**                                                                                     |
| Passing / failing / skipped  | **975 / 0 / 1** (skip = live arXiv test, network-gated)                                     |
| Full-suite wall clock        | 16.5 s                                                                                      |
| Source                       | 126 modules, 29,837 lines, 26 test files                                                    |
| Tools                        | 126                                                                                         |
| Risk tiers                   | READ 68 · LOW 33 · EXTERNAL 11 · DESTRUCTIVE 11 · PHYSICAL 3                                |
| Tools requiring confirmation | 24                                                                                          |
| Agents                       | 15                                                                                          |
| API paths                    | 100 (106 route objects incl. method variants)                                               |
| Intent handlers              | 36 (methods of `RulePlanner` with signature `(self, text, raw)`)                            |
| Config fields                | 55                                                                                          |
| Desktop app                  | 544K, `codesign --verify` valid and satisfies its Designated Requirement; self-test 7/9     |
| Live bind                    | `127.0.0.1` only (verified: connection to the LAN IP fails)                                 |
| Live auth                    | 10/10 probed state routes return 401 without a token; Host-header spoof returns 400         |
| Unauthenticated routes       | 8 of 106 (enumerated in §4, risk CR3)                                                       |

**The instruction document's stated baseline (968 collected / 967 passing) is
already stale**; the true numbers are 976 / 975.

---

## 2. Actual architecture as built

```
CLI / dashboard / Tk launcher / voice session / HTTP API
                 → jarvis/api/app.py  (106 route objects)
                 → core/orchestrator.py  JarvisCore.handle()
        guard phrases → core/planner.py  RulePlanner (36 regex handlers, no LLM)
                     → security/policy.py  PermissionEngine.evaluate()
                     → EXECUTE/CONFIRM/BLOCK
                     → agents/base.py:54  (the one registry dispatch site)
                     → tools/*.py  handlers
                     → recovery → audit → memory → response
```

Confirmed true, with evidence:

- **The LLM is not in the loop.** `RulePlanner` is pure regex; `planner="rules"`;
  `ModelRouter` is unused in the request path. The spec's "LLM must never be the
  security boundary" currently holds because there is no LLM at all.
- **Risk is never caller-supplied in the orchestrated path.** It is read from
  `ToolSpec.risk` in the registry; `PlannedAction` has no risk field.
- **Scheduler escalation is genuinely blocked.** Ceiling at creation
  (`MAX_SCHEDULABLE_RISK = EXTERNAL`), re-evaluated per run, CONFIRM parks rather
  than self-approving, and no code path raises a job's `max_risk` after creation.
- **Confirmation basics hold.** 128-bit ids, 300 s TTL, no replay, no
  cross-tool reuse, and no tool can resolve one.
- **Live proof of the gate:** `move the robot arm 10 centimeters forward` →
  `decision: CONFIRM`, audit row `result=awaiting_confirmation`. Correct.

Confirmed false — see §3 and §4:

- There is **no verification stage** in the pipeline.
- Not every execution goes through the permission engine.

---

## 3. GREEN / YELLOW / RED matrix

Using the definitions given: GREEN = implemented **and** tested **and** live
verified here; YELLOW = implemented but an external dependency is unavailable;
RED = not implemented.

### GREEN (measured working, this session)

| Capability                                                      | Evidence                                                                                |
| --------------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| Intent → plan → permission → execute → audit → respond          | live demo sweep, 14/14 correct dispositions; `/command` CONFIRM observed live           |
| Pre-planning phrase guard (`delete everything`)                 | live `decision=BLOCK`                                                                   |
| Permission gate incl. PHYSICAL confirmation                     | live CONFIRM + `awaiting_confirmation` audit row                                        |
| Rate limiting / CORS / Host-header guard                        | live 401 / 400 responses                                                                |
| File search, directory list, read, battery, time, system status | live values                                                                             |
| App launch/quit **dispatch**                                    | ran (but see RED-Verification: success is unproven)                                     |
| Hybrid memory write + recall                                    | live remember → recall round-trip                                                       |
| Local maildir read/search/draft                                 | `/integrations` → `local_maildir` active                                                |
| Local calendar + daily briefing                                 | live "Your today is clear."                                                             |
| Scheduler with ceiling enforcement                              | code-verified ceiling + re-evaluation; **needs live run to be GREEN under this rubric** |
| Vision OCR (macOS Vision)                                       | `/vision/capabilities` → present **and** responsive                                     |
| Text to speech                                                  | `macos_say`, live                                                                       |
| Desktop app build + signature                                   | self-test 7/9, signature valid                                                          |
| Microphone **state reporting**                                  | four states, live-verified settling in 3.0 s                                            |

### YELLOW (built; real dependency absent)

| Capability                    | What is missing                                                 |
| ----------------------------- | --------------------------------------------------------------- |
| Microphone capture            | macOS consent (`denied` for an unbundled process)               |
| On-device STT                 | Developer ID signature (or whisper.cpp / cloud key)             |
| Screen capture                | Screen Recording permission                                     |
| Home / robotics / car / watch | no gateway, adapter, or companion connected                     |
| Remote email / calendar       | no IMAP/SMTP or CalDAV credentials                              |
| Cloud LLM planner             | not configured (`llm_enabled: false`) — by design at this stage |
| Vision content understanding  | no vision-model endpoint configured                             |

### RED (not implemented)

| Capability                                         | Status                                                                 |
| -------------------------------------------------- | ---------------------------------------------------------------------- |
| **Verification engine** (spec §7, §11)             | **absent** — no OBSERVE / expected-vs-actual stage, no `UNKNOWN` state |
| Continuous acoustic wake word                      | interface reserved, no engine                                          |
| Truthful machine-level audit for no-ops            | not implemented (see CR1)                                              |
| Policy-gated dispatch for direct API handler calls | not implemented (see CR2)                                              |
| Immutable audit anchor                             | not implemented (see CR4)                                              |
| Version control / checkpointing                    | not a repository at all (see CR5)                                      |

---

## 4. Documentation vs measured reality

These are the places where `STATUS.md`, `README.md` or `PROJECT_DETAILS.md`
describe something the code does not do. Ordered by severity.

1. **Docs: pipeline includes VERIFICATION. Code: it does not.**
   `orchestrator.handle()` runs guard→plan→permission→execute→recovery→audit→
   memory→reply. `tools/base.py:81-90` `ok()` hardcodes `verified=True`, and a
   tool that reports `verified=False` in `**output` has that value swallowed into
   the output dict while `ActionResult.verified` stays `True`.
   `agents/laptop.py:38-40` and `laptop_center.py:41-43` then invent
   `verification_note = "executed on <platform>"` — a note derived from the OS
   name, not from observation.

2. **Docs: hardware agents are "honest offline."**
   True of the _sentence_, false of the _record_. Live:

   ```
   REPLY: No robot bridge is configured, so I could not move Robot arm 01.
          No motion command was sent.
   AUDIT: tool=command_robot_motion risk=confirmed result=success
     detail: {"ok": true}
   ```

   Nothing was sent; the log says success. Any consumer of `/audit`, the
   dashboard, a briefing, or another agent inherits a false claim.

3. **Docs: `PermissionEngine` is the only authoriser.**
   Several routes call handlers directly instead of dispatching through the
   engine, including `/workspaces/{id}/commit` (`api/app.py:1592`, importing
   `commit_workspace` and calling it at `:1600`), the laptop_center routes
   (`:1924-1966`), vision/screen capture (`:1851-1898`), privacy delete (`:696`),
   memory forget (`:1197`, `:1810`), and a raw `DELETE FROM memories` (`:1220`).
   `core/recovery.py:201-208` also dispatches an _alternative tool_ with no
   policy call.

4. **A DESTRUCTIVE commit is authorised by the request payload.**
   `CommitBody.authorized: bool = False` (`api/app.py:1463`) is forwarded
   straight to the handler (`:1600`). The go/no-go is data the caller supplies,
   not a decision the permission engine made.

5. **Audit `risk` is free text.** Live row above shows `risk=confirmed`, which is
   not a `RiskLevel`. Other routes hardcode `"high"`, `"medium"`, `"external"`.

6. **`/auth/token` is unauthenticated and returns the real token.**
   Live: `{"auth_enabled":true,"token":"lQUMs3bz…"}` — matching the on-disk
   secret. Its only defences are a same-origin CORS rule and an `Origin` check.
   `GET /dashboard` is _also_ public and embeds the token in the HTML, so the
   `Origin` check is bypassable by any local process: verified by scraping
   `/dashboard` anonymously and finding the full token.
   Unauthenticated set: `GET /openapi.json`, `/docs`, `/docs/oauth2-redirect`,
   `/redoc`, `/`, `/auth/token`, `/dashboard`, `POST /devices/pair/request`.

7. **Audit tamper-evidence is weaker than described.** `seal_chain()`
   (`security/audit.py:153-183`) re-hashes **every** row from genesis whenever
   any row has an empty hash, and `record_hash` defaults to `''`. Blank one
   hash after editing a row and the next boot re-seals the chain over the
   tampered content, after which `verify_chain()` reports no break. The JSONL
   anchor is never read during verification.

8. **Confirmations are not session-bound.** `policy.py:276` stores no session
   tie, `api/app.py:357` accepts any id, and `pending_confirmation` is a
   process-global singleton shared by all callers. A voice "yes" can resolve a
   confirmation created elsewhere (`voice/session.py:134-139`), and
   `awaiting_followup` is set by any reply ending in `?` (`:161`), which drops
   the wake-word requirement. A row with an unparseable `created_at` is treated
   as never expired (`policy.py:303-305`).

9. **Docs: `open_app` verifies the app exists.** It checks `open -a` exit status
   only (`tools/apps.py:36-56`); `close_app` likewise.

10. **Instruction document's own baseline is stale** (968/967 vs measured
    976/975). Consistent with RULE 15 — corrected here rather than trusted.

---

## 5. Critical risks

| #   | Risk                                                                                                                                                                      | Why it matters                                                                                                                                                                                                | Severity                     |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------- |
| CR1 | Success is fabricated at the structured layer: `verified=True` hardcoded, no-op hardware actions audited as `result=success`                                              | The project's core rule is "never claim success unless measured". Prose is honest; the machine record is not, so every downstream consumer of `/audit`, `/metrics`, briefings and dashboards is being lied to | **CRITICAL**                 |
| CR2 | Authenticated local callers can reach DESTRUCTIVE/PHYSICAL handlers through routes that skip `PermissionEngine.evaluate()`, with `authorized` taken from the request body | Bypasses confirmation, risk re-check and revocation for real mutating actions                                                                                                                                 | **CRITICAL**                 |
| CR3 | The API token is readable by any process on the machine (`/auth/token`, and embedded in `/dashboard`)                                                                     | On a shared Mac, another user's process becomes JARVIS with destructive and physical reach. Loopback-only binding limits it to local, not to you                                                              | **CRITICAL** on shared hosts |
| CR4 | Audit chain can be laundered by blanking one hash; no external anchor                                                                                                     | Removes the tamper-evidence the audit system is advertised to provide                                                                                                                                         | **HIGH**                     |
| CR5 | No version control                                                                                                                                                        | The mandated loop's CHECKPOINT step, and RULE 12, are unimplementable; no rollback for any change made in later phases                                                                                        | **HIGH**                     |
| CR6 | Confirmations not session-bound + global pending singleton + immortal-on-bad-timestamp                                                                                    | Cross-talk between concurrent callers; a "yes" from one channel can authorise another's action                                                                                                                | **MEDIUM**                   |
| CR7 | Scheduler writes no audit records (`runner.py:125,128`) and `BUILTIN_ACTIONS` skip the engine (`:49-56`)                                                                  | Unattributed background execution                                                                                                                                                                             | **MEDIUM**                   |
| CR8 | Vision OCR remains intermittent (0.6 s vs >120 s under load)                                                                                                              | Bounded and self-reporting, not fixed                                                                                                                                                                         | **LOW**, known               |

---

## 6. Recommended Phase 1 scope

The operating loop says: if a phase exposes a critical architectural defect,
stop and repair that first. Two do qualify, and they are adjacent enough to fix
together:

**Phase 1 = make the machine record as honest as the prose, and gate every
execution.**

1. **Build the verification stage the spec describes** (REQUEST → EXECUTE →
   OBSERVE → expected vs actual → SUCCESS / FAILED / **UNKNOWN**).
   - Remove the hardcoded `verified=True` from `ok()`; require the handler to
     state what it observed, defaulting to UNKNOWN rather than SUCCESS.
   - Delete the fabricated `verification_note` in the two laptop agents.
   - Map a no-op/unreachable-backend outcome to UNKNOWN end to end, and make the
     audit row say the same thing the user was told.
2. **Close the authorisation-free paths**: route the direct-call API endpoints
   and the recovery alternative-tool dispatch through `PermissionEngine`,
   replace `CommitBody.authorized` with an engine-issued confirmation, and let
   `risk` in audit come from `RiskLevel` only.

Deferred deliberately to later phases: CR3 token exposure (Phase 1b or a
security phase — it changes the dashboard's bootstrap and needs its own design),
CR4 chain anchor, CR6 session binding, CR7 scheduler audit, and anything
touching real hardware.

Deliberately **not** in scope: adding an LLM planner. It is Phase 18 in the
roadmap and the deterministic core should not be widened before verification
exists to check it.

## 7. Files Phase 1 would modify

Code:

- `jarvis/tools/base.py` — `ok()`/`fail()` verified semantics
- `jarvis/core/models.py` — `ActionResult.verified` → tri-state; `Verification` record
- `jarvis/core/orchestrator.py` — verification stage; truthful audit outcome mapping
- `jarvis/agents/base.py` — dispatch gains the observation hook; recovery path gating
- `jarvis/agents/laptop.py`, `jarvis/agents/laptop_center.py` — stop fabricating verification
- `jarvis/core/recovery.py` — policy-check the alternative tool
- `jarvis/api/app.py` — commit/`authorized`, laptop_center + vision + privacy + memory routes through the engine; audit `risk` values
- `jarvis/security/audit.py` — result vocabulary; refuse to re-seal over existing hashes (anchor)
- `jarvis/tools/apps.py` — verify the process actually exists
- `jarvis/tools/robotics.py`, `jarvis/tools/home.py` — no-op must not yield success
- `jarvis/email/email_provider.py` + `jarvis/tools/email.py` — "sent" requires provider evidence

Tests:

- new `tests/test_verification.py`; regressions for each false-success above
- updates in `tests/test_robotics.py`, `test_home.py`, `test_api.py`,
  `test_pipeline.py`, `test_coding.py`, `test_email_calendar.py`, `test_audit`
  coverage inside `test_security.py`

Docs: `STATUS.md` (§1 health rows, §8 limitations, §11 history), `README.md`
(pipeline + safety model), `PROJECT_DETAILS.md` (§1 pipeline, §5 working table,
§8/§9), and this file.

---

## 8. Reproduce this baseline

```bash
cd /Users/abhijeetmiskin/AppData/aisys/jarvis-forge
PY=./.venv/bin/python

$PY -m pytest -q                                   # 975 passed, 1 skipped
$PY -m pytest --collect-only -q | tail -1          # 976 collected
git rev-parse --is-inside-work-tree                # fatal: not a git repository

$PY -c "import importlib,collections; m=importlib.import_module('jarvis.api.app'); \
from jarvis.tools import build_default_registry as b; r=b(); \
print(len(r.all()),'tools', dict(collections.Counter(s.risk.name for s in r.all()))); \
print(len({x.path for x in m.app.routes if getattr(x,'methods',None) and x.path.startswith('/')}),'paths')"

# CR1, live:
JARVIS_HOME=/tmp/audit JARVIS_PORT=8931 $PY run.py &
T=$(cat /tmp/audit/api_token)
curl -s -X POST -H "X-JARVIS-Token: $T" -H "Content-Type: application/json" \
  -d '{"text":"move the robot arm 10 centimeters forward"}' http://127.0.0.1:8931/command
#   → decision CONFIRM ; approve it, then:
curl -s -H "X-JARVIS-Token: $T" "http://127.0.0.1:8931/audit?limit=1"
#   → "No motion command was sent."  but  result=success detail={"ok": true}

# CR3, live:
curl -s http://127.0.0.1:8931/auth/token           # real token, no credentials
curl -s http://127.0.0.1:8931/dashboard | grep -c "$T"   # 1 — embedded in public HTML
```

---

## 9. Open item

A second read-only security pass (memory-as-permission, injection paths,
credential leakage, failure swallowing) was still running when this was written;
its findings are appended as §10 when it returns and will not alter the
conclusions above.

**STOPPED here per instruction §35. No production code was modified. Phase 1
awaiting approval.**

---

# §10 — Second audit pass (security surface), appended after verification

A second read-only pass covered auth/routes, path safety, command execution,
memory-as-permission, injection, credentials and failure modes. Its claims were
**not** accepted on trust: each one below is marked with how I established it.

## 10.1 Confirmed by me, in this session

**CR9 — the path guard keys off parameter _names_, so two tools walk past it.**
`policy.py:208-219` inspects only params called `path`, `source`, `destination`.
`search_files` declares `directory` (`tools/files.py:473`) and `read_file`'s
sibling guards are fine; `files.py` contains just two `protection_reason` calls.
Through the real engine:

```
paths module flags it protected : True
list_directory             -> BLOCK    Library/LaunchAgents is a protected private location
read_file                  -> BLOCK    Library/LaunchAgents is a protected private location
copy_file                  -> BLOCK    Library/LaunchAgents is a protected private location
search_files               -> EXECUTE  low-risk action
run_allowlisted_command    -> CONFIRM  level 2 (external) action requires confirmation
```

And through the full pipeline, live: _"search for plist files in
~/Library/LaunchAgents"_ → `EXECUTE`, returning 6 matches from a directory the
guard exists to refuse. (Counts only; the filenames are not reproduced here.)
`run_allowlisted_command` is the same class of miss from the other side: its
params are `command`/`args`, so a protected path arrives as an argument and is
downgraded from BLOCK to a confirmation — `policy.py:132-136` states the
opposite ("refused, never merely confirmed").

**CR10 — `PrivacyManager.delete` reports an empty success when deletion fails.**
`security/privacy.py:267-275`: each table delete is wrapped in
`except Exception` which sets `removed[table] = 0` and logs at **info**, then the
call returns normally → HTTP 200 with `total_removed: 0` after a failed wipe.
For a "delete my data" command this is the worst possible failure to disguise:
the user is told there was nothing to delete.

**Confirmed also:** `LOOPBACK_HOSTS` includes `0.0.0.0` (`auth.py:36-42`) — safe
under the default loopback bind, wrong the moment `JARVIS_HOST` is set to a
public interface, because the guard checks the header rather than the socket.
`vision/screen.py:29-31` does `expanduser()` + `mkdir(parents=True)` + write with
no `protection_reason` import in the module, and its route skips policy.
`PermissionEngine.grant()` has **zero callers** in `jarvis/`, so the
standing-grant downgrade branch at `policy.py:152-158` is unreachable code.

**Memory-as-permission: the spec's requirement holds.** `evaluate()` reads only
the registry, device table and `permissions` table; memory is never an input to
any authorisation decision. This is a genuine pass, not a gap.

## 10.2 Reported by the pass, not yet independently verified

Carried as open items, not as findings: ~10 mutating routes that write no audit
record (`/devices/{id}/endpoint`, `/privacy/mode`, `/memory`, `/workspaces*`,
`/memory/reindex`); `_KV_SECRET` failing to match JSON-shaped
`"password": "…"` while params are audit-serialised as JSON; `jarvis.log`
applying no redaction while `run_command` logs full argv; raw exception text
returned over HTTP on some routes; device PSK overwrite when a pairing request
reuses a paired `device_id`. Each is plausible and cheap to test; none should be
acted on before it is measured.

## 10.3 Revised Phase 1

The scope in §6 stands, with one addition and one reordering. The path guard is
now the first thing to fix, because unlike verification it is a live
confidentiality hole rather than a truthfulness one:

1. **Path guard by value, not by parameter name** — scan every string param for
   a protected location, and treat any protected path appearing in _any_ tool
   argument (including `args`) as a refusal, matching `policy.py:132-136`.
2. Verification stage (CR1) — SUCCESS / FAILED / UNKNOWN, no inherited `True`.
3. Gate the authorisation-free dispatches (CR2), including `CommitBody.authorized`.
4. `PrivacyManager.delete` must fail loudly and report per-table truth (CR10).

Recommended Phase 1 file additions: `jarvis/security/policy.py` (`_path_guard`),
`jarvis/tools/files.py`, `jarvis/tools/system.py`, `jarvis/vision/screen.py`,
`jarvis/security/privacy.py`, plus regression tests in `tests/test_paths.py`,
`tests/test_policy.py`, `tests/test_privacy.py`, `tests/test_security.py`.

**Still no production code modified. §9 of this file is superseded by §10.**
