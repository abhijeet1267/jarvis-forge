# Phase 32 — the words accepted and the thing approved are one thing

Both items closed here are Phase 31's own carried findings. Everything below was measured on
this tree, on this machine, on 2026-10-01.

---

## 1. Measured before anything was changed

**31-A — two vocabularies for one request.** The gateway decides what a home sentence means,
from `KIND_SYNONYMS`: 7 kinds and 26 words. The planner's home-control route matched a
hardcoded list of eleven:

```
planner.py:  lights?|fans?|plugs?|lamps?|ac|aircon|heater|temperature|doors?|cameras?|sensors?
gateway.py:  light lights lamp lamps ceiling bulb fan fans plug plugs power outlet ac aircon
             air conditioner sensor sensors temperature motion energy door doors camera cameras cam
```

Walking every word the gateway knows through the router as it stood:

```
turn on the power        -> intent=unknown  kind=not_understood
turn on the outlet       -> intent=unknown  kind=not_understood
turn on the bulb         -> intent=unknown  kind=not_understood
turn on the ceiling      -> intent=unknown  kind=not_understood
turn on the conditioner  -> intent=unknown  kind=not_understood
turn on the air          -> intent=unknown  kind=not_understood
turn on the cam          -> intent=unknown  kind=not_understood
turn on the motion       -> intent=unknown  kind=not_understood
turn on the energy       -> intent=unknown  kind=not_understood
```

Nine words. "power" is in the table the _resolver_ uses to decide whether a request names a
plug — so the system contained the meaning of the sentence and the router refused it anyway.
This is the shape Phase 30 just fixed for tool names: a private list in the router, narrower
than the registry it routes into, with nothing watching the two drift.

**31-B — consent gathered against a string.** The engine builds a `preview` for every
confirmation and the dashboard renders it (`dashboard/index.html:560`). The chat prompt took
that string as an argument and ignored it, re-deriving its own line from the parameters:

```
stored preview: control_home_device on laptop_main | device=kitchen light | action=set | value=40
chat reply:         Details: device=kitchen light, action=set, value=40
```

Two renderings of one approval, free to disagree, and neither naming the device the gateway
will actually change (`kitchen.light`, "Kitchen light") or what will happen to it. Phase 31
argued that an approval asked against an unidentified device is consent for the wrong reason.
The same argument applies one step later, to the device that _was_ identified.

## 2. What changed

**One vocabulary, and it lives in the gateway.** `device_vocabulary()` returns kinds +
synonyms + a declared set of appliance words a house plausibly has that _this_ inventory does
not (`heater`, `radiator`, `kettle`, `boiler`, `humidifier`, … — 39 words total). The
planner's pattern is built from it at call time, longest word first, so "air conditioner" is
not matched as the fragment "air".

The absent-appliance set matters more than it looks. Deriving the list from synonyms alone
would have made `"turn on the heater"` _unrecognised_ again, quietly undoing Phase 31's
refusal-with-candidates answer. A word for an appliance this house lacks is still a home
request, and the honest answer is "there is no home device matching 'heater'; the gateway
has: …". That belongs beside the synonym table because it is the same object: the vocabulary
the router is allowed to use.

**The approval names the thing.** `describe_actuation()` in the home module phrases the
resolved device and the action, with units the device actually has:

```
switch on Living room fan (living.fan)
set Kitchen light (kitchen.light) to 40%
switch on Study AC (study.ac)
```

It lives in the home module because that is where "what does a value mean for this kind" is
known; the permission engine asks rather than duplicating it, and non-home tools keep their
parameter listing (which is as specific as a resolver-less tool can be).

**One string to both surfaces.** `_confirmation_prompt` now uses the preview it is handed, so
the line in the chat and the line stored in the confirmation record — the thing the dashboard
renders — cannot drift. Neither existing test nor the dashboard pinned the old wording, which
is why no suite run had ever noticed the duplication.

## 3. Live, over HTTP, on a launched server

`python run.py serve` on a temporary `JARVIS_HOME`, sandboxed `HOME`, token auth on. No device
in this house changed state; the gateway reports offline and says so.

```
=== the nine words the gateway always knew and the router refused ===
  "Jarvis, turn on the power."
      Which power did you mean -- Study standing desk, Living room media power,
      Kitchen fridge power, Bedroom alarm clock? Name the room and I will switch it.
  "turn on the bulb"
      Which bulb did you mean -- Study desk lamp, Living room ceiling light, Kitchen light,
      Bedroom ceiling light, Hallway light? …
  "turn on the conditioner"   decision=CONFIRM
      Will do: switch on Study AC (study.ac)

=== the refusal for an appliance that is not here still stands ===
  "Jarvis, turn on heater."    decision=BLOCK
      There is no home device matching 'heater'. The gateway has: Study desk lamp, …

=== the approval names the device, and the record says the same thing ===
  "Jarvis, dim the kitchen light to 40." -> decision=CONFIRM
      chat  : Will do: set Kitchen light (kitchen.light) to 40% | Tool: control_home_device | …
      record: set Kitchen light (kitchen.light) to 40%
      chat and record are one string: True
      after approval -> Your home gateway is offline, so I could not set Kitchen light to 40.
                        No device was switched.

7 ledger rows · clarification 3 · awaiting_confirmation 2 · blocked 1 · failure 1
```

Widening recognition did not widen what can be switched: "power" and "bulb" each describe
several devices, and every one of them is now a question naming them instead of an action on
one of them. That is the Phase 31 rule holding under a bigger vocabulary, and it is the reason
this phase could be done at all.

## 4. Findings this phase made about itself

1. **A recognition list and a resolution table are the same object, held apart by nothing but
   file boundaries.** The gateway's table is what decides meaning; the planner's list decided
   only whether to ask. Nine words sat in the gap, and no test could see the gap because each
   side was internally consistent.
2. **Deriving a list can delete a refusal, which is a safety change disguised as a
   simplification.** If `device_vocabulary()` had been built from synonyms alone, "heater"
   would have gone back to being nonsense and Phase 31's honest refusal would have quietly
   regressed. The absent-appliance set exists to keep that refusal reachable by language.
3. **A function that accepts an argument and ignores it is a duplication that looks like
   plumbing.** `_confirmation_prompt(action, preview)` had the canonical string in its
   signature for as long as it existed and never read it. The drift was not hypothetical:
   the two lines already differed in shape.

## 5. Regressions

New `tests/test_phase32_one_vocabulary_one_approval.py`: 39 tests, red first (16 failing
before the code changed, including the nine-word sweep).

The two that hold this phase honest are not about the current word list at all:

- `test_the_router_follows_the_gateway_when_a_word_is_added` — adds `whirligig` as a synonym
  of `fan` at test time and asserts the router reaches _that_ device. A hardcoded list fails
  this immediately, so the lists cannot silently drift apart again.
- `test_the_router_stops_accepting_a_word_the_gateway_drops` — the reverse direction, because
  a one-way sync is still two lists.

Also pinned: no device's `on`/`level`/`speed`/`setpoint` changes from a category word; a
sensor-only word never produces a switch claim; `"turn on heater"` is still BLOCK (Phase 31);
`take a screenshot` / `what is my battery` / `open VS Code` still route as before; the
non-home confirmation still names its path; and the prompt and the stored preview are one
string. No existing test was changed — the old prompt wording was pinned by nothing, which is
reported as the finding in §4.3 rather than as a clean pass.

Full suite: **1621 passed, 0 failed, 0 xfailed** (1621 collected), up from 1582.

## 6. Re-measured

| metric                                                    | before                                                                                 | after                                           | how                                        |
| --------------------------------------------------------- | -------------------------------------------------------------------------------------- | ----------------------------------------------- | ------------------------------------------ |
| words the gateway understands                             | 26 synonyms + 7 kinds                                                                  | 39 (adds 13 absent appliances)                  | `device_vocabulary()`                      |
| …answered `not_understood` by the router                  | **9**                                                                                  | **0**                                           | sweep of the table through `planner.parse` |
| device words hardcoded in `planner.py`                    | 11                                                                                     | 0 (`lights?                                     | fans?` absent from the file)               | source grep, plus the two mutation tests |
| confirmation preview for a home actuation                 | `control_home_device on laptop_main \| device=kitchen light \| action=set \| value=40` | `set Kitchen light (kitchen.light) to 40%`      | `/confirmations` on a launched server      |
| chat prompt vs stored preview                             | two derived strings                                                                    | one string (`record["preview"] in reply`)       | live HTTP + a test                         |
| §45 safety matrix                                         | 6 of 6                                                                                 | **6 of 6**                                      | `python benchmarks/safety_run.py`          |
| §59 acceptance sweep                                      | 17 of 18                                                                               | unchanged (no home-control command in the list) | `python benchmarks/acceptance_run.py`      |
| tools / agents / api routes / confirmation-gated / tables | 127 / 15 / 104 / 27 / 32                                                               | unchanged                                       | `python assets/build_diagrams.py`          |
| suite                                                     | 1582                                                                                   | 1621                                            | `python -m pytest -q`                      |

## 7. Carried forward

- **32-A** the absent-appliance list is declared, not derived: a house that genuinely has a
  heater would need it added in two places (the inventory _and_ this set) before the word
  stopped being refused. Deriving it from "words people say about homes" is a vocabulary
  problem this phase chose not to guess at.
- **32-B** non-home confirmations still show a parameter dump (`delete_file on laptop_main |
path=… | permanent=False`). Honest, and as specific as a tool without a resolver can be;
  the memory and research tools that _do_ have identifiers to resolve could use the same
  `describe_*` treatment, one per module.
- **32-C** `dim` arrives at the gateway as `action="set"`, so the approval reads "set Kitchen
  light to 40%" where a person wrote "dim". No meaning is lost and nothing is invented; the
  verb is the one place the phrasing is still the tool's rather than the user's.
- Everything already open: 30-A (closed in 30), 30-B…D, 29-A…D, 28-A narrowed, 27-A…D, 26-B,
  the `ToolSpec.parameters` residual, 23-E, mic/speech TCC, 15-F, 14-D, 16-F, hardware
  **BLOCKED**, LLM planner, unattended workflows.
- Closed here: **31-A**, **31-B**.

---

## 8. Git

Committed and pushed to `main` on `abhijeet1267/jarvis-forge`:

```
aa248e9  The words the router accepts and the thing the approval names are one thing
         14 files changed
```

### The runner's verdict, measured after the push

Run `36865970543`, both jobs green:

| job            | result  | pytest line                                   | wall                 |
| -------------- | ------- | --------------------------------------------- | -------------------- |
| `suite (3.11)` | success | `1615 passed, 6 skipped, 1 warning in 77.34s` | 13:03:22 → 13:05:11Z |
| `suite (3.12)` | success | `1615 passed, 6 skipped, 1 warning in 86.84s` | 13:03:22 → 13:05:20Z |

`1615 + 6 = 1621` — the laptop's collection count again, and the six are the same accounted
set from Phase 29 (five battery-gated, one `dist/`-gated), so all 39 of this phase's tests ran
on both images. That includes the sweep over every word in the derived vocabulary and the two
mutation tests that add and remove a synonym, which is the part of this phase that would be
easy to pass for the wrong reason on one machine's gateway inventory.

The benchmark step re-ran the safety matrix on the runner, and the row this phase must not
have regressed is still refused there:

```
suite (3.11)  ok   Jarvis, turn on heater.   BLOCKED   "There is no home device matching 'heater'. …"
suite (3.12)  ok   Jarvis, turn on heater.   BLOCKED   "There is no home device matching 'heater'. …"
```

Worth stating plainly, because it is the specific risk of this phase: widening recognition is
the kind of change that can quietly delete a refusal. "heater" is not in the gateway's synonym
table -- it is in the declared absent-appliance set, which exists so the sentence stays a home
request and gets refused with the devices that do exist. The laptop test and both runners
confirm that after the vocabulary change, not before it.
