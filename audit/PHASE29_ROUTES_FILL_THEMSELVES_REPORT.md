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
* **29-D** the bundle-freshness guard is skipped by every CI run: `dist/` is gitignored and
  the workflow builds the bundle after the test step. Found while accounting for the
  runner's skip count in §8, which is the kind of bookkeeping that only looks like
  bookkeeping. Reasoning in §8.
* Everything already open: 28-A (28 unreachable tools), 27-A…D, 26-B, the `ToolSpec.parameters`
  residual, 22-G, 23-E, mic/speech TCC, 15-F, 14-D, 16-F, hardware **BLOCKED**, LLM planner,
  unattended workflows.

---

## 8. Git

Committed and pushed to `main` on `abhijeet1267/jarvis-forge`:

```
6956984  A planner route must fill the action it emits
         11 files changed, 634 insertions(+), 61 deletions(-)
```

### The runner's verdict, measured after the push

Run `36814671141`, both jobs green:

| job | result | pytest line | wall |
| --- | --- | --- | --- |
| `suite (3.11)` | success | `1481 passed, 6 skipped, 1 warning in 69.36s` | 04:20:48 → 04:22:23Z |
| `suite (3.12)` | success | `1481 passed, 6 skipped, 1 warning in 84.89s` | 04:20:49 → 04:22:55Z |

`1481 + 6 = 1487`, which is the same number of tests this laptop collected
(`1487 passed, 0 skipped in 25.64s`), so nothing went missing between the two
machines -- the runner simply could not run six of them.

Five of the six are accounted for by name and mechanism: every test marked
`skipif(not HAS_BATTERY)` is exactly five -- `tests/test_pipeline.py:19`, `:213`,
`:300` and `tests/test_voice.py:636`, `:716` -- and a GitHub macOS runner is a VM
whose `pmset -g batt` prints no `InternalBattery`, while this laptop's does. That
is a fact about the machine, not about the planner change.

The sixth is named, and getting to it needed a measurement rather than a guess, because
CI runs `python -m pytest -q`, which reports skip counts without reasons. `pytest -m slow
--collect-only` says the whole suite carries 4 slow-marked tests today
(`tests/test_research.py:418` arXiv, `tests/test_vision.py:99/114/126` OCR), so the OCR
group did execute on the runner, and `node` must be installed there or the dashboard files
that gate on it would have skipped by the dozen instead of leaving one unaccounted skip.
The remaining mechanism is `tests/test_voice.py:451`, which skips unless
`dist/Jarvis Forge.app/Contents/Resources/launcher.py` exists -- and `dist/` is gitignored
(measured: `git ls-files dist` is empty, `.gitignore:10`), so a fresh runner checkout can
never have it, while this laptop does. 5 battery + 1 bundle = the 6 the runner reported.

Because the six are now fully accounted for, the skip set provably contains no OCR test --
which settles the loose end 27-D left: CI's green does include the helper-dependent Vision
group (the three `slow`-marked ones plus `test_read_image_text_tool` and
`test_find_image_text_tool`), so the flakiness 27-D recorded is a laptop-under-load
observation that the runner does not reproduce, not a case of the runner never looking.
While counting them, 27-D's wording also proved imprecise: it says "five `@pytest.mark.slow`
OCR tests", and five tests do depend on the helper, but only three carry the marker, so
`-m 'not slow'` would still execute two of the group it describes as protected. The finding
itself is unchanged; the README entry is corrected to match, and this phase's local run
executed all five and passed.

That arithmetic carries a defect with it, recorded as **29-D**: the test whose whole purpose
is to catch the built bundle shipping a stale `launcher.py` is skipped by every CI run,
because `.github/workflows/ci.yml` builds the bundle at line 52 -- *after* `Test suite` at
line 44. The guard therefore only ever fires on a developer machine that built yesterday's
bundle, which is exactly the "kept an older copy for a day" failure the test's own docstring
says it exists to prevent. Moving the bundle step before the suite, or re-running that one
test after the build, would make it mean what it claims. Not done in this phase: it changes
CI's shape and ordering, which is the user's call, and the phase's standard is that a
pipeline change is not a side effect of a planner fix.

The `Build the desktop bundle` and `Upload measured benchmark data` steps are gated
to `matrix.python-version == '3.11'` in `.github/workflows/ci.yml:53,73`, so they ran
(and passed) on 3.11 and showed as skipped on 3.12; that is the gate working as written,
not a regression. The benchmark step regenerated `benchmarks/suite_history.json` and the
three SVGs on the runner from this tree.
