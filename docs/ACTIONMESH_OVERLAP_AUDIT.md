# ActionMesh → Vantage overlap audit

**Date:** 2026-11-18
**ActionMesh source:** `github.com/abhijeetmiskin1/ActionMesh` @ `823bcc9` (cloned read-only to `/tmp/actionmesh-audit`, never modified)
**Vantage baseline:** `main` @ `13aea83`, working tree carrying 11 pre-existing modified files (not mine, preserved)
**License:** ActionMesh is **MIT**, not Apache-2.0. The directive's §22 assumes Apache-2.0; the difference matters because MIT requires only the copyright/permission notice be included. Recorded here so the decision is made on the real terms.

This audit was done by reading both codebases. No feature is classified from a README.

---

## Headline

The directive assumes Vantage is missing a large amount of what ActionMesh has. Reading the code, **most of it is already in Vantage** — often in a stronger form. The genuinely missing surface is narrow, and it is one thing, not ten: **conversation-bound entity resolution**.

Vantage's planner *deliberately refuses* vague pronouns today:

```
jarvis/core/planner.py:1484   # Never infer a broad target from a vague pronoun.
jarvis/core/planner.py:2521   if vague in {"", "this", "that", "it", "these", "those", "the"}:
```

That refusal is correct for a stateless command line. It is what makes "Close it." impossible after "Open Calculator." — the one capability in the directive that Vantage genuinely lacks and that ActionMesh demonstrates working.

---

## Overlap matrix

| # | Capability | ActionMesh source | Vantage today | Class |
|---|---|---|---|---|
| 1 | Mission/step state machine | `voiceos/models.py` `TaskSession`, step states | `jarvis/core/missions.py` — `MissionState` enum + declared `TRANSITIONS` table, 12 states incl. `PARTIALLY_COMPLETED` | **ALREADY_IN_VANTAGE** (Vantage stronger: transitions are data, testable) |
| 2 | Verified vs unverified outcome | `orchestrator.py` `succeeded_unverified`, `needs_review` | `jarvis/core/mission_runner.py` `Verification`, `StepOutcome`, 5 real verifiers (file_contains/hash/identical/absent/always_unknown) | **ALREADY_IN_VANTAGE** |
| 3 | Crash-safe step checkpoint | `orchestrator.advance(checkpoint=…)` — names an execution id *before* awaiting | MissionRunner persists step state per step; approval parking keeps the confirmation id (`mission_runner.py` D2 fix) | **ALREADY_IN_VANTAGE** (different mechanism, same invariant) |
| 4 | Approval parking mid-mission | `awaiting_approval` step state | `MissionState.AWAITING_APPROVAL` + `pendingDesktopApproval` in the Swift client | **ALREADY_IN_VANTAGE** |
| 5 | Permission enforcement | `core/permissions/` | `jarvis/security/policy.py`, `permissions/`, ToolSpec parameter declaration | **ALREADY_IN_VANTAGE** |
| 6 | Voice input / STT / TTS / wake word | `voiceos/` transport | `jarvis/voice/{session,stt,tts,wakeword,audio}.py` | **ALREADY_IN_VANTAGE** |
| 7 | Session id + pending confirmation + last turn | `TaskSession` | `VoiceSession.session_id`, `.pending_confirmation`, `.awaiting_followup`, `.last_turn` | **ALREADY_IN_VANTAGE** |
| 8 | **Contextual entity resolution ("it"/"that")** | `voiceos/targeting.py` (351 L), `reasoning.py` | **Absent by design** — planner refuses pronouns (`planner.py:1484,2521`) | **ADD_NEW_FEATURE** ← the real gap |
| 9 | Mission-failure diagnosis | `voiceos/diagnosis.py` (466 L) | `planner._explain_failure` only handles *project* lookup (`planner.py:1266`) | **ADD_NEW_FEATURE** (small, bounded) |
| 10 | Workflow recording → draft | `voiceos/workflow_draft.py` | `jarvis/core/recorder.py` + `POST /recordings/draft` ("The reviewable workflow draft") | **ALREADY_IN_VANTAGE** |
| 11 | Safe file preview (ParallelWorld) | `core/graph/` shadow | `POST /files/preview`, `/apply`, `/undo`, `/organize-plan` | **ALREADY_IN_VANTAGE** |
| 12 | Goal contracts / monitors | `voiceos/project_goals.py` | `jarvis/core/goalguard.py` — `Goal`, `GoalStore`, `record_observation`, `due()` | **ALREADY_IN_VANTAGE** |
| 13 | Per-purpose model routing | desktop provider concepts | `jarvis/gateway/provider_categories.py` (`CATEGORIES`), `routing.py`, `selection.py` | **ALREADY_IN_VANTAGE** |
| 14 | Evidence / receipt views | ProofLens | Mission verification + `docs/APPROVAL_PROVENANCE.md` | **ALREADY_IN_VANTAGE** |
| 15 | Multi-computer target switching | `targeting.py` "switch to <computer>" | Vantage is single-host; its executor targets apps/windows, not machines | **REJECT_AS_DUPLICATE** (no second machine in scope) |
| 16 | Electron desktop shell | `desktop/` | Native SwiftUI (`macos/Vantage/`) | **REJECT_AS_DUPLICATE** (directive forbids it) |
| 17 | ActionMesh SQLite store | `voiceos/store.py` (168 L) | Vantage SQLite via `jarvis/db/` | **REJECT_AS_DUPLICATE** (directive forbids a second DB) |
| 18 | Browser/process providers | `actionmesh/agent/` | `jarvis/tools/browser_*`, process management, ownership checks | **ALREADY_IN_VANTAGE** |

---

## What this means for the campaign

The directive's Phases 5 (workflow drafts, goal contracts, parallel previews, evidence views, purpose routing) are **already satisfied**. Porting them would create the duplicate engines §0 forbids.

The work that is genuinely additive and safe:

1. **Contextual reference resolution** (#8) — a session-scoped entity memory that lets the planner resolve "it"/"that"/"the app I just opened" from *verified* task state, refusing when ambiguous. Must not weaken `planner.py`'s existing pronoun refusal; it replaces "always refuse" with "resolve from evidence, else refuse."
2. **Mission failure diagnosis** (#9) — explain a failed mission from its recorded steps, without inventing causes.

Both sit behind a flag and neither touches the execution path: the resolver only *rewrites a reference into a concrete entity* which then travels the existing planner → ToolSpec → PermissionEngine → MissionRunner path.

---

## Test-coverage note (from ActionMesh, for honest provenance)

`voiceos/diagnosis.py` has no dedicated test file; it is exercised indirectly by `tests/unit/test_{voiceos,llm_generation,model_bridge,cli}.py`. `targeting.py` has 1 referencing test file. So ActionMesh's entity-resolution idea is **less** tested than the parts of Vantage it would sit beside. This campaign should write its own tests rather than borrow confidence.

---

## Classification summary

| Class | Count |
|---|---|
| ALREADY_IN_VANTAGE | 13 |
| ADD_NEW_FEATURE | 2 |
| REJECT_AS_DUPLICATE | 3 |
| IMPROVE_EXISTING | 0 (the two additions are new modules, not edits to existing ones) |
| NOT_YET_IMPLEMENTED_IN_ACTIONMESH | 0 |
| EXTERNALLY_BLOCKED | 0 |