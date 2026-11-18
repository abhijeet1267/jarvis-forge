# Voice Operator v2 — compatibility & acceptance report

**Date:** 2026-11-18
**Baseline:** `main` @ `13aea83`
**Feature branch:** `feature/voice-operator-actionmesh` @ `af992b0`
**Worktree:** `/Users/abhijeetmiskin/AppData/Vantage-voice-operator`
**ActionMesh:** read-only clone of `823bcc9`, MIT-licensed. Never modified, never merged.
**Feature flag:** `JARVIS_VOICE_OPERATOR_V2_ENABLED` (field `voice_operator_v2_enabled`), default **false**

---

## 1. What was actually built

Two capabilities, and only two, because the audit found the rest already present.

| # | Capability | Where | Status |
|---|---|---|---|
| 1 | Session-scoped entity memory, fed only by `verified=True` results | `jarvis/voice/entities.py` (208 L) | IMPLEMENTED, VERIFIED_IN_ISOLATION |
| 2 | Pronoun resolution before planning, flag-gated at both call sites | `jarvis/voice/resolve.py` (127 L) + `jarvis/core/orchestrator.py` | IMPLEMENTED, VERIFIED_IN_ISOLATION |
| 3 | Mission failure diagnosis from recorded steps | `jarvis/core/mission_diagnosis.py` (407 L) | IMPLEMENTED, VERIFIED_IN_ISOLATION |

**Not built, deliberately:** workflow drafts, goal contracts, file previews, evidence views, per-purpose routing, Electron shell, second database. All are either already in Vantage or forbidden by the directive. See `ACTIONMESH_OVERLAP_AUDIT.md`.

---

## 2. The security property, stated precisely

The resolver **disambiguates text; it does not authorize anything.**

* An entity is recorded only when a handler reported `verified=True`. For `open_application` that flag is set only after `_process_observed()` found the process in the process table (`jarvis/tools/apps.py:295-305`).
* A launch that could not confirm the app returns `unverified(...)`, which yields `verification=UNKNOWN` → `verified=False`. That is stored but **never** returned by `last_verified_of_kind`, so a later "close it" cannot name an app nobody saw running.
* The rewrite happens **before** `planner.parse()`. The resolved sentence therefore goes through the same ToolSpec validation, the same `policy.evaluate()`, the same confirmation path and the same MissionRunner as a typed sentence.
* `EntityMemory` exposes no approval-shaped method — asserted by test, not by assertion in prose.

---

## 3. Test evidence (all run in this session, this worktree)

| Suite | Command | Result |
|---|---|---|
| New tests, all five files | `pytest tests/test_voice_{resolve,entities,operator_v2}.py tests/test_mission_diagnosis.py tests/test_voice_operator_adversarial.py` | **79 passed** |
| Planner (flag-off baseline) | `pytest tests/test_desktop_planner.py tests/test_voice.py` | **132 passed, 1 skipped** |
| Command Center | `pytest tests/test_command_center_acceptance.py` | **7 passed** |
| Mission contracts + runner | `pytest tests/test_mission_contracts.py tests/test_mission_runner.py` | **65 passed** (4 failures are in a third file; see below) |

### The four failures, and why they are not this change

`tests/test_mission_approval_refusals.py` reports **4 failed / 19 passed / 1 skipped**. The identical result — same four tests, same counts — was reproduced on a throwaway worktree at pristine `13aea83` with the file restored from git. This branch never edits that file (`git diff HEAD -- tests/test_mission_approval_refusals.py` is empty).

Root cause, stated plainly: the **main working tree** carries an uncommitted test-side fix (adding a required `content` param and `.resolve(id, True)`), and there the suite is **24 passed**. That fix is the user's uncommitted work; the feature branch was correctly cut from HEAD, which lacks it. My change is not implicated in either direction.

---

## 4. Flag-off behaviour (Mode A)

`voice_operator_v2_enabled` defaults to `False`. `test_flag_defaults_off` pins it. Both call sites retain the `getattr(self.settings, ...)` guard, asserted by `test_flag_off_leaves_memory_inert`. With the flag off the resolver is never called, so the planner's existing pronoun refusal is the unchanged behaviour. The 132 planner/voice tests pass unchanged.

**Honest limitation:** I verified the flag-off path by unit and integration tests, not by driving the installed application.

---

## 5. Mode B (flag on) — what is and is not proven

**Proven by test:**
* a verified launch resolves "close it" → "close Calculator" (`test_verified_open_is_remembered`)
* an unverified launch does not (`test_unverified_open_is_not_remembered`)
* a failed launch does not (`test_failed_open_is_not_remembered`)
* sessions and kinds are isolated
* a memory fault cannot break a command that succeeded

**NOT proven:** a live spoken sentence through the microphone driving a real application. That requires Accessibility for a GUI session, which the earlier campaign recorded as PERMISSION_BLOCKED and which this campaign did not attempt to circumvent. `test_mode_b_live_voice` is therefore **NOT TESTED**, not "passing".

---

## 6. Installation and profile — untouched, with one honest correction

| Item | Baseline | Now |
|---|---|---|
| `/Applications/Vantage.app` | `Nov 15 15:59`, v0.2.0 | **unchanged** |
| Profile DB `~/.jarvis-forge/jarvis.db` | `Nov 17 17:40`, 856064 B | **unchanged** (mtime identical) |
| Main checkout working tree | 11 modified + 1 untracked | **14 entries, untouched by me** |

**Correction I owe the reader:** the SwiftUI app process I saw at baseline (pid 84115) is **gone**, and a backend launched from the *checkout* (`run.py serve --port 8765`, pid 92030, parent 1, started 22:55) now holds port 8765. I did not start it, and it is not mine:

* its cwd is `/Users/abhijeetmiskin/AppData/Vantage` (the main checkout, not my worktree);
* it holds **0** open files under `Vantage-voice-operator`;
* my own test backends all used ports 88xx, all of which are gone.

So the installed app and profile are untouched, but the thing answering on 8765 right now is a checkout backend, not the app's bundled one. That is the state I found, and I am reporting it rather than quietly treating it as baseline.

---

## 7. Rollback

Delete the branch/worktree, or set `JARVIS_VOICE_OPERATOR_V2_ENABLED=0` (the default). No migration, no schema change, no persisted memory — `EntityMemory` is in-process only. Rollback needs no reinstall and no data restore.

---

## 8. Classification

| Gate | Status |
|---|---|
| A — Vantage preservation | **VERIFIED_IN_ISOLATION** (installed app and DB unchanged; flag-off tests green) |
| B — Voice Operator end-to-end | **NOT TESTED** (no live microphone run; unit + integration only) |
| C — Conversation intelligence | **VERIFIED_IN_ISOLATION** (resolver tests) |
| D — ActionMesh enhancements without duplicate engines | **VERIFIED_IN_ISOLATION** (audit + 2 additions) |
| E — Security / no new bypass | **VERIFIED_IN_ISOLATION** (13 adversarial tests) |
| F — Native SwiftUI product experience | **NOT_IMPLEMENTED** (no UI work attempted this pass) |
| G — Rollback | **VERIFIED_IN_ISOLATION** (flag default off; in-process memory) |

Nothing in this report is GUI_VERIFIED. Nothing is INTEGRATION_VERIFIED against the installed app.