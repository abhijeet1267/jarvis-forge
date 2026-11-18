# What a confirmation id actually proves

The deployment asks: *does possession of a confirmation id alone let an agent or
untrusted caller approve a destructive action?* Answered by measurement, not by
reading intent into the code.

## What authenticates a caller

Every protected route depends on `require_auth` (`jarvis/api/app.py:538`), which
accepts either:

- the **master API token** (`$JARVIS_HOME/api_token`, 0600), or
- a **dashboard session token**, checked against `_path_allowed(surface, path)` so
  a phone's session cannot reach a laptop's routes (`app.py:520-560`).

That is one credential for the whole backend surface. There is no per-action
identity beyond it.

## Measured: the id is not sufficient

```
park a level-3 write_file step        -> CONF-be4b26bd9c28e...
POST /missions/{id}/run with that id and NO auth header
                                      -> 401
file written                          -> False
```

So a confirmation id on its own authorises nothing. It is a **selector**, not a
capability: it names which parked decision is meant, and the token says whether
the caller may speak at all.

## Measured: the id alone is also not enough at the policy layer

The generic confirmation hub (`POST /confirm/{id}`) shares a store with the
mission runner. A mission-owned confirmation resolved through the hub skipped the
plan-revision check, so an approval parked at revision 0 could execute a step's
*original* params after a steer had replaced them. That is now refused in
`policy.resolve` on `requested_surface == "mission"`, a field the mission gate
writes itself. Regression test mutates the guard and fails 4 tests.

## The honest conclusion about "human approval"

**Any process holding the master token can approve anything.** A model-driven
tool call runs inside the backend, so it can: request a run, read the
confirmation id from the response, and post it back. Nothing in the protocol
distinguishes that from a person pressing Approve in the app.

This is not a defect that can be fixed inside the approval system — it is what a
single-tenant local backend with one master token *is*. What the current design
does guarantee is narrower and worth stating plainly:

1. **No silent execution.** A gated step cannot run without a confirmation
   existing and being presented. The previously shipped client behaviour (scrape
   the id, repost it, never show a plan) is gone; the app now holds the plan in
   `pendingDesktopApproval` until a person answers.
2. **The id cannot be smuggled or exchanged.** A wrong id neither authorises nor
   rotates the live one; a denial cannot be erased; a pre-steer approval cannot
   execute a post-steer plan.
3. **A tool that merely echoes an id it was handed still cannot reach a step
   whose revision, params or target have changed** — that is enforced by the
   runner, the intent scope and the lease, not by the caller's good faith.

What it does **not** guarantee: that a human saw anything. If you want that,
the token has to be held by something a model cannot reach — a separate
approval channel with its own credential — which is a design change, not a bug
fix. Recorded here so no later report claims human consent the architecture
cannot provide.

## Test coverage

See `tests/test_mission_approval_refusals.py` (24 passing): missing approval,
forged id, another mission's id, empty/whitespace id, the hub path, denial
persistence, steering + fresh approval, and the "no side effect" assertions that
each end in a filesystem fact.
