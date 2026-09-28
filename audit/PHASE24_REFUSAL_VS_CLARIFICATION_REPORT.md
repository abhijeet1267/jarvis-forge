# Phase 24 — A refusal and a question are not the same answer

Report date: 2026-09-28. Continues Phases 9-23.

No brief; the scope came from the open-item ledger. **19-B** was Phase 19's own honest
withdrawal: it found that *"I will not do that"* and *"which one did you mean?"* both
arrived as `reply_kind="declined"`, and left the split undone because separating them
"would mean guessing from punctuation". Phases 22 and 23 then filled the system with real
questions — *"Send what to Arun, and by which medium?"*, *"Which light did you mean --
Living room ceiling light, Kitchen light, Bedroom ceiling light, Hallway light?"* — all of
which were labelled refusals, while the guard's actual refusals carried **no** label at all.

---

## 1. What the measurement said

Five sentences through the real pipeline, before any change:

| Sentence | what it is | `reply_kind` | `needs_clarification` |
|---|---|---|---|
| `Jarvis, delete everything.` | refusal | `""` (none) | False |
| `Jarvis, design this system.` | not understood | `not_understood` | False |
| `Jarvis, send to Arun.` | question | `declined` | False |
| `Jarvis, turn on the light.` | question | `declined` | False |
| `Jarvis, send it.` (no draft) | question | `declined` | False |

So the taxonomy was wrong in both directions: three questions were marked as refusals, and
the one real refusal was unmarked. `Plan.needs_clarification` and
`Plan.clarification_question` already existed, and the dashboard already mapped
`needs_clarification === true` to a `NEEDS DETAIL` tile — a state with no way to be reached,
which is exactly what Phase 16 had measured and written down.

## 2. 24-A — the distinction, without guessing at anyone

Phase 19's objection was sound: classifying a *user's* sentence by punctuation is guesswork.
That is not what this does. The string being classified is the note the **route wrote for
itself** — `Intent(note=...)` in `jarvis/core/planner.py` — so its shape is a property of
this codebase, not an inference about a person.

- `Intent` gains `clarification: Optional[bool] = None`; `None` means "read it from my own
  wording".
- `_asks_for_something(note)` returns True for a note ending in `?` or starting with an
  interrogative (`What`, `Which`, `Who`, `How`, `Can you`, …).
- `_to_plan` maps it: question → `reply_kind="clarification"`, `needs_clarification=True`,
  and the note *is* the question. Statement → `declined`, unchanged.
- Two routes state rather than ask in a shape the rule would misread, so they say so:
  `"Send what to Arun, and by which medium? Draft it and I will ask before sending: …"` and
  `"There is no draft waiting to send. Draft one first …"` are prerequisites the user can
  satisfy, and are marked `clarification=True` at the site.
- The phone-battery route's note — *"I cannot read that device's battery: no phone is
  paired"* — is a statement of what is missing. It stays `declined`, and a test asserts it
  is **not** promoted, because "the user could answer it" is not the same as "the system
  asked them something".
- Guard blocks get the word they never had: `reply_kind="refusal"`.

The one-directional property under test is the safety-relevant one: **a refusal must never
be presented as an invitation to continue.** Getting that inverted is how a boundary quietly
becomes a prompt.

## 3. 24-C — a question was advertising a pending approval

The clarification branch did this:

```python
response.needs_confirmation = True   # removed
```

…on a path with no action, no tool, and nothing to approve. A user asking "which light?"
was, in the response shape, indistinguishable from a command waiting for consent: the
dashboard's approval surface keys off exactly that field, and the token would have aged out
with nothing ever to confirm. It was invisible for five phases because the branch was
unreachable; making the branch real made the bug real, and the fix had to come with it.

Verified live after the change: every one of the five sentences returns
`needs_confirmation=False`, and no `confirmation_id` is issued for a question.

## 4. 24-D — the ledger now says which kind of nothing it was

Both non-action audit branches carry `reply_kind` in `detail`, so the hash chain can answer
"was this a refusal or a question?" without reading prose:

```
no_action      reply_kind=declined        | Jarvis, what is my phone battery?
no_action      reply_kind=not_understood  | Jarvis, design this system.
blocked        reply_kind=refusal         | Jarvis, delete everything.
no_action      reply_kind=clarification   | Jarvis, turn on the light.
no_action      reply_kind=clarification   | Jarvis, send to Arun.
```

## 5. Live surface

Launched `run.py serve`, scratch `JARVIS_HOME`, sandboxed `HOME`, over HTTP:

| Sentence | `plan.reply_kind` | State the shipped `pipelineState()` renders |
|---|---|---|
| send to Arun | `clarification` | NEEDS DETAIL |
| turn on the light | `clarification` | NEEDS DETAIL |
| delete everything | `refusal` | BLOCKED |
| design this system | `not_understood` | NOT RECOGNISED |
| what is my phone battery | `declined` | NOTHING EXECUTED |

## 6. Three existing tests had their premise retired, and say so

This is the part of a phase that can quietly become cheating, so each change is recorded:

- `test_phase15_acceptance.py::test_a_question_is_not_guessed_at_from_reply_wording`
  asserted `needs_clarification is False` and `state != "NEEDS DETAIL"` — its own docstring
  said the split *"needs a field the backend does not have yet"*. The field now exists, so
  the test asserts the stronger property instead: the kind comes from `plan.reply_kind`,
  never from the renderer reading punctuation, **and** a statement-note is still not
  promoted. Name kept, because the property it guards is unchanged.
- `test_refusals_and_clarifications_have_their_own_state` and
  `test_schedule_two_hours_tomorrow_reports_no_invented_success`: the two genuinely-asking
  sentences now read NEEDS DETAIL. The substantive claims (nothing executed, no job filed,
  no invented success) are untouched and still asserted.
- `test_phase16_accountability.py::test_rule_planner_never_reaches_the_clarification_branch`
  was a measurement of unreachability. Renamed to
  `test_the_clarification_branch_is_reachable_from_real_sentences`, which is what Phase 16
  said it could not be: it noted the branch "can only be driven by constructing the plan",
  and now three typed sentences drive it. Its companion, which constructs the plan, still
  exists.
- `test_phase15_acceptance.py` dropped its `"Jarvis, send it."` row: whether that sentence
  refuses, asks or sends depends on whether a draft exists in the session, so the new
  coverage sets the precondition instead of relying on test ordering
  (`monkeypatch latest_draft -> None`).

No assertion was weakened to accommodate the change; one state label moved because the
system's answer genuinely became more specific.

## 7. Regressions

`tests/test_phase24_refusal_vs_clarification.py` — **13 tests**, red first (11 failed).
They cover: five real sentences classified as clarifications; a refusal labelled `refusal`
and not `clarification`; a statement of unavailability not promoted; `not_understood`
unchanged; the kind travelling in the HTTP payload; the shipped dashboard JS (executed by
Node, not reimplemented) rendering NEEDS DETAIL for a question and BLOCKED for a refusal;
`reply_kind` in both audit branches; and the missing-prerequisite case with its
precondition set.

Suite: 1331 → **1343 collected, 1342 passed, 0 failed, 1 skipped, 0 xfailed**, identical
across two runs.

## 8. What moved elsewhere, and what did not

The §59 acceptance sweep changed state for exactly the two sentences that ask
(`schedule two hours…` and `send it.`): now 7 VERIFIED / 4 UNAVAILABLE / 2 NOTHING EXECUTED /
**2 NEEDS DETAIL** / 1 FAILED / 1 NOT RECOGNISED. That is the published artifact disagreeing
with its previous version on purpose, and the README table and chart were regenerated from
the new run rather than hand-corrected.

`benchmarks/safety_run.py` still reports **5 of 6** with `turn on heater` as `FAIL`; the
`send to Arun` row passes under either label because its allowed set was written to include
a clarification (`NOTHING EXECUTED`, `NEEDS DETAIL`, `BLOCKED`) before this phase existed —
which is the right way for an expectation to be written: about the *kind* of answer, not one
incidental spelling of it.

Latency is unchanged in kind, and this phase touched none of it: the search row in the new
sweep measured 1,217 ms, the widest value in seven runs of that command (346 → 1,217 ms)
because the fallback walk reads real disk under a 2.5 s ceiling it reports when it hits.

## 9. Carried forward

Closed here: **19-B**, and with it the last dashboard state that had no way to be reached.

Still open: **23-E** the planner's `or "amr_main"` default for an unnamed robot (confirmation
names the machine, so it is truthful; ambiguous `stop` is a safety-semantics call) ·
**22-G** thermal appliances need no confirmation when uniquely resolved · **14-D** a
partly-routable sentence can still answer one half without flagging the other · **15-F**
arXiv's client refusal · **15-H** which calendar store was read · no `read_file` route · no
notification tool · no `ProjectGraph` delete API · desktop bundle exits 1 · hardware
**BLOCKED** · LLM planner · multi-step unattended workflows · 16-F's security-event surface.

## 10. Git and browser

Committed as this phase's commit and pushed to `origin/main` as a fast-forward; nothing
force-pushed. CI is reported after it runs — this file will not claim a verdict before the
observation exists.

**LIVE BROWSER DRIVE: NOT PERFORMED.** §1-§5 are the ASGI app and a launched uvicorn over
HTTP, plus pytest. The dashboard JS was executed by Node on real payloads — which proves the
*state string* the page computes, not that a human clicked it. The phantom-approval fix in §3
is asserted from the response field the approval surface reads, not from the rendered page.

Server stopped (no listener on 8765); scratch homes left under `/tmp`; `~/.jarvis-forge`
never written.
