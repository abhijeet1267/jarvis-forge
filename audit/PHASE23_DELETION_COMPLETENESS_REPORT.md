# Phase 23 — Deletion that means the same thing on every surface

Report date: 2026-09-28. Continues Phases 9-22.

No brief. The scope came from the project's own ledger of parked work: three tests in
`tests/test_phase10_memory_deletion.py` were marked `xfail` in Phase 10 with the root cause
named precisely and the reason *"this phase does not own that file"*. Twelve phases later
nobody had owned them, which is how a known untruth survives: it gets documented instead
of fixed. This phase fixed all three at their sources, plus the Phase 22 carry-over (22-H).

---

## 1. What was actually broken, and how it was known

| Id | Defect | Root cause named in Phase 10 | Now |
|---|---|---|---|
| 23-A | `POST /memory/{id}/forget` answered `{'deleted': True}` — nothing else | `jarvis/api/app.py` had its own handler and never called the tool | route delegates to `forget_memory()` |
| 23-B | checking a deletion wrote another copy of the deleted fact | `JarvisCore._remember_outcome()` echoes *every* command with actions, including the recall used to verify | memory-read-only commands are not echoed |
| 23-C | deleted text stayed resident in the process | `RemoteEmbedder._cache` keyed by raw text, no eviction API | `forget_text()` / `forget_text_everywhere()`, called from `MemoryStore.forget` |
| 23-D (22-H) | a robot command with no robot named inferred `amr_main` | `command_robot_motion` fell through to `gw.resolve("amr")` | asks when >1 machine can take the motion |

The interesting part is that all four were *invisible to the honest path*. The tool said
"Partly forgotten: … 1 other memory quotes the same wording word for word"; the HTTP route
beside it said `deleted: true`. Same deletion, two claims, and the one a dashboard or a
script would call was the less truthful one.

## 2. 23-A — one deletion, one report

`/memory/{id}/forget` now calls the tool and passes its facts through, and its audit token
is derived from what happened rather than hardcoded:

```json
{
 "memory_id": "MEM-1b433bd24ebd",
 "deleted": true,
 "complete": false,
 "report": "Partly forgotten: I deleted … and its search vector, but 1 other memory quotes
            the same wording word for word and will still come back in search … I removed
            only the one row you named. The wording still stands in the history of this
            exchange -- 2 transcript message(s) and 1 audit record(s) -- kept as records by
            design and not deleted.",
 "remaining_copies": [{"id": "MEM-948dd2e9df63", "scope": "session", "source": "orchestrator"}],
 "retained": {"messages": 2, "action_logs": 1, "graph_nodes": 0}
}
```

Measured live over HTTP, not asserted from the code. The ledger row for that same call is
`unverified` rather than `success`, so the chain and the reply agree (the Phase 17 rule).

An unknown id still returns **404**, now because the tool said it had no such memory rather
than because the route pre-checked.

## 3. 23-B — verifying a deletion must not re-create it

`_remember_outcome()` stores a session memory quoting the request and the tool's reply, for
any command that ran something. That is what makes "what did I work on yesterday" work. It
also meant that `do you remember ZQ9177?` — a *read*, whose whole purpose was to check
whether a fact was gone — stored a new row quoting the very text being checked, and asking
again stored another.

The rule added: a command whose actions were **all** memory reads (`search_memory`,
`memory_overview`) is not an event worth remembering. Looking at the activity log no longer
adds to it.

Live check on a scratch home, forgetting the named row and then asking twice:

```
rows holding ZQ9177 after the forget : 1   (the session echo)
rows holding ZQ9177 after recall #1  : 1
rows holding ZQ9177 after recall #2  : 1
```

and the guard on the other side, so this is not a suppression of the activity log: a
command that *changes* something (`remember that …`) still echoes. That asymmetry is the
whole fix, and both directions are tested.

## 4. 23-C — the cache had to become evictable, which meant making it shared

`RemoteEmbedder` kept `self._cache = {}` per instance. Adding a `forget_text()` to one
instance would not have touched another instance's cache, so a deletion could not have
reached "the process" at all — the text would live on in every `RemoteEmbedder` anyone had
constructed, which is exactly the shape of the bug.

So the cache moved to module scope (`_SHARED_CACHE`), on the argument that an embedding is
a function of text and model, not of object identity. That makes eviction honest:
`forget_text_everywhere(text)` drops the exact key *and* any longer prompt containing it.
`Embedder.forget_text()` is now part of the interface with a base implementation that
returns 0 — the local hashing embedder genuinely holds no text, and it should be able to say
so instead of being special-cased at the call site.

`MemoryStore.forget()` reads the row's content before deleting it, then evicts. A user-facing
"forgot" that leaves the sentence resident is not a forgot.

## 5. 23-D — the robot, and where the fix deliberately stops

`command_robot_motion` with `robot=""` used to infer a machine:
`if action in AMR_ACTIONS … gw.resolve("amr")`, else `resolve("arm")`, else
`resolve("amr") or resolve("arm")`. Measured in the test run before the fix:

```
command_robot_motion(robot="", action="charge")
  -> "No robot bridge is configured, so I could not charge AMR main."
```

Nothing was reached, so nothing was harmed — but the system had chosen a machine the user
never named, which is Phase 22's defect on hardware that moves. It now returns a failure
naming every robot capable of the action, and acts without asking only when exactly one
machine can perform it (a choice the fleet cannot make is not a question worth asking).

**Where it stops.** A *typed sentence* still does not reach that guard, because the planner
hardcodes a default before the tool is called:

```python
params={"robot": self._robot_ref(text) or "amr_main", ...}   # planner.py:1884
```

Measured live: `Jarvis, home the robot.` → confirmation prompt naming `robot=amr_main`,
nothing executed until approved. The plan's §45 accepts "Require confirmation", and the
prompt states which machine, so this is truthful — it is just not yet *unambiguous*. It is
recorded as **23-E** rather than fixed, for a reason worth stating: for `stop`, asking
"which robot?" inserts a round trip between a person and a moving machine. Whether that
trade is right — or whether an ambiguous `stop` should hit every capable robot at once — is
a safety-semantics judgement about their hardware, not a truthfulness defect I can settle
by editing a condition.

## 6. Regressions

`tests/test_phase23_deletion_completeness.py` — **10 tests** (7 failed red-first; three of
my own assertions were wrong against the real API and were corrected rather than the code
bent to them: `fail()` sets `.error`, not `.message`; `dict()` is needed on `sqlite3.Row`;
a nested comprehension counted one robot twice and made an unambiguous action look
ambiguous).

| Group | Pins |
|---|---|
| 23-A (3) | the HTTP route returns the tool's `complete`/`report`/`remaining_copies`; its audit row is not a hardcoded `success`; an unknown id is a 404 |
| 23-B (3) | a recall stores no new memory; a changing command still echoes; a forgotten fact stays forgotten across repeated verification |
| 23-C (2) | deleting a memory evicts its cache entry, including substring hits; the local embedder exposes the same API and holds nothing |
| 23-D (2) | an action two robots can do asks which; an action only one can do proceeds without a question |

The three `xfail` markers in `tests/test_phase10_memory_deletion.py` were **removed**, not
relaxed: those tests now run and pass, and their section header records that they were
parked in Phase 10 and closed here. That is the difference between a green suite and a
truthful one — an `xfail` that stays green forever is how a known defect becomes a footnote.

Suite: 1321 → **1331 collected, 1330 passed, 0 failed, 1 skipped, 0 xfailed**. Run twice,
identical. The `1 skipped` is still `test_research.py` refusing to pretend arXiv answered.

## 7. Nothing else moved

Re-running the §59 acceptance sweep after all four fixes produced the same terminal states
as Phases 21-22 (7 VERIFIED / 4 NOTHING EXECUTED / 4 UNAVAILABLE / 1 FAILED / 1 NOT
RECOGNISED), 2,378 ms total, median 5 ms. `benchmarks/safety_run.py` still reports **5 of
6** with `turn on heater` printed as `FAIL` — 22-G is a permission-policy decision and is
untouched by this phase.

## 8. Carried forward

Closed here: **P0-5's three residuals** (23-A, 23-B, 23-C) and **22-H** at the tool layer.

Still open: **23-E** the planner's `or "amr_main"` default, with the stop-under-pressure
reasoning above · **22-G** thermal appliances need no confirmation when uniquely resolved ·
**15-F** arXiv's client refusal (1,752 ms and no arXiv results per call, degraded honestly) ·
**19-B** refusal vs clarification sharing a label · no `read_file` route · no notification
tool · no `ProjectGraph` delete API · **15-H** which calendar store was read · desktop
bundle exits 1 · hardware **BLOCKED** · LLM planner · multi-step unattended workflows ·
16-F's security-event surface.

## 9. Git and browser

Committed with this phase: `jarvis/api/app.py`, `jarvis/core/orchestrator.py`,
`jarvis/memory/embeddings.py`, `jarvis/memory/store.py`, `jarvis/tools/robotics.py`,
`tests/test_phase23_deletion_completeness.py`, `tests/test_phase10_memory_deletion.py`,
`benchmarks/*` (including the phase-23 history point), `assets/*.svg`, `README.md`, this
report. Pushed to `origin/main` as a fast-forward; nothing force-pushed. **CI verified
after the fact, not predicted:** run `36406299851` on `a01b4c6` finished
`success` with both matrix legs green -- `suite (3.11) = success`, `suite (3.12) = success`
-- checked with `gh run view --json jobs` once it completed.

**LIVE BROWSER DRIVE: NOT PERFORMED.** §2-§5 are HTTP against a launched `run.py serve`
(scratch `JARVIS_HOME`, sandboxed `HOME`) plus pytest. The dashboard page was not opened,
so no claim is made about how `remaining_copies` is *rendered* — only that the API returns
it, which is what the dashboard reads.

Server stopped (no listener on 8765); scratch homes and sandbox trees left under `/tmp`;
`~/.jarvis-forge` never written.
