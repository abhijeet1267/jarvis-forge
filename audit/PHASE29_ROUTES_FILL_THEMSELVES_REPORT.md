# Phase 29 — A route fills the action it claims, or asks for what is missing

Report date: 2026-10-01. Continues Phases 9-28.

No brief. This one fell out of Phase 28's reachability sweep while checking whether
`create_flowchart` counted as "routed": it is routed, and has never once worked.

---

## 1. Measured before anything was changed

```
>>> "draw a flowchart of onboarding: request, review, approve"
    intent=create_flowchart            decision=BLOCK  tools=[]
    reply="I cannot do that: missing required parameter 'steps'."
>>> "make a diagram of the build pipeline with checkout, compile and test"
    intent=create_architecture_diagram decision=BLOCK  tools=[]
    reply="I cannot do that: missing required parameter 'components'."
>>> "notify my watch"
    intent=watch_notify                decision=BLOCK  tools=[]
    reply="I cannot do that: missing required parameter 'title'."
```

The registry says what those tools need:

```
create_flowchart             required=('title', 'steps')
create_architecture_diagram  required=('title', 'components')
send_watch_notification      required=('title',)
```

and the planner's routes sent `{"title": …}` and `params={"title": title} if title else {}`.
So a static sweep of every `Intent(tool=…, params={…})` literal against the registry found
**four** emissions that could never pass their own gate — three real (the two design tools
and the watch notification) and one false positive (`search_files`, whose dict is built in a
variable and always carries `query`).

Two things make this worse than a missing feature:

* **The content was in the sentence.** `… onboarding: request, review, approve` names three
  steps and the route dropped them. So it was not "we haven't built that yet", it was "you
  told me, and I still said no".
* **The right answer already existed, in the tool.** `create_flowchart` returns *"Which
  steps? Give me them in order, separated by commas."* and `create_architecture_diagram`
  returns *"Which components? Use 'id:Label:tier' separated by commas."* Neither was ever
  reachable, because the gate refused the action before the handler ran. A schema field name
  out-vocalled a human sentence written months earlier.

---

## 2. What changed

**Extraction.** `_listed_parts()` reads a list from where a list actually appears — after a
colon, after `with / containing / steps / components / of / for`, and splits on commas,
`and`, `then`, `&`. All-or-nothing by design:

```python
if not items or any(len(t) < 2 or len(t.split()) > 4 for t in items):
    continue        # ask, rather than draw two steps of a three-step request
```

That clause is not tidiness. Taking the tidy prefix of *"request, review, approve with review
as a decision"* produced a **2-step flowchart from a 3-step request**, reported as done —
measured while writing this. It is the same defect this program has been closing since
Phase 19 (a partial answer with nothing saying so), reimplemented by me in the fix. So the
route now asks instead, and `test_a_list_it_cannot_read_cleanly_is_asked_about_not_trimmed`
holds the line.

**Grammar.** `_diagram_args()` maps names to the builder's `id:label` form *and* pulls the
arrows out of the same list: `"api > worker, worker > database"` becomes three components
and two links. Feeding that list in as components alone produced `2 component(s),
0 link(s)` — the connections the user described, gone without a word.

**Asking.** Where a required parameter cannot be filled, the route returns a note-only
clarification, which Phase 24's taxonomy already turns into `NEEDS DETAIL` in the UI and
`no_action` + `reply_kind: clarification` in the ledger:

```
>>> "draw a flowchart of the build"
    Which steps does the flowchart show, in order? Like: request, review, approve.
>>> "make a diagram of the payment service"
    Which components should the diagram show? Like: api, worker, database.
>>> "notify my watch"
    What should the notification say?
```

**A structural guard, not a fix-per-route.** `test_no_route_in_the_planner_emits_an_action_it_cannot_fill`
walks the planner's own AST and compares each literal `params` against that tool's
`required_parameters` in the live registry. It is the measurement that opened this phase,
left running as a test, so the next route cannot make the mistake quietly.

---

## 3. Live, over HTTP, on a launched server

```
=== a sentence that names the parts now draws the artifact ===
  "draw a flowchart of onboarding: request, review, approve"
      intent=create_flowchart | decision: EXECUTE | tools: ['diagram_created']
      reply: Created diagram: flowchart of onboarding (svg) — flowchart-of-onboarding-0263950f.svg
  "make an architecture diagram of the build with api, worker, database"
      intent=create_architecture_diagram | decision: EXECUTE | tools: ['diagram_created']
      reply: Created diagram: architecture diagram of the build (svg) — …svg

=== a sentence that does not, gets asked ===
  "draw a flowchart of the build"          | kind: clarification | tools: []
  "make a diagram of the payment service"  | kind: clarification | tools: []
  "notify my watch"                        | kind: clarification | tools: []

=== nothing in the ledger answers a user with a parameter name ===
   rows leaking a schema name: 0
   - | no_action | clarification | What should the notification say?
   - | no_action | clarification | Which components should the diagram show? Like: api, …
   - | no_action | clarification | Which steps does the flowchart show, in order? Like: …
   create_architecture_diagram | success | - | Created diagram: architecture diagram of the build (
   create_flowchart            | success | - | Created diagram: flowchart of onboarding (svg) — flo
```

---

## 4. Two findings this phase made about itself

* **A regression caught before it shipped**, described above (2 steps for a 3-step request).
  Worth recording because the mechanism was quiet: my own tests passed on the cases they
  covered, and the flaw showed up only when I probed a sentence I had not written a test for.
* **`research this idea` cost 41,247 ms** in this phase's acceptance run — arXiv held the
  socket until the read timed out, and the command then answered honestly from the local
  corpus (*"arXiv could not be reached: The read operation timed out"*) and was still
  labelled VERIFIED (degraded). The *wording* has been fixed for phases; the *wait* has
  not, because nothing bounds it. Carried as **29-B**, and it is the same shape as
  everything else here: the claim at the end is honest, and the cost of getting there is
  invisible to the person waiting. The 16 commands that touch no network summed **142 ms**
  in the same run.

## 5. Regressions

`tests/test_phase29_routes_fill_their_own_action.py` — **21 tests**, red first (16 failed,
2 passed on the unfixed tree). 21 of the suite total are new; one existing test was retired
rather than weakened:

| Test | Was | Now |
|---|---|---|
| `test_phase28_…::test_a_diagram_request_names_its_internal_parameter_parked_as_28B` | asserted the parked defect: the reply must contain `components` and the decision must be BLOCK | renamed `test_a_diagram_request_no_longer_answers_with_its_parameter_name`, asserting the closed behaviour (no schema wording, no action executed, a clarification asked). Phase 28's note said "asserted rather than hidden" — the point was always that it was there to be removed when fixed |

The other two Phase 28 tests that touched diagrams needed no change; the corpus sweep in this
file is what keeps 28-B from coming back.

---

## 6. Re-measured

| | |
|---|---|
| Suite | **1487 passed · 0 failed · 0 xfailed** (1487 collected), 53 modules. In the run that produced the latency numbers above it was 1486 passed + **1 skipped**: the same `test_research.py` network gate, because arXiv answered 429 then and not since. Both are reported because the count is not stable across a network |
| Acceptance | 17 of 18 measured; median **5 ms**; **no command changed intent, state or audit token** — this phase touches sentences that were blocked, not the ones already routed |
| Acceptance total | 41,389 ms, of which `research this idea` is 41,247 ms and the other sixteen are **142 ms** (§4) |
| Safety matrix | 5 of 6 ok, `turn on heater` still FAIL (22-G, owner's call) |
| Registry | 127 tools, 25 gated, tiers unchanged; unrouted-from-a-sentence: **28** (28-A) |
| Figures | charts and diagrams regenerated; `chart-suite-growth.svg` ends at the Phase 29 point |

---

## 7. Carried forward

* **29-A** optional parameters are still not extracted: a presentation gets a fixed outline,
  not the content of *"a presentation about the quarterly results"*. Required parameters are
  now guarded structurally; this is the same sloppiness one level down.
* **29-B** no bounded read timeout on the arXiv path (measured 41.2 s).
* **29-C** the structural sweep covers `params={…}` literals only; a route assembling its
  dict in a variable is covered by behaviour tests, not static ones.
* Everything already open: 28-A (28 unreachable tools), 27-A…D, 26-B, the `ToolSpec.parameters`
  residual, 22-G, 23-E, mic/speech TCC, 15-F, 14-D, 16-F, hardware **BLOCKED**, LLM planner,
  unattended workflows.

---

## 8. Git

Committed and pushed to `main` on `abhijeet1267/jarvis-forge`. The CI verdict is appended
once the runner has produced it, not before.
