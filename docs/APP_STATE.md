# Vantage Mac App — what is actually in the installed build

**Installed:** /Applications/Vantage.app (rebuilt + reinstalled this session)
**Backend:** running on 127.0.0.1:8765, token at ~/.jarvis-forge/api_token

## The bug this session fixed

Five screens existed in source and the user had never seen them. The installed
binary was stale: it contained Mission Control and ParallelWorld but not Safety
Center, Skills Library or GoalGuard. `strings` on the installed app is now the
first check, not the last.

## Screens in the installed app (verified by symbol/string)

| Screen | In installed build |
|---|---|
| Home (with launch pad) | YES |
| Mission Control (with composer) | YES |
| ParallelWorld | YES |
| Safety Center (leases + Trust Delta) | YES |
| Skills Library | YES |
| GoalGuard | YES |
| Project Doctor | YES (added this session) |

## What each can do, and what it cannot yet

- **Home** — greets by time of day; launch pad of six tiles, each navigating to a
  real screen. Does not yet accept a command inline.
- **Mission Control** — create a mission (goal + acceptance condition), list,
  open detail, open ProofLens. No approve/run controls yet.
- **ParallelWorld** — build a plan, preview (no writes), apply with approval,
  undo. Verified live over HTTP.
- **Safety Center** — active leases with scope/expiry, per-lease revoke, revoke
  all (verified 1 -> 0), Trust Delta comparison.
- **Skills Library** — list skills with measured reliability, run (parks for
  approval on level-2 actions).
- **GoalGuard** — list monitored goals with real observation state, check what
  is due.
- **Project Doctor** — pick a project folder, see what is actually wrong. Every
  finding names the check, command and exit code; an unperformable check shows
  "not checked". Running the project's build/tests is a separate explicit
  button. Verified live against a genuinely broken package.json: detected the
  real parse error and refused to guess a repair.

## Not done

- Command Center still does not execute a full mission from a typed goal.
- Smart Files screen does not exist yet.
- No approve/run control in Mission Control.
- Visual acceptance never confirmed: screen capture is refused here. Every claim
  above is from binary inspection and live API responses, not from seeing pixels.
