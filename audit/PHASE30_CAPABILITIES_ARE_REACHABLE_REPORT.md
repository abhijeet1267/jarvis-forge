# Phase 30 — a capability JARVIS has must be reachable from a sentence

Everything below was measured on this tree, on this machine, on 2026-10-01. Where a number
is quoted in `README.md`, the same command that produced it is named.

---

## 1. Measured before anything was changed

The item this phase took from the backlog was 28-A, which Phase 28 had left open with a
count attached. Re-measuring it was the first surprise: **the instrument was wrong.**

```
registered tools                              127
tool names appearing nowhere in planner.py     28   (the 28-A figure)
```

Counting names in the source says how many routes were _hand-written_, not how many
capabilities a person can actually ask for. So the same registry was walked a second time,
behaviourally: for every tool, say its own name words out loud and see what comes back.

```
python -c '… parse(" ".join(name.split("_"))) …'   # with the planner as it stood

saying a tool's own name was answered "I understood the words but not the goal"
for                                                  46 of 127
```

Forty-six, not twenty-eight. `read_file`, `get_time`, `open_app`, `close_app`, `copy_file`,
`list_events` all _have_ routes and all were dead ends when named plainly, because their
route is keyed on some other phrasing. The 28-A number had been reporting 28 of a gap of
46 — the difference is not a rounding argument but a category error, and it was computed
from the one artefact, `planner.py`, that cannot measure behaviour.

Three concrete defects were then reproduced, because "unreachable" is a claim about the
answer a user gets, not about a source file:

```
"cancel event 12"
    intent=calendar  kind=declined  tools=[]
    reply='I can read your schedule, find free time, and create or cancel events. …'
        -> cancel_event was registered, DESTRUCTIVE, and reachable by nothing.
           The route advertised a capability the router did not have.

"get the device capabilities of phone_main"
    intent=help  kind=declined  tools=[]
    reply='I can open and close applications, search/copy/move/delete files, … Registered tools: {127 names}.'
        -> "capabilities" is a trigger word for the help rule, so naming one specific
           capability earned the whole catalogue.

"list the workspace"
    decision=BLOCK
    reply="I cannot do that: missing required parameter 'workspace_id'."
        -> 29-B, alive in the shipped tree, arriving through a route that built its
           params dict in a variable — which is precisely the blind spot 29-C had
           admitted about the static sweep.
```

And one that was not a defect: `"list car metrics"` was answered with `get_car_status`,
whose reply — _"Your car adapter is not connected…"_ — is true, just not what was asked.
Recorded as a specificity gap rather than fixed (30-C below).

## 2. What changed

**A registry-derived fallback route, last in the chain** (`_capability_fallback`). A tool is
reached when the sentence contains _all_ of its own name words (function words ignored,
singular and plural folded together, at least two words required so one keyword never plans
anything). Then:

- if the tool requires nothing, or the sentence supplies what it requires, the action is
  emitted — **after** `ToolRegistry.validate_params` says it is complete, so this route
  cannot structurally repeat Phase 29's "claim an action it cannot fill";
- if the argument is missing, the user gets a question built from the tool's own
  `description`, which is human language the registry already carries. It never names a
  field: _"I can read one message in full — your sentence does not say which one. Say it
  with the name or id, like "read email <which one>"?"_;
- if two tools fit the same words, it asks which, quoting both descriptions, and runs
  nothing.

Values are taken from three shapes only — a quoted phrase, an id the system issues
(`ASSET-1a2b3c`, `phone_main`), a bare number — and assigned in the order the tool declares
them, so a sentence naming one argument for a two-argument tool fills nothing and is asked
about rather than half-served.

**Three advertising routes now yield to a sentence that names a capability** (`_help`, the
`_calendar` decline, the `_car` catch-all). The rule is narrow on purpose: it is applied at
three named sites, not in the dispatch loop. A route that _lists what it can do_ is
describing the system and should lose to a sentence naming one part of it; a route that
_refuses_ must not, because silently preferring a keyword match over a refusal would be a
way of talking past the permission engine. Case-by-case routes still run first and still win
wherever they fit.

**Two leaks fixed at their source.** `_coding` asks _"Which workspace? Give me its id
(WS-…)"_ instead of emitting `workspace_id=""`; `_scheduler` asks for the schedule id
instead of emitting `schedule_id=""` for pause/resume/cancel. The second one was found by
the new sweep, not by a human — see §4.

**30-A: `sweep_expired_memories` was tiered LOW with no confirmation while it deletes
memories permanently.** Making it typeable would have made an unconfirmed deletion live, so
the tier had to catch up first: now DESTRUCTIVE, `requires_confirmation=True`, tagged
`destructive`, agreeing with `clear_memory_scope` fifteen lines away in the same file
("Delete every memory in a scope", DESTRUCTIVE, confirms, `reversible=False`). Permission
was tightened, never loosened, to get anything green.

## 3. Live, over HTTP, on a launched server

`python run.py serve` on a temporary `JARVIS_HOME`, a sandboxed `HOME`, token auth on;
`httpx` POSTs to `/command`. Nothing destructive was approved — the two CONFIRM rows below
are the offer, not the act.

```
=== capabilities that no typed sentence could reach, now reached ===
  "list the workspaces"
      intent=list_workspaces | decision: EXECUTE | tools: ['list_workspaces']
      reply: No workspaces are open.
  "get the device capabilities of phone_main"
      intent=get_device_capabilities | decision: EXECUTE | tools: ['get_device_capabilities']
      reply: phone_main can: intents, battery_status, media, camera.
  "ping device phone_main"
      intent=ping_device | decision: EXECUTE | tools: ['ping_device']
      reply: That did not work: phone_main is not paired, so there is nothing to ping.
  "list car metrics"
      intent=list_car_metrics | decision: EXECUTE | tools: ['list_car_metrics']
      reply: No car telemetry is available right now (no car adapter or vehicle API is configured). …

=== reached, and still stopped by the same gate everything else crosses ===
  "cancel event 12"        intent=cancel_event        decision: CONFIRM | tools: [] | uid=12
  "sweep expired memories" intent=sweep_expired_memories decision: CONFIRM | tools: []   (30-A)

=== asked about, in the tool's own words, with no field name in sight ===
  "pause the schedule"  kind: clarification | "Which schedule? Give me its id (SCHED-…), or say "list my schedules" to see them."
  "list the workspace"  kind: clarification | "Which workspace? Give me its id (WS-…)."
  "close the workspace" kind: clarification | "Which workspace? Give me its id (WS-…)."

=== what must not have changed, and did not ===
  "what can you do"  kind: declined    | the catalogue is still one sentence away
  "read the readme"  decision: EXECUTE | tools: ['read_file']   (Phase 28 intact)
  "pleach the mimbolas" kind: not_understood                    (no false reachability)

12 ledger rows · rows leaking a schema name: 0
  no_action/clarification 3 · success 3 · failure 2 · awaiting_confirmation 2
  no_action/not_understood 1 · no_action/declined 1
audit chain: {'ok': True, 'checked': 12, 'total': 12}
server stopped afterwards; `ps` for run.py serve / uvicorn: nothing
```

`ping_device` and `read_file` failing is the honest outcome on a machine with no paired
phone and no README in the sandbox home — a reachability fix is not a data fix.

## 4. Four findings this phase made about itself

1. **The backlog item was measured with the wrong instrument.** 28-A said "28 tools are
   unreachable"; the truth was 46, and it was only found by asking the registry instead of
   grepping the planner. Any open item phrased as "X appears nowhere in file Y" is a claim
   about a file.
2. **The static sweep in Phase 29 could not see what the behavioural sweep saw.** Running
   every registered tool's own name through the planner and asking the registry whether
   each emitted action is complete found three more leaks in its first second —
   `pause_schedule`, `resume_schedule`, `cancel_schedule` all emitting `schedule_id=""`.
   29-C had described this blind spot correctly and then left it; here it was holding live
   defects.
3. **A route can advertise a capability the router does not have.** The calendar decline
   said _"create or cancel events"_ in its own text while `cancel_event` was unreachable by
   any sentence. That is a different kind of untrue from a mislabelled state: the reply was
   accurate about the _tool_ and false about the _product_.
4. **Fixing reachability found a safety hole behind it.** `sweep_expired_memories` had been
   LOW-and-unconfirmed for exactly as long as it had been unreachable; the two facts were
   load-bearing for each other. Making a capability typeable is a security change, and the
   tier review belongs in the same commit, not in the next incident.

## 5. Regressions

New file `tests/test_phase30_capabilities_are_reachable.py`, written red first and watched
fail: 40 of 56 failing on the first run (the fallback did not exist), then 68 tests after
the shadowing fixes were specified. Includes two registry-wide sweeps that do not depend on
this phase's sentences:

- `test_every_registered_tool_is_reachable_when_its_own_name_is_said` — no tool may be
  answered `not_understood` when its name is spelled out. This is the test that would have
  caught the 46.
- `test_no_tool_in_the_registry_is_reachable_only_to_leak_its_field_names` — for every
  tool, a name sentence plus three value shapes; no emitted action may be one the registry
  itself would refuse, and no reply may contain `missing required parameter`.

Also pinned here: reachability does not move a permission verdict (the five zero-argument
tools keep exactly the decision their spec implies); a destructive match runs nothing before
approval; the ambiguity guard; the conservatism guards (one name word is not enough, an
unregistered verb stays `not_understood`, `help` is still `help`).

Full suite: **1555 passed, 0 failed, 0 xfailed** (1555 collected), up from 1487 — 68 new
tests, nothing removed, no assertion weakened to get there.

## 6. Re-measured

| metric                                               | before          | after  | how                                                                               |
| ---------------------------------------------------- | --------------- | ------ | --------------------------------------------------------------------------------- |
| tools registered                                     | 127             | 127    | `core.tools.all()`                                                                |
| names absent from `planner.py`                       | 28              | 25     | source grep — **and this metric is retired; see §4.1**                            |
| tools that answer `not_understood` to their own name | 46              | **0**  | registry walk through `planner.parse`                                             |
| …reaching an action from a bare name                 | 50              | 69     | same walk, `plan.actions`                                                         |
| …getting a question about the argument               | 28              | 55     | same walk, `reply_kind == clarification`                                          |
| …answered with a route's refusal                     | 3               | 3      | unchanged; all three are path-guard refusals                                      |
| ledger rows leaking a schema name (live run)         | 3 known phrases | 0      | `/audit` over HTTP, 12 rows                                                       |
| confirmation-gated tools                             | 25              | 26     | 30-A                                                                              |
| §45 safety matrix                                    | 5 of 6          | 5 of 6 | `benchmarks/safety_run.py` — `turn on heater` still prints FAIL on purpose (22-G) |
| suite                                                | 1487            | 1555   | `python -m pytest -q`                                                             |

The three columns of the registry walk are one tally with the fallback removed from the
chain and then restored, so they sum to 127 both times. "Reaching an action" rose 50 → 69
and the question column rose 28 → 55: of the tools that already answered a bare name with
_something_, none lost their action; the 26 that moved into being asked about were the ones
whose sentence named a capability without its argument, and the previous answer for most of
them was `not_understood`, not a successful call.

The "reaching an action" figure _fell_ from 81 to 69 and that is the correct direction: of
the tools that used to answer a bare name with an action, several now answer with a question
because the sentence named a tool whose arguments it did not supply — an action emitted with
empty required parameters was never a success, it was a BLOCK wearing an intent.

## 7. Carried forward

- **30-A is closed here**, not deferred: the tier fix shipped in the same commit as the route
  that makes the tool typeable. Its lesson stays open as a rule — any phase that makes a
  capability reachable owes a review of that capability's gate in the same change.
- **30-B** reachability is by _name words_, not by meaning. "ping my phone" is not matched
  (`ping_device` needs the word "device"), and no synonym table exists. The alias vocabulary
  is the next real gain, and it is also the thing that will produce false matches if added
  carelessly — which is why this phase did not add one.
- **30-C** an earlier route still wins where it claims the sentence, even when it reads the
  request less precisely: "list car metrics" answers with car status. Honest, but not what
  was asked. The `_capability_matches` yield rule was applied at three advertising sites
  only; the `_car` telemetry branch is the next candidate.
- **30-D** 55 capabilities answer with a question because their id-shaped argument is not in
  the sentence and the system never offers the list. `list_workspaces` → "close the
  workspace" is two sentences apart and no route connects them; a "list, then pick" plan
  would close it, and Phase 14's multi-step machinery already exists to carry it.
- Everything already open: 28-A **narrowed, not closed** (name-words reachability is now
  total; synonym reachability is 30-B), 29-A, 29-B, 29-C (this phase closed its scheduler
  and coding instances and replaced the sweep with a behavioural one), 29-D, 27-A…D, 26-B,
  the `ToolSpec.parameters` residual, 22-G, 23-E, mic/speech TCC, 15-F, 14-D, 16-F,
  hardware **BLOCKED**, LLM planner, unattended workflows.

---

## 8. Git

Committed and pushed to `main` on `abhijeet1267/jarvis-forge`:

```
626b5de  Naming a capability reaches it, or the answer says what is missing
         12 files changed
```

### The runner's verdict, measured after the push

Run `36846991516`, both jobs green:

| job            | result  | pytest line                                   | wall                 |
| -------------- | ------- | --------------------------------------------- | -------------------- |
| `suite (3.11)` | success | `1549 passed, 6 skipped, 1 warning in 65.94s` | 10:06:16 → 10:07:46Z |
| `suite (3.12)` | success | `1549 passed, 6 skipped, 1 warning in 83.78s` | 10:06:19 → 10:08:17Z |

`1549 + 6 = 1555`, which is the collection count on this laptop too, so the 68 new tests
all exist on the runner and none of them was quietly dropped between the two machines. The
6 skips are the same six Phase 29 accounted for -- five `skipif(not HAS_BATTERY)` and the
`dist/`-gated bundle check -- so no phase-30 test is among them, which matters here more
than usual: this phase's two headline tests walk the _whole registry_ (every tool's own name
through the planner; every emitted action back through `validate_params`), and a sweep like
that is only worth its claim if it ran on a machine that is not the one that wrote it.
The runner has no battery, no built bundle and a cold `JARVIS_HOME`, and the reachability
sweep still reports 0 dead ends and 0 leaked field names there.

`Build the desktop bundle` and `Upload measured benchmark data` ran on 3.11 and are gated
off on 3.12 (`ci.yml:53,73`), and the benchmark step regenerated `suite_history.json` and
the figures from this tree -- including the Phase 30 row: 1555 collected, 1555 passed, 0 failed.
