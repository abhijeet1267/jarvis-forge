# Phase 21 — Make it fast where it was unbounded, and non-fatal where it was one source deep

Report date: 2026-09-28. Continues Phases 9-20.

Brief, in the user's words: *"checkk all are working in jarvise and make it smooth and
fast without fail."* Three verbs, so three questions had to be answered with numbers:
is everything actually working, what is slow, and what fails that should not.

---

## 1. What "all working" measured as

Full suite, `JARVIS_HOME` in a scratch directory, twice, same result:

```
1300 passed, 1 skipped, 3 xfailed in 27.20s
```

18 acceptance commands posted through the real ASGI app (`benchmarks/acceptance_run.py`):
17 measured, 1 skipped because it would launch a GUI on this machine.

| Terminal state the user sees | count |
|---|---|
| VERIFIED | 7 (one of them degraded -- see §4) |
| NOTHING EXECUTED | 4 |
| UNAVAILABLE | 4 |
| FAILED | 1 |
| NOT RECOGNISED | 1 |
| not executed (VS Code) | 1 |

No command returned a false success here, which is the condition Phases 10-20 built
toward: the four hardware-backed commands say **UNAVAILABLE** because the car, home and
phone are genuinely not paired, not because the tool answered `[]`. That part is already
true, so the phase's work went to the two things that were *not* true: latency, and
brittleness.

### Surfaces driven for real, not just under pytest

The suite runs in-process, so "all working" was also checked against a launched server and
the spoken pipeline, on a scratch `JARVIS_HOME`, with the author's real home never written:

| Surface | Result |
|---|---|
| `python run.py serve` (uvicorn, 127.0.0.1:8765) | starts; `/health` `/tools` `/devices` `/tasks` `/memory` `/schedules` `/audit` `/voice/status` all **200** |
| `GET /audit` without a token | **401** |
| `POST /command` over the network | answered; `search_files` 462 ms, `car_telemetry` 6 ms, `remember` 7 ms |
| `GET /health`, first call vs warm | 110 ms once, then **1.5-8 ms** — nothing expensive runs per request |
| Hash chain over that home | `verify_chain()` → `{'ok': True, 'checked': 4, 'total': 4}` |
| Voice pipeline (`VoiceSession`, wake `jarvis`/`hey jarvis`/`ok jarvis`, `macos_speech`/`macos_say`) | `what time is it` → EXECUTE; `what is my laptop battery` → **70 % (charging)** read from the real sensor |
| Same pipeline, hostile input | `delete everything` → **BLOCK**: "I will not do that automatically: blanket delete request." |

`run.py demo` and `run.py voice-demo` were **not** executed: their built-in list contains
`open VS Code`, `take a screenshot`, `send an email` and `delete everything`, and a
checkpoint that launches GUI apps, writes to the desktop, or attempts a blanket delete on
someone's machine is not a test, it is a side effect. The same commands minus those
side effects are what the acceptance sweep above measures.

## 2. The slow path, measured before touching it

Posted through `/command` on this machine, same query, same home directory:

| Path | Before | After |
|---|---|---|
| `Jarvis, find my latest project report.` (no match in `~`) | **11,299 ms** | **683 ms** (346 / 451 / 683 across three runs) |
| `search_files` with an unmatched query, rooted at `~` | **6,955 ms** | **346-683 ms** |
| All 17 measured acceptance commands, end to end | **22,573 ms** | **2,522 ms** (2,028-2,522 ms across runs) |
| Worst single command in the sweep | 21,600 ms (the same search) | **1,722 ms** (`research`, see §4) |
| Median single command | low single-digit ms | **5-6 ms** (unchanged) |
| API reads (`/tools`, `/tasks`, `/audit`, `/memory`) | 1-2 ms | unchanged |
| `GET /health` on a launched server | 1.5-8 ms warm | unchanged |
| `JarvisCore()` construction | 29 ms | unchanged |

The 6,955 ms and 11,299 ms figures are the same bug at different ambient loads, which is
why both are reported instead of the prettier one. So is the spread on the right-hand
side: the *same* search command measured 346 ms, 451 ms and 683 ms in three runs of the
same build, because the fallback walk reads real disk and the disk was doing other things.
What the change actually buys is not a number, it is a **ceiling** — the walk cannot now
cost more than ~2.5 s, and when it hits that it says so — where before the cost was
whatever `~/Library` happened to be that day, with no upper bound at all.

**Root cause, from the code and not from a guess** (`jarvis/tools/files.py`): the
filesystem walk only stopped early when it *found* something. A query that matched
nothing therefore had no exit condition, and the dot-directory filter did not exclude
`~/Library` -- macOS' largest tree, and nowhere a user means when they say "find my
project report". A matching query paid 0.36 s; an unmatched one paid 6.9-11.3 s. Worst
case, not average case, was the problem: the slow path was the *miss*, which is exactly
the path a user cannot distinguish from "you searched my files and found nothing".

## 3. The fix, and what it promises

Three bounds and one disclosure rule:

```python
PRUNED_DIRS = {"Library", "Applications", "node_modules", "__pycache__", "dist", "build",
               "venv", ".venv", ".cache", "Caches", ".npm", ".gradle", ".mypy_cache",
               ".pytest_cache", ".Trash"}
SEARCH_SCAN_CAP = 60_000          # entries examined
SEARCH_TIME_BUDGET_S = 2.5        # wall clock
```

- **Prune what nobody means.** Matched on the directory *name* at any depth, so
  `~/Library` and `~/code/x/node_modules` are both skipped. The cost of that choice is
  stated rather than hidden: a project folder literally named `Library` is skipped too.
  The escape hatch is that pruning applies to *descendants of the root*, never to the root
  itself -- asking for `directory="~/Library/Containers"` searches it, live-verified below.
- **Cap entries and time.** Either bound can end the walk.
- **Say which one ended it.** A miss now reports one of two different claims:

  > I could not find any file matching `'project report'` under /Users/…  *(complete)*
  >
  > I searched N entries under … and stopped early, so this is not exhaustive: no file
  > matching … turned up. *(bounded)*

  `scanned`, `truncated` and the pruned names actually skipped are on the tool result, and
  the *sentence* is what the ledger keeps: `detail` persists `ok` + `message`
  (`jarvis/core/orchestrator.py:230`), so a chain reader sees "I stopped early" instead of
  a bare `success`. Checked rather than assumed -- the structured flags are deliberately
  **not** separate audit columns, and dumping every tool's whole output dict into the
  hash chain is not wanted (a 50-file result or a paper list would be written into the
  chain for it).
  **"Nothing matched" and "I stopped
  looking" are different sentences, and only the first one is safe to make from a
  bounded walk** -- a truncation flag the user never sees is not honesty, it is a
  comment. The bound is also not allowed to hide a real hit: the tests pin that a
  matching file below the cap is still found, and that a *complete* search does not
  cry truncation.

Observed on this machine while writing this report: an explicit
`directory="~/Library/Containers"` probe hit the **time** budget after 2,757 entries and
answered *"I searched 2757 entries under … and stopped early, so this is not exhaustive:
no file matching 'stash' turned up."* The bounded path announcing itself instead of
handing back a confident negative, and an explicitly requested Library still searched.

Pruning is a default, not a wall: Spotlight (`mdfind`) is still consulted first, over
`base` and with no prune list, so an indexed match inside `~/Library` reaches the user
even though the fallback walk will not descend there. Pruning bounds the *walk*, not the
search.

## 4. The `research` outlier, and three diagnoses I threw away

`Jarvis, research this idea.` is the slowest measured command at 1,722 ms, and every
millisecond of it is upstream: arXiv answers **406 Not Acceptable** to this client.
Getting there cost three wrong answers, each killed by measurement rather than by
argument:

| Claim | How it was tested | Result |
|---|---|---|
| "It is the User-Agent" | browser-like UA, then a descriptive self-identifying UA, 10 requests each | 406, 10/10 |
| "It is the plain-HTTP 301 hop" | switched `ARXIV_ENDPOINT` to `https` | still 406 |
| "It is HTTP/1.1 vs HTTP/2" | installed `h2`, retried with urllib **and** httpx | 406, 10/10 each |
| **What is actually true** | same machine, same minute | **curl 200, urllib 406, httpx 406** |

So it is a client-fingerprint refusal at the edge, not anything this repository controls.
Two consequences, both handled here rather than argued about:

1. **The endpoint is HTTPS now, permanently** — the 301 hop was real and removing it is
   correct on its own merits, even though it was not the 406's cause. The comment in
   `sources.py` says exactly that, so nobody re-litigates the redirect as a fix later.
2. **One refused source must not fail the command.** `search_papers` used to try arXiv
   and return `fail` on any exception. It now walks the approved source list (arXiv,
   then the rest), keeps a short retry inside the arXiv client for the *throttled* case,
   and answers from whichever source works — while naming what was refused:

   > I found no results for 'this idea.' on local. (arxiv: arXiv could not be reached: HTTP Error 406)

   If **every** source fails, the command fails, with each source's error in
   `output.source_errors`. It never substitutes invented papers; `test_no_invented_papers_when_every_source_fails`
   pins that. This is the difference between "research is broken" and a 1.5 s answer with
   a caveat the user can act on.

A bug in the fallback itself, found while writing its test: the skip-list compared
backends with `is not`, but `get_source()` constructs a fresh instance per call, so the
requested source was queried **twice** and the retry budget was silently spent on one
source. Now compared by `name`, pinned by `test_a_refused_source_is_queried_once_per_command`.

`h2` was installed for the HTTP/2 experiment and **uninstalled afterwards**; the
environment is as found.

## 5. A measurement bug in my own harness

`benchmarks/acceptance_run.py` timed a command, then, for a command that needed
confirmation, added `time.perf_counter() - started` to the elapsed value — the *total*
elapsed, not the increment. Any confirmed command was therefore published at roughly
double its real latency. Fixed to a single re-read of the stopwatch. No command in this
run took the confirmation branch, so the published numbers were unaffected; the bug was
still wrong, and it lived in the file the README tells readers to run.

## 6. Regressions

Written red first, then green. `tests/test_phase21_speed.py` — **13 tests**:

| Test | Pins |
|---|---|
| `test_a_search_has_a_scan_cap_at_all` | the bound exists; no unbounded walk returns |
| `test_pruned_directories_are_the_ones_nobody_means` | `Library`/`node_modules`/caches are the pruned set |
| `test_pruned_directories_are_not_walked` | pruning is real: the walk does not descend a pruned name *(index taken out of the path — see §8)* |
| `test_an_indexed_match_in_a_pruned_directory_still_answers` | pruning bounds the walk, not the search; the index answers first and the walk is skipped entirely |
| `test_an_unmatched_search_stops_within_its_budget` | worst case (a miss) is bounded, not best case |
| `test_a_stopped_early_search_says_so` | `truncated` and the entry count reach the reply and the audit detail |
| `test_a_complete_search_does_not_claim_truncation` | the bound does not cry wolf |
| `test_a_real_match_still_wins_before_the_cap` | bounding the walk cannot lose a genuine hit |
| `test_arxiv_request_identifies_the_client` | descriptive UA, HTTPS endpoint, no 301 hop |
| `test_arxiv_retries_before_giving_up` | one retry for the throttled case, then a real failure |
| `test_one_refused_source_does_not_fail_the_command` | fallback answers and names the refusal |
| `test_no_invented_papers_when_every_source_fails` | total failure stays a failure, with per-source errors |
| `test_a_refused_source_is_queried_once_per_command` | the `is not` / compare-by-name regression |

Suite: 1290 → **1304 collected, 1300 passed, 0 failed**. The `1 skipped` is
`tests/test_research.py:423`, which skips itself when arXiv is unreachable — i.e. the
406 of §4, reported as a skip rather than passed off as green. The `3 xfailed` are the
three Phase 10 memory-deletion limits documented at their own source (`_remember_outcome`
echoes, the hardcoded `deleted: True` HTTP route, the process-local embedding cache),
unchanged since they were written. Run twice, identical.

## 7. Documents and figures, re-generated from the data

- `README.md`: acceptance table and the **Smooth and fast** section rewritten against the
  freshly committed `benchmarks/*.json`; the arXiv entry in *known open items* now states
  the measured curl/urllib/httpx split instead of the earlier guess.
- `benchmarks/acceptance_run.py`: `("21 speed", 1304, 1300, 0, …)` added to the phase
  series, and `suite_history.json` regenerated (the JSON had been written before that
  entry existed, so the growth chart was one phase behind the code).
- `assets/build_charts.py` plotted every point from `benchmarks/suite_history.json` but
  hardcoded its own two labels (`TEST SUITE THROUGH PHASES 9-20`, `AT PHASE 20 CLOSE`).
  Adding the phase-21 point therefore silently grew the plot under a phase-20 title — the
  kind of figure that stays right only until the next phase. Both strings are now derived
  from the data, and the rebuilt chart reads **9-21 / AT PHASE 21 CLOSE** over thirteen
  points. Charts and diagrams rebuilt; **all 7 SVGs parse as XML** (`xml.etree`), the
  check that previously caught a nested-quote `font-family`.
- `benchmarks/hardware_status.json` carried this machine's random scratch path in
  `security.token_path`, so every regeneration churned a line that means nothing about
  the product. Now written as `<JARVIS_HOME>/api_token`. The token itself was never in
  the file; only the path was.
- New: this report.

## 8. CI caught what this laptop let through

The first pushed commit (`4085a5b`) **failed CI on Python 3.12** with one test:

```
FAILED tests/test_phase21_speed.py::test_pruned_directories_are_not_walked
  assert [{'name': 'wanted-file.txt', 'path': '.../T/pytest-of-runner/.../Library/deep/wanted-file.txt', ...}] == []
1 failed, 1274 passed, 25 skipped, 3 xfailed
```

Local: green. CI: red. The code is identical on both. The difference is **which search
path answered**:

* `search_files` asks Spotlight first (`mdfind -onlyin <base> …`), and falls back to the
  bounded walk only when the index returns nothing.
* Pruning applies to the *walk*. The index is not pruned — deliberately, §3 says so.
* On the author's laptop, Spotlight has not indexed a file created two milliseconds ago
  in a temp directory, so the walk ran, pruning held, the test saw `[]`.
* On GitHub's macOS runner the same query *does* return the file, so the index answered
  from inside `Library/`, and an assertion written as "pruned directories are never
  searched" was contradicted by the other half of the same tool.

So the test encoded a machine property, not a product property, and passed for the wrong
reason. Fixed by naming the invariant the test actually owns:

* `test_pruned_directories_are_not_walked` now takes the index out of the path
  (`_no_index`, which stubs `shutil.which`) and asserts the **walk** does not descend a
  pruned name. Deterministic on any machine.
* `test_an_indexed_match_in_a_pruned_directory_still_answers` is new and asserts the
  other side of the same design with a canned index reply: the index wins, the result
  comes back, and `scanned == 0` proves the walk never ran. Nothing is left to whatever
  macOS felt like indexing.

Neither change weakens the product rule, and no permission or bound was touched to make
CI green — the bound, the pruning and the disclosure all still fail a test if removed. The
25 skips on the runner against 1 here are capability gates doing their job (no battery,
no local hardware, no paired accounts), which is the pattern established when the Phase 20
runner failure was diagnosed rather than papered over.

Two claims in this report and in the test file's own header were also wrong in the same
direction — written from a plausible mechanism instead of from measurement — and are
corrected here rather than quietly: §6's table row for
`test_pruned_directories_are_not_walked` said "a file inside a pruned dir is not reached"
(it is, via the index, and that is by design), and the module docstring asserted the 406
was urllib's default `Python-urllib/3.x` User-Agent, which §4 measured and disproved.
Both now state the precedence and the fingerprint finding.

## 9. Carried forward

Made true here: worst-case search latency; a refused upstream source no longer failing a
command; a published benchmark harness that double-counted confirmed commands; a chart
whose title disagreed with its data; `benchmarks/suite_history.json` lagging the code; and
one test whose green colour was a property of the laptop that ran it (§8).

Still open, unchanged in kind: **15-F** arXiv's client refusal (now degraded gracefully,
still not working — 1,722 ms and no arXiv results per call) · **19-B** refusal vs
clarification both surfacing as `declined` · **14-E kin**: no `read_file` route, no
notification tool, `POST /memory/{id}/forget` overclaims · **15-H** which calendar store
was read · desktop bundle exits 1 · hardware **BLOCKED** (nothing to pair) · LLM planner ·
multi-step unattended workflows · 16-F's security-event surface, a user decision.

## 10. Git and browser

Two commits carry this phase. The first (`4085a5b`) shipped the bounded walk, the source
fallback and the re-measured documents — and failed CI on Python 3.12 for the reason in §8.
The second rewrites that one test so it asserts the walk instead of the machine, adds the
precedence test, corrects the two claims written from mechanism rather than measurement,
and re-syncs every published count (1304 collected / 1300 passed).

Both pushed to `origin/main` as fast-forwards on the author's own private repository;
nothing force-pushed, no history rewritten. The first push attempt was rejected by a
transient GitHub error (`remote: fatal error in commit_refs`) and succeeded on retry — the
only re-run of a command in this phase, and it changed nothing but the transport.
**CI after the first commit: failed (1 test). CI after the second: reported by
`gh run list` when it finishes; this file will not claim green before it is.**

**LIVE BROWSER DRIVE: NOT PERFORMED.** The proof in §1 is HTTP against a launched uvicorn
server plus the spoken pipeline through `VoiceSession`, and §1-§4's sweep is HTTP against
the real ASGI app plus pytest; the dashboard page was not driven in a browser this phase.
No claim about what a user sees on screen is made beyond what `dashboard_state` in
`benchmarks/acceptance_latency.json` shows — that column comes from the shipped
`pipelineState()` function executed by Node on the real response payloads, which is
stronger than a screenshot of one command but weaker than operating the page.

Server stopped (port 8765 has no listener); scratch homes left under `/tmp` for
inspection; `~/.jarvis-forge` never written — its newest file was last touched before this
phase began.

