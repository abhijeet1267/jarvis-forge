# Phase 17 — Response coherence and audit-card truth

Report date: 2026-09-27. Continues Phases 9-16.

---

## 1. Scope, and how it was chosen

No Phase 17 brief was supplied. The two open items that Phase 16 itself left, and
that need **no new capability, no new vocabulary and no user decision**, were taken
as the phase:

* **16-D** — the ledger now recorded `no_action`, but `handle()`'s reply-only branch
  still returned `decision=PolicyDecision.EXECUTE` for a command that executed
  nothing. The answer contradicted its own audit record.
* **the audit card half of 16-D** — the dashboard's ledger card coloured every token
  that was not `success`/`blocked`/`denied` amber, so `no_action` sat in the same
  colour as `failure`.

Explicitly **not** attempted, because each is waiting on a decision or an
authorisation rather than on work:

* **16-F** — authentication failures in the hash chain (the flooding-vs-accountability
  trade-off; §8 of the Phase 16 report). Needs the user's call.
* Any new `PolicyDecision` enum member (16-D's alternative fix, which touches every
  consumer of `decision`).
* Hardware, read route, notification tool, memory-deletion redesign, desktop bundle,
  RT-3 / R-2 / R-11-R-14, LLM planner.

A numbering note, so the trail stays readable: Phase 16's §9 and §12 both call the
`EXECUTE`-contradiction **16-D**, while my task list for this phase labelled it 16-E
and the card colouring 16-D. They are the same finding; this phase closes both halves
of 16-D.

---

## 2. 16-D, measured before the change

`JARVIS_HOME` isolated, all six reply-only commands through the real pipeline:

| Command | `decision` | actions | ledger `result` |
| --- | --- | --- | --- |
| transfer this file to my phone. | `EXECUTE` | 0 | `no_action` |
| what is my phone battery? | `EXECUTE` | 0 | `no_action` |
| design this system. | `EXECUTE` | 0 | `no_action` |
| flibbertigibbet the wozzle. | `EXECUTE` | 0 | `no_action` |
| send it. | `EXECUTE` | 0 | `no_action` |
| help | `EXECUTE` | 0 | `no_action` |

Six for six, the response claimed execution while the row written in the same call
denied it. The clarification branch (`:271`) already left `decision` unset, so the
two no-execution returns disagreed with each other as well.

Consumers of `decision` were enumerated before touching it, so the change could not
silently break one: `dashboard/index.html:259` (`data.decision === "BLOCK"`),
`orchestrator.py:436` (`response.decision = response.decision or EXECUTE`, on the
*end* path that the reply-only branch never reaches), and assertions in
`tests/test_pipeline.py`. Nothing read `EXECUTE` as "something ran" on this branch.

---

## 3. Changes

### 3.1 `jarvis/core/orchestrator.py`

`response.decision = PolicyDecision.EXECUTE` removed from the `plan.direct_reply`
return. `decision` is the permission outcome; when nothing was permitted to run it is
now left unset, exactly as the clarification branch already did. No new enum member,
so nothing consuming `decision` had to learn one.

### 3.2 `dashboard/index.html`

A named `auditTone(result)` mapping replaces the inline "is it success?" ternary, and
the audit card calls it:

| Ledger token | Tone | Meaning |
| --- | --- | --- |
| `success` | ok | verified work |
| `recovered` | warn | failed, then recovered |
| `failure`, `unverified`, `blocked`, `denied` | bad | did not happen / refused by policy |
| `expired`, `awaiting_confirmation`, `clarification` | warn | waiting on the user |
| `no_action` | **muted** | nothing was going to run, and that was the answer |
| anything unrecognised | warn | a new token is never silently green |

Presentation only. The card reads the ledger's own `result`; nothing here supplies a
state.

---

## 4. After, over the live HTTP path

Real server on `127.0.0.1:8899`, scratch home, dashboard bootstrap → session token,
ledger read straight from `action_logs`:

| Command | `decision` | actions | ledger | agrees |
| --- | --- | --- | --- | --- |
| transfer this file to my phone. | null | 0 | `no_action` | yes |
| what is my phone battery? | null | 0 | `no_action` | yes |
| design this system. | null | 0 | `no_action` | yes |
| send it. | null | 0 | `no_action` | yes |
| help | null | 0 | `no_action` | yes |
| what is the time? | EXECUTE | 1 | `success` | yes |
| show me my laptop battery. | EXECUTE | 1 | `success` | yes |

`agrees` is two checks, not one: `decision==EXECUTE` iff `len(actions)>0`, **and** the
ledger row's `request_id` equals the response's. Both hold for all seven. Distinct
tokens in that ledger: `['no_action', 'success']`.

---

## 5. 17-A — `no_action` covers three different things

`help`, an unrecognised sentence, a missing capability and a deliberate refusal all
land on `no_action`, because the backend genuinely does not distinguish them: every
no-tool route produces `plan.direct_reply` (Phase 16 finding 16-A). The token is
truthful — nothing ran, in each case — but it is coarse, and the dashboard cannot
tell "JARVIS refused" from "JARVIS answered a question" from "JARVIS did not
understand" without guessing from the reply text, which this project has repeatedly
decided not to do.

Splitting them needs one field on `Plan` (`reply_kind`, or the same distinction the
`needs_clarification` flag was meant to carry but never reaches). That is a backend
vocabulary decision, not a presentation fix, so it is recorded rather than invented.

---

## 6. TDD record

`tests/test_phase17_response_coherence.py` — **29 new tests**, written first.

Red → green, watched:

* `test_refusal_does_not_report_execution` — 6 parametrised failures, each naming the
  `EXECUTE` claim.
* `test_refusal_over_the_api_never_reads_as_executed` — 6 failures, asserting on the
  serialised wire rather than the object, because `decision` is what the dashboard
  branches on.
* `test_page_ships_audit_tone_mapping` — 1 clean failure ("no token-to-tone mapping
  exists in the shipped page").
* `test_decision_and_action_count_never_disagree` — the invariant as one property.
* 10 parametrised tone assertions.

Held-green throughout (the other half of the proof): the three
`test_executed_command_still_reports_execution` cases assert `EXECUTE` is still
reported where work really ran, so removing the false claim cannot be smuggled in as
removing the true ones.

Two of my own harness bugs, recorded rather than quietly fixed:

1. The tone fixture initially asserted the missing function inside the fixture, which
   produced 12 **errors** instead of failures. Restructured: a real test fails cleanly,
   the fixture skips.
2. The Node harness read `process.argv[1]` — the script path — so every token came out
   as the fallback `warn` and the mapping looked wrong. Correct argv index is `2`.
   The page was right; my probe was not.

---

## 7. Regressions

```
Phase 17 alone                       : 29 passed
Phase 13 dashboard-truth suite       : still green against the changed page
complete suite, run 1                : 1251 collected - 1247 passed, 0 failed,
                                       1 skipped, 3 xfailed - 22.08s
complete suite, run 2                : identical - 20.61s
baseline before this phase           : 1222 collected / 1215 passed
```

+29 tests. No existing assertion was modified or removed, and no existing test failed
at any point in the phase — including after the `decision` change, which is the one
edit here that could have broken a consumer silently.

---

## 8. Files modified

| File | Change |
| --- | --- |
| `jarvis/core/orchestrator.py` | `plan.direct_reply` return no longer claims `decision=EXECUTE` |
| `dashboard/index.html` | `auditTone(result)` named mapping; audit card uses it |
| `tests/test_phase17_response_coherence.py` | new, 29 tests |

No earlier report was edited.

## 9. Carried forward

Closed here: **16-D** (both halves).

Still open, unchanged: **16-F** authentication not ledgered (awaiting the user's
choice between the two safe designs) · **17-A** `no_action` conflates refusal,
clarification, missing capability and unrecognised input · **14-D** a partly-routable
sentence answers one half and never mentions the refused half · **14-E** simulated
actuations recorded as `verification=success` · **15-E** memory echo rows answering
dated questions · **15-F** arXiv 406 · **15-H** which calendar store was read ·
**15-J** 21.6 s full-filesystem search · no `read_file` route · no notification tool ·
`POST /memory/{id}/forget` overclaims · `ProjectGraph` delete API · RT-3 · R-2 ·
R-11-R-14 · desktop bundle exits 1 · hardware **BLOCKED** · external credentials ·
LLM planner · multi-step unattended workflows.

## 10. Browser and git

Browser not used: the proof is HTTP plus direct reads of `action_logs`, and the
card's mapping is executed from the shipped file by node in §6's tests. **LIVE
BROWSER DRIVE: NOT PERFORMED.**

Still branch `main` with **no commits** — `git rev-list --count HEAD` fails with
`unknown revision or path`, 17 untracked top-level entries. Phases 10-17 remain
unreviewable as a diff.

* staged: no · committed: no · pushed: no. No git write operation was performed.

Cleanup: server stopped; scratch homes `/tmp/p15home*`, `/tmp/p16home*`,
`/tmp/p17home` left for inspection; `~/.jarvis-forge` untouched.

### Addendum — git state changed after this report was written

The §10 statement above was true when measured. On 2026-09-27 the user authorised a
commit, ending the eight-phase no-history condition: `c1664ed`, 208 files, 59,389
insertions, branch `main`, working tree clean, **not pushed**. The audit trail for
Phases 9-17 is now a reviewable diff; `git show c1664ed` is the single commit, since
all of it was previously untracked.
