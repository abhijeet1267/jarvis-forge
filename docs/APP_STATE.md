# Vantage Mac App — current state

**Installed:** /Applications/Vantage.app (rebuilt this session)
**Backend:** 127.0.0.1:8765
**Suite:** 4140 passed / 30 skipped / 0 failed

## What actually works, verified against the live backend

| Screen | What you can do |
|---|---|
| **Home** | Greeting, launch pad of tiles → every real screen |
| **Command Center** | Type a command; it runs real tools **and records a mission** that settles to a final state |
| **Mission Control** | Create a mission (goal + acceptance condition), run it, approve a parked one, **approve or deny individual steps**, open ProofLens |
| **Smart Files** | Pick a folder, find duplicates, see the largest files, propose organise-by-type, **bulk rename with collisions reported** |
| **ParallelWorld** | Build operations, preview (never writes), apply with approval, **undo — including moves** |
| **Safety Center** | Active leases with scope/expiry, revoke one, revoke all, Trust Delta |
| **Skills Library** | Run a skill (parks for approval on protected steps); measured reliability |
| **GoalGuard** | Monitored goals with honest observation state; check what is due |
| **Project Doctor** | Inspect a project, see what is actually wrong, **repair corrections JSON forces**, refuse ambiguous ones |

## Fixes this session that changed behaviour

1. **The installed app had 2 of 5 screens.** Source had them; the bundle did not.
2. **Commands vanished.** `/command` ran tools and left no record — now every command records a mission.
3. **Missions were stuck in `running` forever** after their command finished. Now every exit path settles them.
4. **Smart Files couldn't run.** It proposed `move_file`; ParallelWorld knew only `create`/`replace`/`append`/`delete`. Moves are now a real, reversible operation.
5. **Approving a step re-parked it forever** — the parked confirmation id was never recorded anywhere durable.
6. **A move looked successful but journalled nothing**, so undo could not reverse it.

## Not done

- **I have never seen the UI render.** Screen capture is refused in this environment.
  Everything above rests on compiled symbols and live API responses.
- Swift tests never run (no XCTest here). Nothing pushed; CI has validated none of it.
- No Mac Control screen (open apps / workspaces).
- No Automation Studio UI (the semantic recorder backend exists).
- No per-step *execution* progress streaming; the screen refreshes on demand.
