# V26.2 — Voice Operator integration: acceptance report

**Date:** 2026-11-19
**Branch:** `feature/v26-voice-operator-actionmesh` (worktree `/Users/abhijeetmiskin/AppData/Vantage-v26`)
**Baseline preserved:** main @ `13aea83` with 15 untouched working-tree files; `/Applications/Vantage.app` Nov 15 15:59 v0.2.0; profile DB Nov 17 17:40; ActionMesh read-only @ `823bcc9`.

Evidence is reported in the campaign's categories and **not combined**.

---

## 1. What this campaign asked, and what was answered

The critical question: *does an intended action reach the existing backend, pass authorisation, execute once, and produce a verified result?*

| Question | Answer | Category |
|---|---|---|
| Does a command reach the backend and execute? | Yes — `open Calculator` → `open_app`, mission `MIS-faa5300139df`, `verified: True` | **INTEGRATION_VERIFIED** |
| Does the claim match reality? | Yes — `pgrep -x Calculator` → pid 7527 independently | **INTEGRATION_VERIFIED** |
| Does the permission gate park a destructive step? | Yes — `awaiting_approval`, "level 3 (destructive)" | **INTEGRATION_VERIFIED** |
| Does denial prevent execution? | Yes — `blocked`, step `failed`, "denied by the operator" | **INTEGRATION_VERIFIED** |
| Does a forged confirmation work? | No — refused, and the valid id is **not** rotated | **INTEGRATION_VERIFIED** |
| Does a cancelled mission resume? | No — 404, "cancelled → awaiting_approval is not a legal transition" | **INTEGRATION_VERIFIED** |
| Does a pronoun resolve across turns? | Yes — three-turn conversation, "it" → Calculator | **INTEGRATION_VERIFIED** |
| Does the flag gate the feature? | Yes — off: `unknown`; on: `close_app` | **INTEGRATION_VERIFIED** |
| Was anything clicked in the GUI? | **No** | **NATIVE_UI_NOT_VERIFIED** |
| Was a physical desktop effect produced? | **No** | **PHYSICAL_NOT_VERIFIED** |

---

## 2. Two real defects found and fixed

### 2.1 An operator's denial could be erased

**Reproduced live before the fix:**

```
park → decide deny            → state=blocked, "denied by the operator"
POST transition {"running"}   → 200 (legal by design)
POST /run                     → state=awaiting_approval, step "pending",
                                FRESH confirmation id minted
```

The denial text was **absent from the mission entirely**, and the fresh id was **accepted**, dispatching the action the operator had refused. A refused request was exchangeable for a usable credential — the same shape already fixed for the `AWAITING_APPROVAL` case, never closed for `BLOCKED`.

**Fix** (`jarvis/core/mission_runner.py`): the park site checks for a prior denial and returns the mission instead of re-parking. It returns rather than raises, because a raised error becomes a 404 with no body — the caller could not read the record it was refused against, and the denial, the thing worth preserving, would not be in the response.

**Proven two ways:**
- the `xfail(strict=True)` that documented the gap is now a real assertion and passes (6 passed);
- mutation-tested — with `if denied:` disabled the test **fails**, restored it passes;
- live re-run: `state=blocked`, `step=failed`, `detail="denied by the operator"`, **no confirmation minted**.

### 2.2 A read contradicted itself

**Measured live:** `"is it running?"` answered *"Calculator is installed (version 12.0) and is running, but I could not verify it happened."*

Every field was honest — a read reports `verification=UNKNOWN` because it never claims to alter the world — but the composed sentence contradicted the fact it had just stated.

**Fix** (`jarvis/core/orchestrator.py`): the caveat now belongs only to a tool that changed something. The signal is the spec's risk tier (`READ` means its message *is* the observation), read from the registry the permission engine already trusts, not a hand-maintained list. A tool with no spec keeps the caveat.

**Live after:** read → *"Calculator is installed (version 12.0) and is running."* change → *"Quit Calculator, but I could not verify it happened."* 5 regression tests.

---

## 3. Two measured gaps, documented but NOT fixed

### 3.1 `/command` has no idempotency

Two identical POSTs produced **two distinct missions** (`MIS-67368905e7e6`, `MIS-b1c81dc2dca3`) and ran the action twice — sequentially and, in the worker's test, concurrently.

- `jarvis/api/app.py:1044` builds `CommandRequest` from text/device/source/context/session — no request id.
- `Idempotency-Key` / `X-Request-Id` headers are **silently ignored** (`extra=ignore`).
- `MissionStep.idempotency_key` exists but is read only when *resuming* a mission, never to suppress a repeat inbound command.

Pinned `xfail(strict=True)`, so it will XPASS and fail loudly when fixed.

**Why this matters for the Voice Operator:** the view's own guard (proved in V26) stops a double *click*. It cannot stop a caller that reconnects and resends, or two surfaces sending the same words. That is the directive's exact warning — a disabled button is not backend idempotency.

### 3.2 `/command` is not the MissionRunner path

`MissionRunner` is constructed only at `jarvis/api/missions_routes.py:478` and `jarvis/core/skill_runner.py:196`. `/command` executes actions inline and writes a mission as a **ledger stub**:

```
state=partially_completed  verdict=unknown  steps=[('S1','open_application','pending')]
```

The action genuinely ran and verified; the mission record simply never gets step or condition updates, because nothing on that path calls `mark_step` or `record_condition`.

So there are two real paths, and **the Voice Operator UI uses the first**:

| Path | Behaviour |
|---|---|
| A. `/command` (what the UI calls) | inline execution, ledger stub, no step/condition verification |
| B. `/missions/desktop` → `/run` | MissionRunner: parks for approval, records steps and conditions, verifies |

If the campaign requires path B for voice, that is a wiring change **not yet made**. Recorded in `docs/V26_2_LIVE_EVIDENCE.md` §7.

---

## 4. Live evidence (isolated profile, port 8891, flag ON)

**Three-turn conversation, session `ctx-8294`:**

| Turn | Input | Result |
|---|---|---|
| 1 | "open Calculator" | `open_app`, `verified=True`; `pgrep` → pid 7527 |
| 2 | "is it running?" | `check_agent_status`; "Calculator is installed (version 12.0) and is running." |
| 3 | "close it" | `close_app` on Calculator; `pgrep` → **gone** |

"Its" resolved to Calculator in turns 2 and 3 from verified task state, through the real backend.

**Flag A/B, same sequence:** OFF → `unknown` (pronoun not resolved); ON → `close_app`. The flag is the only difference.

**Approval lifecycle:** park → deny → blocked; forged id refused without rotating the valid one; cancel → rerun 404.

---

## 5. Tests

| Run | Result |
|---|---|
| Consolidated (18 files) | **331 passed, 1 skipped, 1 xfailed** |
| Denial guard mutation | disabled → **fails**; restored → passes |
| Composer rules (Swift, V26) | 18 checks; each guard mutation-tested |

The 1 skip needs a GUI fixture. The 1 xfail is the idempotency requirement, deliberately encoded as the property that does not yet hold.

**A test-isolation bug was found and fixed during this run:** `test_voice_diagnosis_tool` used `asyncio.get_event_loop()`, which fails once a sibling suite has closed the loop — it passed alone and failed 5/6 in sequence. Switched to `asyncio.run`. That was my test's defect, not the code's.

**Pre-existing, untouched:** `tests/test_mission_approval_refusals.py` fails 4 at HEAD (documented test-side issue), and `swift test` cannot run here (no XCTest, no Xcode — pre-existing, recorded at `PROJECT_STATUS.md:250`).

---

## 6. Multi-agent report

**Workers launched this campaign: 2**, `general-purpose`, in parallel, over disjoint files. Peak concurrency **2**. Neither edited `jarvis/` — both produced only test files, verified by `git status`.

Both findings were **independently reproduced by the lead against a live backend** before being reported: the denial erasure (park → deny → transition → run, observed the denial vanish and the new id spend) and the duplicate execution (two identical POSTs, two missions).

**Agent Teams were not available in this session**; the two were parallel subagents and are not described as teammates.

---

## 7. What remains

| Item | Status |
|---|---|
| GUI control interaction | **NATIVE_UI_NOT_VERIFIED** — needs Xcode or a GUI session with Accessibility |
| Physical desktop effect | **PHYSICAL_NOT_VERIFIED** — no executor binary built in this worktree |
| `/command` idempotency | **NOT_IMPLEMENTED** — measured, pinned, not fixed |
| Voice → MissionRunner wiring | **NOT_IMPLEMENTED** — path A in use; path B exists and is tested separately |
| Workflow drafting / Goal Contracts | **ALREADY_IN_VANTAGE** — audited in V26, not repeated |

**The native executor note:** `/missions/{id}/run` after a valid approval failed with *"the native desktop executor is not built"*. That is honest — the binary is gitignored build output and is absent in this worktree (the main checkout has it, 245528 bytes). No click was faked to make the demo look complete.