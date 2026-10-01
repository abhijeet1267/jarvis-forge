# Phase 34 — one sentence, one deadline

Measured on this tree, this machine, 2026-10-01→02. Closes 29-B, which 29 recorded as
"no bounded read timeout on the arXiv path (measured 41,247 ms)". That number was real and
the conclusion drawn from it was too generous to the code.

---

## 1. Measured before anything was changed

The path does set a timeout, twice:

```
jarvis/research/sources.py:45   REQUEST_TIMEOUT = 20.0
jarvis/research/sources.py:186  urlopen(request, timeout=REQUEST_TIMEOUT)      # attempt 1
                                urlopen(request, timeout=REQUEST_TIMEOUT)      # attempt 2
```

`urlopen`'s timeout is a **socket inactivity** timeout. A server that answers and then
sends one byte every 10 ms never trips it. Measured against a local trickler standing in for
an arXiv that is neither up nor down:

```
STILL WAITING after 60.0s -- socket timeout is 20.0s per inactivity gap, total unbounded
```

The first red run of this phase's own tests did not fail. It hung, and had to be killed at
five minutes. That is the defect, demonstrated by the test suite's own behaviour rather than
by argument: 41,247 ms was not the bound, it was what one real server happened to take.

The same shape sat at the other two `urlopen` sites: `memory/embeddings.py:233` (30 s) and
`vision/providers.py:129` (60 s) -- and the embeddings one is inside `remember that …`, so a
stalled embedding endpoint holds a sentence that has nothing to do with research.

Then the aggregate was measured, after per-read bounds existed:

```
"remember that the heater coil arrived"   elapsed 10.0s | outbound reads: 2
    http://…/embeddings   budget 30.0
    http://…/embeddings   budget 30.0
```

Two reads, thirty seconds each. The wait a person faces is the sum, so "30 seconds" in the
source was not a statement about any wait they would ever observe.

## 2. What changed

**`jarvis/net.py`** — `http_read(request, total=…, what=…)`. The `urlopen` call stays, so
whatever patches it in tests still works; what wraps it is a worker thread joined with a real
deadline, so `total` is a ceiling on the whole call rather than on the gaps inside it.
`DeadlineExceeded` carries the number in its message, because "it timed out" with no figure is
the same unexplained silence this module exists to remove.

The honest limitation, stated where the code is: a Python thread cannot be killed, so when a
budget expires the reading thread is abandoned and its socket closes when the response is
collected. A stray read for a while, against a caller waiting forever. That trade is written
into the module docstring rather than hidden in a `daemon=True`.

**Three readers converted.** arXiv's two attempts now share one budget through
`remaining_of()`, and a second attempt with no time left is refused by the clock -- it does
not start a wait it cannot finish. Embeddings and the vision provider keep their own numbers
(30 s, 60 s) as **total** budgets, so the constants mean what they say.

**`SENTENCE_BUDGET = 60.0`, one per request.** `handle()` wraps the whole pipeline in
`request_budget(...)`, and `http_read` clamps each read to `min(its own cap, what the sentence
has left)`. Sixty is deliberately wider than any single call's own budget: a legitimate 60 s
vision read may use the whole allowance, and then nothing else in that sentence gets any. A
`ContextVar` rather than an argument threaded through every call, because the reads happen two
and three layers down and a leaked budget would eventually refuse every network call the
process makes -- which is why it is reset in a `finally`, and tested.

**A timeout is not an empty shelf.** The arXiv path reports the deadline in the same words it
uses for a refusal, so `search_papers` answers *"…on local. (arXiv did not answer within 1s…)"*:
the empty result stays attributed to the shelf that really is empty, and the source that went
quiet is named. Phase 21 spent a phase on that distinction; a timeout landing in the same
reply as "nothing found" would have reopened it.

## 3. Live, over HTTP, on a launched server

`python run.py serve` (in-process, temp `JARVIS_HOME`, sandboxed `HOME`), with
`JARVIS_LLM_BASE_URL` pointed at a socket that sends a byte every 10 ms forever -- an endpoint
that is never silent and never finishes, which is the only kind a socket timeout cannot stop.

```
  POST /command "remember that the heater coil arrived"  returned in 35.1s
      reply: Got it. I will remember: the heater coil arrived
  budgets now: 30s per read, 60s for the sentence; before this phase the total had no end

  POST /command "what time is it"                        returned in 5.0s
      reply: It is 00:08 on Friday, 02 October 2026.
```

35.1 s is two bounded reads plus indexing, inside a 60 s ceiling: the fact was still
remembered, and the API thread came back. Before this phase the same request had no answer at
all -- it was still open when the process was killed. The normal sentence is untouched.

## 4. Findings this phase made about itself

1. **A number in a `timeout=` argument is not a promise about latency.** Two of these three
   sites had been read as bounded for their whole lives; `REQUEST_TIMEOUT = 20.0` invited
   exactly that. The name is now `total` at the call, and the comment says what it bounds.
2. **Bounding each read is not bounding the request.** Found by counting, not designing: the
   sentence that performed two reads doubled the wait. The per-sentence ceiling is the
   statement a reader can actually use.
3. **The tests were the evidence, not just the check.** A hanging test run is usually treated
   as tooling trouble. Here it was the finding: the only honest way to demonstrate an
   unbounded wait is to be subjected to one.
4. **What `research this idea` measures changed without the network changing**: 41,247 ms in
   Phase 29's run, **714 ms** in this one. That is not this phase's doing and is not claimed
   as it -- it is the reason latency numbers are quoted with their run.

## 5. Regressions

`tests/test_phase34_one_sentence_one_deadline.py`: 11 tests against a local trickler and a
well-behaved server, written red first. Two attempts of the same search share one budget; the
fast path still parses real XML; a timeout names itself and its number instead of reading as
an empty result; nothing is invented when the clock runs out; the vision and embedding reads
are bounded through their public entry points; the second read of one sentence sees ≤1 s of a
4 s budget; the budget is returned after the sentence; and two sweeps -- every `urlopen` in
`jarvis/` goes through `http_read` (except the definition in `net.py`), and the six
silence-bounded `httpx.Client(timeout=…)` sites are *counted*, so a seventh is a red test
rather than a quiet addition.

No existing test was changed. Full suite: **1653 passed, 0 failed, 0 xfailed** (1653
collected), up from 1642.

## 6. Re-measured

| metric | before | after | how |
| --- | --- | --- | --- |
| one arXiv search against a server that never stops | no bound (60 s observed, still waiting) | ≤20 s, both attempts together | trickler test |
| outbound reads in `"remember that …"` | 2 × 30 s = 60 s of wait labelled "30" | 2 reads inside a 60 s sentence ceiling | counted, then bounded |
| `urlopen` sites with a total deadline | 0 of 3 | 3 of 3 | sweep test |
| httpx sites still silence-bounded | 6 | 6, now pinned by a ratchet | sweep test |
| `research this idea` on the real network | 41,247 ms (Phase 29) | 714 ms (this run; not attributed to this change) | `python benchmarks/acceptance_run.py` |
| §45 safety matrix | 6 of 6 | **6 of 6** | `python benchmarks/safety_run.py` |
| §59 acceptance sweep | 17 of 18 | 17 of 18, VERIFIED 7 | same |
| suite | 1642 | 1653 | `python -m pytest -q` |

## 7. Carried forward

* **34-A** six `httpx.Client(timeout=…)` sites (device calendar 20 s ×4, watch gateway,
  router 30 s, STT 60 s) still bound silence, not duration, and are not behind
  `http_read`. They were left because each needs its own client-level reading and none was
  measured hanging; the ratchet test makes the number impossible to grow by accident.
* **34-B** the abandoned read keeps its socket open until garbage collection. A `total` that
  is much smaller than a server's dribble interval leaves more of these around than the
  defaults do; a connection pool or `httpx` with an explicit `read` + `pool` timeout would
  close it properly.
* **34-C** `SENTENCE_BUDGET` is one number for every sentence. A voice turn wants a far
  tighter ceiling than a research brief; making it per-surface is a policy choice about
  which sentences are allowed to be slow, not a mechanism.
* Everything already open: 33-A…C, 32-A…C, 31-A/31-B closed in 32, 30-B…D, 29-A, 29-C,
  29-D, 28-A narrowed, 27-A…D, 26-B, the `ToolSpec.parameters` residual, 23-E, mic/speech
  TCC, 15-F, 14-D, 16-F, hardware **BLOCKED**, LLM planner, unattended workflows.
* Closed here: **29-B**.

---

## 8. Git

Committed and pushed to `main` on `abhijeet1267/jarvis-forge`:

```
e5c80a6  A network read gets a total deadline, and a sentence gets one budget
         14 files changed, 782 insertions(+), 36 deletions(-)
```

### The runner's verdict, measured after the push

Run `36910545352`, both jobs green:

| job | result | pytest line | wall |
| --- | --- | --- | --- |
| `suite (3.11)` | success | `1646 passed, 7 skipped, 1 warning in 107.32s` | 18:56:18 → 18:58:49Z |
| `suite (3.12)` | success | `1646 passed, 7 skipped, 1 warning in 97.22s` | 18:56:14 → 18:58:28Z |

`1646 + 7 = 1653`, so nothing was lost between the machines — but the skip count moved from
the six accounted for in Phase 29 (five battery-gated, one `dist/`-gated) to **seven**, and
the extra one is **not named here**: `pytest -q` reports counts without reasons. What is
measured is the candidate set and one signal pointing inside it. The six from Phase 29 still
account for themselves, so the seventh must come from a runtime skip, whose mechanisms are
the arXiv reachability skip (`tests/test_research.py:423`), the Vision helper sites in
`tests/test_vision.py`, `ps` availability, and `node` — the last two are ruled out because the
dashboard tests that gate on `node` would then have skipped by the dozen, and the run grew by
16–23 s of wall time against the previous one (81.4 s / 84.3 s), which is what an arXiv
attempt that has to be waited out costs. That makes the arXiv skip the likely seventh and
keeps it an inference, not a fact.

The distinction matters for this phase specifically, because the arXiv path is the one it
changed: a skip on the runner means the *real-network* leg was not exercised there, so the
evidence that the deadline leaves the working path intact is the local run (0 skipped, arXiv
answering, `research this idea` at 714 ms in `benchmarks/acceptance_latency.json`) and the
fast-path test against a server that declares the length it actually sends — not CI.

The §45 matrix re-ran on the runner and still holds 6 of 6, heater row included.

### Follow-up, measured on the addendum's own run

The addendum commit `9e0648e` triggered run `36911749436`, green, and it reported
**`1647 passed, 6 skipped`** on both images — `1647 + 6 = 1653`, and back to the six that
Phase 29 accounted for.

That settles the inference above in both directions. The seventh skip was there on one run
and gone on the next, with the tree unchanged in any way that touches it: which is exactly
how a network-reachability skip behaves. It also retires the caveat attached to it — on this
run the runner *did* exercise the real arXiv path and the test passed, so the deadline is
verified against a live upstream on a machine that did not write it, not only locally.

The honest residue is the method, not the fact: `pytest -q` gives counts without reasons, so
two runs were needed to identify a skip that a single run could only guess at. A `-rs` on the
CI invocation would make the next one a measurement instead — recorded with 29-D as a CI
change this phase did not make on its own authority.
