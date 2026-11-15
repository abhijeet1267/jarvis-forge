# Vantage X — Execution State (resumable checkpoint)

> Read this first when resuming. Update on meaningful state changes only.
> Current revision: HEAD after `807be6b`. Working tree clean.

## How to resume

1. `git log --oneline -12` and `git status` — confirm the tree is as described here.
2. Read `docs/engineering/VANTAGE_X_MASTER_TASK_GRAPH.json` for per-task status.
3. Pick the highest-priority task whose status is NOT_STARTED or IMPLEMENTED.
4. Run `swift build -C macos/Vantage` after any Swift change.
5. Full suite: `.venv/bin/python -m pytest -q -p no:cacheprovider`
   **Do not restart the backend while it runs** — several files read the live one.

## VERIFIED capabilities

| Capability | Evidence |
|---|---|
| Mission contracts + validated state machine | `test_mission_contracts.py` (16) |
| Permission-gated execution, CONFIRM parks awaiting approval | `test_mission_runner.py` (24) |
| PermissionLease: scope, TTL, revocation, atomic spend | `test_permission_leases.py` (17) |
| IntentShield + prompt-injection boundary | `test_intentshield.py` (49) |
| Trust Delta (widened plan needs fresh approval) | live endpoint verified |
| ProofGraph / ProofLens / Outcome Receipts | `test_proofgraph.py` (8) |
| ParallelWorld preview/apply/undo | `test_parallelworld.py` (22) |
| Crash journal + reconcile + retention | `test_journal.py` (19), wired into apply |
| Self-healing: observers, bounded proposals, **real owned repair** | `test_selfhealing.py` (18) |
| GoalGuard monitoring + goal→mission bridge | `test_goalguard.py` (21) |
| Skills engine: skill runs as a gated mission | `test_skill_runner.py` (11) |
| Parallel mission scheduler | `test_scheduler.py` (15) + runner (3) |
| Adversarial security suite | `test_security_adversarial.py` (21, no xfail) |
| Flagship showcase (real write→verify→run→recover) | `test_flagship_showcase.py` (2) |
| Skill store, validation, reliability counters | `test_skills.py` (29) |

## Security defects found and fixed

| # | Defect | Fix |
|---|---|---|
| 1 | Lease scope escaped with `..` | `_in_scope` normalises before comparing |
| 2 | Same hole in IntentShield | same normalisation |
| 3 | Symlink swapped after preview wrote through it, reported verified | symlinks refused at preview and apply |
| 4 | `consume()` without a resource bypassed scope | a use must name its resource |

## IMPLEMENTED, visual acceptance unverified

- Mission Control, ParallelWorld, Safety Center, Skills Library screens.
- All build; API payload shapes matched against live endpoints.
- **Not seen rendering** — screen capture is refused in this environment.

## BLOCKED

| Item | Blocker |
|---|---|
| Swift test target verification | no `XCTest` locally; needs a CI runner |
| Public CI / push | requires owner authorization (repo is public) |
| Android continuity | no device or emulator package installed |

## NOT STARTED

- Demonstrate-to-Automate (semantic recording → Workflow Studio)
- Multi-agent runtime lifecycle polish beyond the scheduler
- Capability Health Map, privacy boundary controller, analytics
- Release packaging / installer acceptance

## Owner actions outstanding

1. **Authorize the push** — the only way CI can validate Swift and Android.
2. **Decide the repository license** — unresolved; not to be invented.
