# Phase 33 — a question must not ask for something the system already holds

Measured on this tree, this machine, 2026-10-01. Continues the arc of Phases 30–32: what a
product knows should not be turned back into work for the person using it.

---

## 1. Measured before anything was changed

Phase 30 made capabilities reachable by name and left **55** of them answering with a
question. Walking those questions showed what they ask for:

```
capabilities that answer a plain request with a question:            55
missing parameters that are identifiers the system issued itself:
    workspace_id ×8, paper_id ×6, target_device ×4, asset_id ×3,
    schedule_id ×3, memory_id ×2, uid ×3 ...

tools registered that enumerate those identifiers with no arguments:  13
    list_workspaces, list_schedules, list_devices, list_papers,
    list_design_assets, list_events, search_email, ...
```

And the wording the user gets:

```
close the workspace  -> 'Which workspace? Give me its id (WS-…).'
inspect memory       -> '…say it with the name or id, like "inspect memory <which one>"?'
pause the schedule   -> 'Which schedule? Give me its id (SCHED-…), or say "list my schedules"…'
```

`list_workspaces` returns `{'workspaces': [{'workspace_id': 'WS-301adce395d5', 'name':
'shelf-…'}]}`. The product holds the identifier, prints it on request, and then asks the
person to type it back. An id shaped like `WS-301adce395d5` is not something a human
remembers; it is something a tool prints.

Two measurements shaped the design rather than intuition:

* the enumerators' **messages** do not contain the ids. `list_workspaces` says "1
  workspace(s) open." — the id lives only in the structured `output`. So the candidates have
  to come from the result shape, which means the shape has to be *declared*, not parsed by
  guesswork;
* `search_memory` refuses an empty query. Some identifiers genuinely have no
  no-argument source, and for those the plain question is still the honest answer. Building
  a listing tool to make an assertion come true would have been this program's oldest error
  wearing a new face.

## 2. What changed

**`ToolSpec.provides_identifiers`** — declared on the *enumerator*, not on the dozens of
tools that consume the id:

```python
name="list_workspaces", ...
provides_identifiers={"workspace_id": {"items": "workspaces",
                                       "id": "workspace_id", "label": "name"}}
```

The thing that hands an identifier out is the thing that knows its shape. Declaring it on
each consumer would have been eight copies of one fact, which is how Phase 32's two
vocabularies happened in the first place. Seven tools declare now: workspaces, schedules,
devices, papers, design assets, email messages, calendar events.

**`ToolRegistry.identifier_source(param, agent)`** returns which enumerator prints an id,
admitting only a READ-tier tool that requires nothing of its own — a lookup that exists to
spare the user a chore must not be able to change anything, and must not itself need an
answer first. `uid` belongs to both an email and a calendar event, so the asking tool's own
agent picks between them and the tie-break is by name, never by dict order.

**The orchestrator offers, and never chooses.** `_offer_identifiers` runs before either
reply branch, because a route's question arrives as `direct_reply` and the planner's as
`clarification_question` -- both are questions. It appends at most four candidates
(`IDENTIFIER_OFFER_LIMIT`, the same reasoning as `MAX_READ_EXCERPT`: this text is also what
gets remembered):

```
close the workspace -> Which workspace? Give me its id (WS-…).
                       Here is what I have: shelf-2c0axpr3 (WS-301adce395d5).
```

No action is emitted, the sentence still ends in a question, and the lookup is attributed in
the ledger as `identifier_sources_used` -- the same attribution debt R-14 settled for a
fallback that *answers* with another tool's result (26-B). A lookup that fails says "I asked
list_workspaces and it could not tell me: …", and one that finds nothing says "I checked,
and there is nothing of that kind right now." Silence is never padded and an empty shelf is
never reported as a result.

**Route-level asks declare what they need too.** `_coding` ("Which workspace?") and
`_scheduler` ("Which schedule?") were written before this mechanism; they now state their
missing identifier, so the offer is not a favour the new route does for itself.

**One verb substitution found on the way.** Writing the test surfaced this, measured live:

```
"close the workspace WS-e3c166a91c5b"
    tool=inspect_workspace
    reply='p33-shelf-me0lki7h: 0 source file(s), 0 test file(s). Languages: unknown.'
```

A request to *close* answered with a *description*, recorded as a success, with nothing in
the reply or the chain saying the two verbs differ. The coding route's default branch is an
inspect and it claimed the sentence because the word "workspace" was in it. Fixed by
applying Phase 31's rule to a default rather than only to an advertisement: a route that
falls through to a catch-all yields to a sentence that names a specific capability.

## 3. Live, over HTTP, on a launched server

`python run.py serve`, temporary `JARVIS_HOME`, sandboxed `HOME`, token auth on; one
workspace opened over HTTP first.

```
=== questions that used to demand an id the system already had ===
  "close the workspace" -> tools=[]
      Which workspace? Give me its id (WS-…). Here is what I have: p33-shelf-me0lki7h (WS-e3c166a91c5b).
  "list the workspace"  -> tools=[]
      Which workspace? Give me its id (WS-…). Here is what I have: p33-shelf-me0lki7h (WS-e3c166a91c5b).
  "pause the schedule"  -> tools=[]
      Which schedule? … I checked, and there is nothing of that kind right now.

=== the ledger says which read informed each question ===
   no_action  clarification  src=['list_workspaces']   close the workspace
   no_action  clarification  src=['list_schedules']    pause the schedule
   no_action  clarification  src=-                     forget paper
9 rows · rows leaking a schema name: 0
```

`"forget paper"` still gets a bare question — measured, that sentence is claimed by the
research route, which asks for a topic. Recorded in §7 rather than worked around.

## 4. Findings this phase made about itself

1. **`search_memory` cannot answer "which memory?"**, because it refuses an empty query and
   no other tool lists memories with their ids. The honest fix was to leave that identifier
   without an offer; the tempting one was to invent an enumerator to make the sweep green.
2. **The offer is a read, so it inherits every rule about reads.** That is what makes
   "list, then pick" safe rather than a way to run arbitrary work while composing a
   sentence — hence the READ-tier/no-required-params admission test and the ledger
   attribution, not just the happy path.
3. **The suite had a leak that only appeared in one run order.** `test_phase30_…` installs
   counting handlers and registers test capabilities, and its docstring said they were
   "restored by the autouse fixture". No such fixture existed. So `list_workspaces` was a
   stub for the rest of the session, and Phase 33's file passed alone and failed inside the
   full run — the worst shape a test signal can have, because both results are defensible.
   Four phase files (30–33) now snapshot and restore `_specs` and `_handlers`, including the
   nested dicts, because Phase 32's synonym test and Phase 33's declaration test both mutate
   those in place. This was found by running the new file *after* the old one, not by
   reading either: a suite that passes in one order and fails in another is not measuring
   the code.
4. **Verb substitution inside one domain was still live** after three phases of
   substitution fixes, and it survived because the action it chose was read-only and
   therefore harmless-looking. "It only read something" is not a reason the reply may
   describe a different verb than the one used.

## 5. Regressions

`tests/test_phase33_questions_that_carry_their_own_answer.py`: 21 tests, written red first
(20 failing on the first run). Includes the registry-mutation test in both directions (add
a declaration → the offer appears; delete it → the question goes back to plain), the
same-agent tie-break on registered twins, the bounded list with its "+N more", the failing
lookup, the empty shelf, the "id already supplied means no round trip" case, and a
registry-wide invariant sweep asserting every declared source is READ-tier, needs no
arguments, and names `items`/`id`/`label`.

No existing test was weakened. `tests/test_phase30_capabilities_are_reachable.py` got the
missing autouse fixture its own docstring promised; its 68 tests pass unchanged.

Full suite: **1642 passed, 0 failed, 0 xfailed** (1642 collected), up from 1621 — run three
times in a row green, after the ordering fix.

## 6. Re-measured

| metric | before | after | how |
| --- | --- | --- | --- |
| capabilities answered with a question | 55 | 55 | registry walk (this phase answers them, it does not remove them) |
| …of which the question now carries real candidates | **0** | **19** | same walk with one workspace open |
| …still asking plainly, no zero-argument source exists | — | 36 | same walk |
| tools declaring an identifier source | 0 | 7 | `grep -c provides_identifiers jarvis/tools/*.py` |
| "close the workspace WS-…" | answered with an *inspect* and a file count | reaches `close_workspace` | measured live, then tested |
| ledger rows attributing the informing read | 0 | present (`identifier_sources_used`) | `/audit` over HTTP |
| §45 safety matrix | 6 of 6 | **6 of 6** | `python benchmarks/safety_run.py` |
| §59 acceptance sweep | 17 of 18 | unchanged | `python benchmarks/acceptance_run.py` |
| tools / agents / api routes / confirm-gated / tables | 127 / 15 / 104 / 27 / 32 | unchanged | `python assets/build_diagrams.py` |
| suite | 1621 | 1642 | `python -m pytest -q` |

## 7. Carried forward

* **33-A** 36 of the 55 questions still ask for something with no declared source:
  `memory_id` (no enumerator answers without a query), `query`, `title`, `path`, `claim`,
  `message`, `kind` — values a person legitimately has rather than ids the system issued.
  Offering those would mean reading the user's own data into a question, which is a privacy
  conversation, not a plumbing one.
* **33-B** sentences claimed by a specific route never reach the offer: "forget paper" is
  asked for a research topic by `_research`. The offer and the route's own questions are two
  mechanisms, and only the fallback-level one speaks it.
* **33-C** the offer lists candidates but cannot act on the answer yet: "close the
  workspace" → "shelf-x (WS-e3c…)" → the user still has to say the id back, or its name,
  which no route resolves. Matching a candidate by name in the follow-up turn is the next
  step, and it needs the resolver to accept a *label* — which is precisely the thing Phase
  31 made actuation refuse to do loosely. A read may match a label; an actuation still needs
  the confirmation that names the device.
* Everything already open: 32-A…C, 31-A/31-B (closed in 32), 30-B…D, 29-A…D, 28-A narrowed,
  27-A…D, 26-B, the `ToolSpec.parameters` residual, 23-E, mic/speech TCC, 15-F, 14-D, 16-F,
  hardware **BLOCKED**, LLM planner, unattended workflows.

---

## 8. Git

Committed and pushed to `main` on `abhijeet1267/jarvis-forge`:

```
253bce5  A question offers the identifiers the system already holds
         23 files changed, 888 insertions(+), 37 deletions(-)
```

### The runner's verdict, measured after the push

Run `36887074627`, both jobs green:

| job | result | pytest line | wall |
| --- | --- | --- | --- |
| `suite (3.11)` | success | `1636 passed, 6 skipped, 1 warning in 84.33s` | 15:48:08 → 15:50:12Z |
| `suite (3.12)` | success | `1636 passed, 6 skipped, 1 warning in 81.38s` | 15:48:07 → 15:49:54Z |

`1636 + 6 = 1642` — the same collection as this laptop, and the six are the six accounted
for in Phase 29 (five battery-gated, one `dist/`-gated), so none of this phase's 21 tests was
skipped on either image.

That equality is the point of this particular commit. The defect the phase found in its own
test suite was *order*-dependent, not machine-dependent: a stub handler left installed by one
file made another file pass alone and fail inside a full run. A single green local run of one
file would have proved nothing about that, which is why §5 records three consecutive full-suite
runs here rather than one, and why the runner's full-suite count matching the local count is
reported as a measurement instead of assumed.

The runner also re-ran the plan's §45 matrix in its benchmark step, and it still holds 6 of 6
with the heater row refused — nothing in this phase touched the permission tiers, and that is
now also verified on a machine that did not write it.
