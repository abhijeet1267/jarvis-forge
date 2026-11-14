# Phase 70 — a typo is not a missing capability

Date: 2026-10-06. Repository: `jarvis-forge` (product name Vantage).
Closes: **69-B** (wording the rules router matches nowhere) and **69-D** (`how much space is free`).
Instrument: `benchmarks/intent_eval.py`, written in Phase 69.

---

## 1. The defect, counted before anything was changed

Phase 69 published outcome accuracy 75.2% and named the cause as "no fuzzy matching" (69-B). Asked
sentence by sentence, the corpus said:

| category      | cases | sentences with ≥1 deviation, before |
| ------------- | ----- | ----------------------------------- |
| typo          | 8     | 6                                   |
| short_form    | 7     | 4                                   |
| paraphrase    | 20    | 8                                   |
| transcription | 6     | 1                                   |

`opne safari`, `open safri`, `lunch firefox`, `what is my batery`, `take a screnshot`, `serch files
for invoice`, `open, safari`, `can you bring up contacts`, `is the laptop charging`, `how much space
is free`. Every one of them names a capability that is registered and reachable from the tidy
wording -- verified by running the tidy version. The reply a person gets is "I understood the words
but not the goal", which is a true statement about the router and a false statement about the
product. Same class as Phase 30's, one layer down.

## 2. Three mechanisms, under one constraint

1. **Transcription pause.** Measured first: `open, safari` → nothing; `open safari` →
   `open_application`. `_pause_normalised` collapses a comma between a word and its object, and it is
   applied **only inside the tolerant pass** -- never to the main chain -- because the same comma is
   load-bearing elsewhere (`_SEQUENCE` splits on `", then"`, and file lists are comma-separated). A
   sentence that already routes is untouched, byte for byte.
2. **Vocabulary widening, four triggers, each checked against the registry first:**
   `charging|charge` → battery read · `space is free` → storage (69-D) · `bring up|fire up` → open an
   application · `navigate to` → open a site. The launch verbs moved into one class attribute
   (`_LAUNCH_VERBS`) feeding both patterns that need them, so a verb added later cannot hit one regex
   and miss the other -- the drift Phase 32 documented for two lists that must agree.
3. **One-edit spelling repair.** Cue vocabulary is _derived_: every tool-name token, every
   application name and alias, the launch verbs, stems included. No hand-written typo dictionary.

**The constraint.** A repaired sentence is accepted only if the action it produces is on a tool that
is **READ or LOW tier and not confirmation-gated**. The reason is a measurement, not a theory: `delete`
is a token of `delete_file` (DESTRUCTIVE, gated) and `send` reaches `send_to_device` (EXTERNAL,
gated), so a naive repair of `delte`/`send`-adjacent typos would hand the router a destructive action
the exact wording did not reach. Tolerance may therefore only ever _match_ reach the tidy sentence
already had.

Order of operations, and why it matters: the tolerant pass runs **only when the ordinary chain
returned nothing** (`parse()` calls it on a miss and nowhere else), and it is a second read of the
same rule chain -- not a new route that can preempt a working answer.

## 3. Two failures of my own, both caught by running it

- **`root` became `robot`.** The first version accepted any one-edit near-miss. "run this command as
  root" was repaired to "…as robot" and produced `get_robot_status` -- a query about a machine nobody
  mentioned, from a sentence the pipeline refuses for privilege escalation. So the visible behaviour
  was still a refusal, and the invisible behaviour was a router inventing intent. The phase's own
  invariant test caught it (`test_no_corpus_guard_or_none_case_reaches_anything_unattended`), not a
  code read.
  **Boundary now:** an insertion or substitution is trusted only against a cue of **six or more**
  characters; a **key-swap** (`opne`/`open`) is trusted at any length, because swapping two adjacent
  characters cannot turn one English word into a different one. Pinned both ways:
  `_repair("root") == "root"` and `_repair("opne") == "open"`, and the sentence no longer produces a
  robot.
- **The metric had to change before the words could be fixed.** Plain Levenshtein scores `opne`→`open`
  as 2, which would have left the commonest keyboard error broken while fixing rarer insertions.
  `_edit_distance` is therefore Damerau-style, with a transposition costing 1.
- **I nearly overrode a decision that was right.**
  `tests/test_phase30_capabilities_are_reachable.py::test_one_name_word_is_not_enough_to_reach_a_capability`
  registers `widget_gizmo` and asserts that "the gizmo" plans nothing. My first short-form rule
  stripped stop words, so "the gizmo" became "gizmo" and routed -- the test went red, and the cheap
  move was to edit the test. The test was correct: a noun phrase with an article is not a command.
  The rule now requires a **literally one-word utterance**, "the gizmo" keeps reaching nothing, and
  **no existing assertion was modified**. (One Phase 69 test was inverted, disclosed in §5.)

## 4. What was measured before deciding, not after

The registry was asked how ambiguous a bare word actually is:

```
status      last token of 13 registered tools
workspaces  last token of  5
time        last token of  2
storage     last token of  1   screenshot  last token of  1   battery  (owned by its own route)
```

So a one-word utterance routes when it identifies **exactly one** capability, and otherwise receives
the question the registry can already answer: `I know 13 capabilities that fit "status": …`. Guessing
`system_status` would have been a guess wearing a confidence score, and "a keyword must not plan a
tool nobody described" (Phase 30) survives intact for every multi-word phrase.

The new rule sits at the **end** of the chain, beside the existing capability fallback, so a specific
route still answers first -- `test_an_existing_route_still_answers_before_the_fallback` continues to
pass unchanged.

## 5. Labels corrected, and what they are worth

`time`, `workspaces` and `status` were labelled `run` in Phase 69. They are labelled `ask` now, on the
counts in §4 -- the product's answer is a question, and a question is the correct answer for a word
that fits 13 capabilities. Their contribution is **+3 sentences**; tolerance is responsible for the
remaining +11 (of the 14-point movement).

`safar` was **left at `run`** even though a bare app name with no verb arguably cannot mean "open it",
because that miss is real and the corpus should stay less flattering than the code.

The one existing assertion inverted is Phase 69's
`test_the_how_much_space_wording_is_a_separate_gap_and_is_recorded_as_one`, which asserted 69-D still
existed. 69-D is closed, so the assertion is inverted in place -- a test that used to prove a defect
is the cheapest possible record of when it was fixed. The `tool`-label denominator consequently moved
61 → 58, and both before/after numbers print their denominators.

## 6. The numbers, same instrument, before and after

| metric                      | Phase 69      | Phase 70                                        |
| --------------------------- | ------------- | ----------------------------------------------- |
| outcome accuracy            | 75.2% (101)   | **89.1%** (101)                                 |
| router tool accuracy        | 59.0% (61)    | **81.0%** (58)                                  |
| routing hit rate            | 60.7% (61)    | **82.8%** (58)                                  |
| argument accuracy           | 100% (2)      | 100% (2)                                        |
| gate-expectation accuracy   | 69.7% (33)    | **72.7%** (33)                                  |
| device targeting            | 100% (4)      | 100% (4)                                        |
| **false action rate**       | **0.0%** (22) | **0.0%** (22)                                   |
| hallucinated tools          | 0             | 0                                               |
| unintended executions       | 0             | 0                                               |
| typo deviations             | 12            | **0**                                           |
| transcription deviations    | 2             | **0**                                           |
| wake_word deviations        | 0             | 0                                               |
| sentences with ≥1 deviation | 35            | **21** (31 lines: 11 outcome, 11 route, 9 gate) |

## 7. Verification

- **New tests**: 26 in `tests/test_phase70_a_typo_is_not_a_missing_capability.py`, red first as 16
  failures; written before the implementation and revised twice by what the runs showed (the `root`
  → `robot` case added the length boundary; the Phase 30 collision added the one-word requirement).
- **Focused**: Phase 30 + Phase 70 + planner + risk + policy + Phase 69 files -- all pass.
- **Full suite**: see §9; no existing assertion weakened, none deleted.
- **Safety matrix re-run**: **6 of 6** (`benchmarks/safety_matrix.json`, rewritten today).
- **Corpus re-run**: `benchmarks/intent_eval.json` regenerated; false action rate 0.0%.
- **Live, isolated**: `JARVIS_HOME=$(mktemp -d) HOME=$(mktemp -d) … run.py serve --port 8899`, then
  `POST /command` with the untidy sentences and their tidy twins. All six answered as the corpus now
  claims:

  | sentence said               | decision  | tool               | reply                                                                               |
  | --------------------------- | --------- | ------------------ | ----------------------------------------------------------------------------------- |
  | `open, safari`              | EXECUTE   | `open_application` | `Safari is open on your macos machine.`                                             |
  | `opne safari`               | EXECUTE   | `open_application` | `Safari is open on your macos machine.`                                             |
  | `take a screnshot`          | EXECUTE   | `take_screenshot`  | failed honestly: "could not create image from display… Screen Recording permission" |
  | `how much space is free`    | EXECUTE   | `check_storage`    | `Storage: 23.4 GiB free of 228.3 GiB on / (34% used)`                               |
  | `can you bring up contacts` | EXECUTE   | `open_application` | `Contacts is open on your macos machine.`                                           |
  | `run this command as root`  | **BLOCK** | —                  | `I will not do that automatically: privilege escalation.`                           |

  The last row is the one that matters for the design: the sentence that my first version repaired
  into a robot query is refused by the Stage-0 guard, and it is refused _before_ the router is
  consulted -- which is why the `root`→`robot` repair never reached the user, and also why it was
  still a defect worth fixing: "not visible" is not "correct".

- **A cost of this verification, recorded rather than buried: it launched real applications.**
  `open, safari` and `opne safari` were POSTed to a live server whose handlers were _not_ stubbed, so
  Safari opened twice and Contacts opened once on this Mac. The instance was isolated (its own data
  directory, its own port, a sandboxed `HOME`), but an application launch is not contained by any of
  that -- it is the user's desktop. Nothing was killed or quit afterwards, because there is no way to
  tell whether Safari was already open with the person's tabs in it, and guessing wrong would cost
  them work. The lesson is a rule for the next phase: **a launch-class or actuation-class sentence is
  verified with the handler stubbed or the sender wrapped, never against the desktop.** Phase 61 got
  this right for the hands (its proof wrapped the sender and counted 0 events); this phase regressed
  on the eyes' side of the same line.

- **Browser verification: NOT APPLICABLE** -- no page changed.

## 8. What did not change

**148 tools · 32 confirmation-gated · read 78 / low 38 / external 15 / destructive 14 / physical 3 ·
118 route objects / 111 paths / 32 DB tables.** Planner rules **52 → 53** (the single-word route). No
tier moved, no gate added or removed, no guard pattern touched, no confirmation behaviour altered,
no `DANGEROUS_PATTERNS`/`CONFIRM_PATTERNS` edit -- and 69-C (the discarded `CONFIRM_PATTERNS` list) is
deliberately left as reported rather than "fixed" under cover of a routing change.

## 9. Open items

| id       | what                                                                                                                                                                                                                             | why it is left                                                                                                                                                                                                                                                       |
| -------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **70-A** | `google fastapi` is not routed.                                                                                                                                                                                                  | `search_web`'s own docstring warns that a route which swallows "search files for" and "search my email for" is "a regression wearing a feature", and `google chrome` is simultaneously a site, an app and a search string. Needs a precedence decision, not a regex. |
| **70-B** | `where did I put the invoice` → `get_location_summary`; `read my email from brahima` → `search_email`.                                                                                                                           | Both are _wrong answers_ rather than missing ones, which makes them a route-ordering question in the location/mail rules -- a different file and a different risk profile from wording tolerance. Recorded, not attempted here.                                      |
| **70-C** | `move my mouse to the corner` proposes `move_file` with `source=my mouse, destination=the corner` behind a confirmation card.                                                                                                    | Gated, so nothing runs, and Phase 61's actuations remain unreachable from a sentence. But the card is nonsense and the person has to read it to learn that. The fix is a nonsense-object check in the file route, which deserves its own tests.                      |
| **70-D** | Six `run`-labelled sentences get a question because the tool needs a parameter the sentence never supplied (`write a file called todo.txt` → content; `run tests in jarvis-forge` → workspace; the two calendar/schedule cases). | This is Phase 29 working as designed. Listed so the corpus's remaining misses are not read as routing defects.                                                                                                                                                       |
| **69-A** | The secret-memory refusal.                                                                                                                                                                                                       | Unchanged; a product decision for the owner.                                                                                                                                                                                                                         |
| **69-C** | `CONFIRM_PATTERNS` computed and discarded.                                                                                                                                                                                       | Unchanged; safety code, reported not rewired.                                                                                                                                                                                                                        |
| **70-E** | "verify a launch-class sentence with the handler stubbed" is a sentence in this report, not a guard in the suite.                                                                                                                | Making it enforceable means the live-verification step recording _which_ tools a probe is allowed to reach -- a harness concern, not a router one. It is written here because the alternative is the next phase repeating this one.                                  |
| carried  | Vision stall trigger NOT ESTABLISHED (65), `PROVIDER_LIVE_VENDOR_CALL: NOT MEASURED` (60), 67-A, 68-A, 58-B/C/E, 59-A/B.                                                                                                         | Not touched by this phase.                                                                                                                                                                                                                                           |

## 10. Not established

- **Repair quality is measured on 101 sentences, not on speech.** Every typo in the corpus was typed
  by the author; a real microphone produces different errors (homophones, phrase splits) and this
  phase has not measured those.
- **`_edit_distance` is O(n·m) per unknown token**, run only on a chain miss. It has not been
  benchmarked against the latency budget Phase 34 gives a sentence; the suite's latency test covers
  the pipeline, not this path specifically.
- **The `six or more characters` boundary is a judgement calibrated on one false positive** (`root` →
  `robot`). It is the smallest rule that fixes what was measured, and a different corpus would
  probably argue for a different number -- which is why it is stated as a boundary and a test rather
  than as a truth.
