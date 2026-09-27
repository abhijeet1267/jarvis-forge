# Phase 20 — Dated recall, and echoes called what they are

Report date: 2026-09-27. Continues Phases 9-19.

---

## 1. Scope, and a scope change made by measurement

No brief. The plan was to close **16-F** (authentication decisions not in the audit
chain). Measuring first showed that the category 16-F assumed **does not exist in this
system**, so implementing it would have added risk without adding accountability:

- `require_auth` (`jarvis/api/app.py:222-241`) is binary: a request either carries a
  valid token or is rejected 401. There is no "authenticated but not authorised" tier
  to record.
- The origin gate returns 403 before credentials are considered (`app.py:265-267`), so
  it is a browser-protection layer, not a per-user authorisation decision.
- That leaves 16-F's "safe design 2" collapsing into design 1: writing **401s from an
  unauthenticated caller** into the hash-chained ledger -- attacker-cheap to generate at
  the rate limit (~36 000 rows/hour), carrying attacker-controlled text into
  `intent`, and polluting `AuditLog.failures()` with non-tool events.

So 16-F is resolved here as **an analysis, not a code change**: the accountability
boundary legitimately starts where `require_auth` passes, and rejected requests are
already recorded in the application log (`"rejected unauthenticated request"`,
measured 3 log lines for 3 anonymous commands in Phase 16 §8). If a queryable security
event surface is ever wanted, it should be a bounded non-chained store -- a decision
that stays with the user.

The work then went to **15-E**, the highest-value truth defect left that needs no
user decision.

---

## 2. 15-E, measured

`RulePlanner._recall` turned a date into a search string:

```python
if "yesterday" in text:
    query = "yesterday work"
```

So "what did I work on yesterday" asked the index for the *phrase* "yesterday work" and
returned whatever ranked best from any day, prefixed with "Here is what I remember".
Phase 15's live sweep had already caught it doing exactly that; the store's
`created_at` existed and was never used, never returned, never mentioned.

Two untruths in one answer: not scoped to the day asked about, and an **activity echo**
(JARVIS's own `"User asked: … Result: …"` line, `scope=session`, `source=orchestrator`)
presented as a remembered fact.

---

## 3. Changes

### 3.1 `MemoryStore.in_window(since, until)` (`jarvis/memory/store.py`)

An exact `created_at` range query, **not** a filtered top-k. The distinction matters:
filtering after ranking would answer "what did I do yesterday" with a confident nothing
whenever yesterday's entry ranked sixth against today's noise -- trading one untruth for
a more dangerous one.

### 3.2 `search_memory(ctx, query, since, until)` (`jarvis/tools/memory_tools.py`)

Two deliberate retrieval paths:

* **With a window** — exact range, newest first; an optional topic narrows within the
  range and a narrowing that hits nothing reports the range as empty rather than
  silently falling back to undated results.
* **Without one** — the existing hybrid retrieval, unchanged.

Every result now carries `created_at`, `source`, `scope` and **`is_echo`**
(`source == "orchestrator"` or the content starts `"User asked:"`), and the reply says
what it is showing:

* empty day → `Nothing was recorded in memory on 2026-09-26.`
* with results → `On <day> I recorded N thing(s). Most recent, which is my own activity
  log rather than something you told me: …`

The ToolSpec was widened (`since`/`until` declared; `query` no longer required) because
a dated browse is a legitimate request, and demanding a search phrase for it is what
invented one. The tool still refuses when given neither a query nor a window.

### 3.3 `RulePlanner._recall` (`jarvis/core/planner.py`)

`yesterday` / `today` become a date range on the action's params; the date word is
stripped from the query. An undated recall keeps its previous behaviour.

**Nothing was redesigned here.** `_remember_outcome()` still writes echoes, and
`scope`/`source` still distinguish them -- that was always the case; the failure was
entirely on the read side, which is where this phase stayed.

---

## 4. Live proof over the real HTTP path

Real server on `127.0.0.1:8899`, scratch home, dashboard bootstrap → session →
`POST /command`, same session:

| Command | window | dates of returned items | reply |
| --- | --- | --- | --- |
| what did I work on yesterday? | `2026-09-26 → 2026-09-26` | *(none)* | `Nothing was recorded in memory on 2026-09-26.` |
| what did I work on today? | `2026-09-27 → 2026-09-27` | `2026-09-27` | `On 2026-09-27 I recorded 4 thing(s). Most recent, which is my own activity log rather than something you told me: User asked: what did I work on yeste…` |

Before this phase the first row answered with a today-dated echo and the words "Here is
what I remember". It now states a fact about the day that was asked about, and when the
best answer is JARVIS's own log line, it says so. `top_is_echo` is a machine-readable
field, so no caller has to read prose to know.

---

## 5. TDD record

`tests/test_phase20_dated_recall.py` — **10 tests**, written first; 9 failed red and
one (`a recall without a date stays unscoped`) served as a guard. Red reasons included
`query == "yesterday work"`, `TypeError: unexpected keyword 'since'`, no `window` in
the output, and an answer that mixed dates.

The fixture back-dates one memory with a direct `UPDATE`, because the point under test
is the date and nothing in the product lets a caller invent one. Created rows are
deleted in `finally`.

**One of my tests was wrong and the full suite caught it**: I asserted every returned
row was a non-echo. It passed alone and failed in the full run — the session-scoped
store is by then full of *other tests'* activity echoes. Retargeted to the row it
created. That failure is itself the best evidence in this report for 15-E: in a busy
store, echoes dominate recall. Recorded rather than smoothed over.

---

## 6. Regressions

```
Phase 20 alone          : 10 passed
complete suite, run 1   : 1290 collected - 1286 passed, 0 failed, 1 skipped,
                          3 xfailed - 21.23s
complete suite, run 2   : identical - 21.26s
baseline before phase   : 1280 collected / 1276 passed   ->  +10 tests
```

No existing test was modified, skipped or deleted. The widened `search_memory` spec and
new optional parameters broke nothing in 1290 collected tests, including the Phase 15
and Phase 12 command-path suites that exercise recall.

---

## 7. Files modified

| File | Change |
| --- | --- |
| `jarvis/memory/store.py` | `in_window(since, until, limit)` exact range query |
| `jarvis/tools/memory_tools.py` | `search_memory` date windows, `created_at`/`is_echo` per result, echo-aware messages, widened `ToolSpec` |
| `jarvis/core/planner.py` | `_recall` parses `yesterday`/`today` into a range instead of a search phrase |
| `tests/test_phase20_dated_recall.py` | new, 10 tests |

## 8. Carried forward

Closed here: **15-E** (read side) and **16-F** (resolved by analysis, with the reasoning
recorded in §1 rather than silently dropped from the list).

Still open: **19-B** refusal vs clarification both `declined` · **14-D-adjacent**: an
`"on Monday"` style date is not parsed, only today/yesterday · **15-F** arXiv 406 ·
**15-H** which calendar store was read · **15-J** 21.6 s full-filesystem search ·
**14-E's** remaining kin: no `read_file` route · no notification tool ·
`POST /memory/{id}/forget` overclaims · `ProjectGraph` delete API · RT-3 · R-2 ·
R-11-R-14 · desktop bundle exits 1 · hardware **BLOCKED** · external credentials ·
LLM planner · multi-step unattended workflows.

## 9. Git and browser

Not committed. Phases 18, 19 and 20 are all uncommitted in the working tree; `c1664ed`
and `df4583a` remain the only commits. Staged: no · committed: no · pushed: no.

Browser not used; the proof is HTTP against the real server. **LIVE BROWSER DRIVE: NOT
PERFORMED.**

Server stopped; scratch home `/tmp/p20home` left for inspection; `~/.jarvis-forge`
untouched.
