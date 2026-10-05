# Final-audit addendum — the cold start was being called a stall

| | |
|---|---|
| Follows | `VANTAGE_FINAL_ACCEPTANCE_REPORT.md` §7, which recorded this as "cause not established" |
| Date | 2026-10-05 |
| Starting commit | `455a64c` (final audit close-out) |
| Ending commit | `5e90b81` (the fix) → `0896589` (the redeploy record; its own hash is written by the next commit to touch this file) |
| Tests before | 2383 passed · 0 failed |
| Tests after | **2387 passed · 0 failed** (77.2 s), verified on **6 consecutive full-suite runs, 0 failures** |
| Before, measured today | five `test_vision.py` OCR tests failed on **4 of ~12** full-suite runs, always as a group of exactly five, and passed on an immediate re-run |

---

## 1. The report said "cause not established". It was not a label to keep.

The final acceptance audit published the flake honestly and stopped there. That is the wrong place
to stop: an unexplained failure in the suite is a hole under every number the suite is used to
justify. So this addendum exists because the finding was chased, and the chase changed the answer.

Three hypotheses were tested and **two were falsified by measurement**, which is the part worth
writing down:

| hypothesis | experiment | result |
|---|---|---|
| CPU contention makes the recogniser slow enough to fail | 16 busy-loop processes, then `pytest tests/test_vision.py` | **falsified** — 48 passed in 6.3 s |
| a concurrently running Vantage host contends for Vision | second host on port 8921 with a live scheduler, vision tests and then a full suite alongside | **falsified** — 48 passed / 2383 passed |
| one real call exceeds the fixed budget, and the cooldown turns it into five | traced every recogniser call inside full-suite runs until it reproduced | **confirmed**, twice |

The traced reproduction:

```
20.02s  ok=False timed_out=True                    <- the only call that actually ran
 0.00s  ok=False degraded=True cooldown=44.9s      <- and four more, refused instantly
 0.00s  ok=False degraded=True cooldown=44.9s
 0.00s  ok=False degraded=True cooldown=44.9s
 0.00s  ok=False degraded=True cooldown=44.9s
FAILED tests/test_vision.py::test_ocr_reads_real_text
FAILED tests/test_vision.py::test_ocr_returns_bounding_boxes
FAILED tests/test_vision.py::test_find_text_locates_a_word
FAILED tests/test_vision.py::test_read_image_text_tool
FAILED tests/test_vision.py::test_find_image_text_tool
```

A second reproduction, after the first fix attempt, showed the same shape at **45.02 s** — which is
what proved the first fix was moving a threshold rather than finding a cause.

## 2. What the numbers said

Same image, same helper binary, one process, measured directly:

```
steady state                0.28 s a call (25 successive calls, 0.27-0.31 s spread)
a process's first call      7.40 s   (idle machine, helper already compiled)
that first call, mid-suite  20 s and 45 s observed -- i.e. over the budget
```

macOS Vision pays a large one-time initialisation the first time a process asks it anything, and the
cost grows when the machine is doing other things. Vantage's OCR guard is a **20 s** steady budget
plus a **45 s per-process cooldown** after a timeout. So the first call — the one that is not
steady — was measured against the steady ceiling. When it lost that comparison, the cooldown made
the loss contagious, and exactly five tests failed together. The cluster and the "passes when
re-run" behaviour were both explained by the same mechanism.

## 3. Two changes, each justified by its own measurement

**Product (`jarvis/vision/ocr.py`).** A process's first recogniser call gets a distinct, documented
budget (`_COLD_START_SECONDS = 45`, capped at 120, reverting to the steady budget the moment one
read succeeds — `_budget()`). This is not a test convenience: a person's first "read this image"
after launch is the same cold call, and holding it to a steady-state ceiling means Vantage tells
them the recogniser stalled when it was starting up. `state()` now publishes `cold_start_seconds`
and `stalls`, so the allowance is visible on `/health` and in `vision_capabilities` rather than
being a number only the source knows.

**Tests (`tests/test_vision.py`).** A session-scoped autouse fixture pays the framework's cold start
once, before any test measures it, asserting nothing about that call. The five tests are unchanged:
same images, same real framework, same assertions about recognised text. What was removed is an
inherited assumption that macOS framework initialisation time is a property of Vantage — the tests
were measuring it, and failing when it moved. No test was skipped, no assertion was weakened, and
nothing was marked xfail.

**What was deliberately not done.** The first draft of the fix also relaxed the guard: a process
that had never succeeded would be allowed two stalls before cooling down. Three of Phase 27's tests
describe the opposite rule, and re-reading them showed the guard was right and my relaxation was
the symptom-chasing move. It was reverted, and a test now records the decision —
`test_a_stall_still_cools_the_recogniser_down_on_a_cold_process` — so the guard cannot be quietly
loosened later to make a run look green.

## 4. Verification

Four new tests, written red-first (all six drafts failed before the implementation), then:

```
before  5 of ~12 full-suite runs failed with the five-test cluster
after   6 consecutive full-suite runs: 2387 passed, 0 failed
        durations 76.80s, 76.99s, 76.80s, 77.14s, 77.33s, 78.13s
```

The duration spread is part of the evidence: the failing runs used to take 95–121 s, because a
timed-out call plus four cooldown-refused calls costs more wall clock than the work it was
protecting against. Tight numbers around 77 s say the cascade is gone, not merely that one run
passed.

**The limit of this claim, stated rather than implied:** the mechanism that produced every observed
failure is removed, and six consecutive runs corroborate it. A machine slow or loaded enough to
push a cold first call past 45 s would still time out — by design, with a truthful message and its
number in it. This is not a proof of impossibility; it is the removal of the cause that was
actually observed, twice traced.

## 5. The instrument was wrong twice, and both times it was my fault

Two things in this investigation produced false evidence, and both are worth recording because the
instinct in the moment was to trust them:

1. The first trace version forgot `return result` in the wrapper, so `read_text` returned `None` and
   **ten** tests failed. For a minute those ten looked like a bigger flake than the five-test
   cluster. They were the instrument. The run was discarded, not quoted.
2. The first cold-call probe ran against `/tmp/x.png`, which does not exist, and `read_text` refuses
   a missing file *before* consulting the helper — so the budget was never reached and the numbers
   were meaningless. Reading the returned error ("I could not find /tmp/x.png") rather than
   assuming the stub was at fault is what exposed it, and the test now creates a real file on disk
   with that reason in a docstring.

A third, quieter one: `-k "stalled or cooldown or timeout"` selects the tests that *stub* a stall.
Those passed on cold-home runs and looked like the guard had fired harmlessly, while the cold cost
had been paid by a different file's test entirely. Selecting by keyword is not selecting by cause.

## 6. Numbers, re-measured

| | before | after |
|---|---|---|
| Tests | 2383 · 0 failed | **2387 · 0 failed** (77.2 s) |
| Test modules | 89 | **90** |
| Vision-OCR failures today | 5 in 4 of ~12 runs | **0 in 6 consecutive runs** |
| Steady / cold call | 0.28 s / 7.4 s → 20 s+ | unchanged; the cold call now gets a matching budget |
| Tools / gated / rules / routes | 148 / 32 / 52 / 122 objects | unchanged |
| `audit/` reports | 63 | **64** (this file) |

Browser verification: **NOT APPLICABLE** — no page changed. Nothing here is visible in a browser;
`/health` gained two OCR state fields that no page reads yet.

## 7. Open items

- **62-A — the delivered app's own bundle has not been re-measured for cold-start OCR.** The
  numbers above come from the repo tree's python. The `.app` runs the same code with the same
  helper cache; asserting it behaves identically would need a timed OCR call against the delivered
  instance, which writes into the user's data directory and so was not done (see the standing rule
  about never probing writes on the live app).
- **62-B — `cold_start_seconds` and `stalls` are published and read by nobody.** The panel shows
  OCR as available/unavailable; the difference between "starting" and "stalled" is now measured and
  reported by the server, and still invisible to a person. A small, honest UI job.
- **62-C — the 45 s cold allowance is one machine's measurement.** 7.4 s idle and >20 s under load
  on this Air; the ceiling is capped at 120 s and clamped by the sentence budget, so a slow machine
  cannot hang a turn, but the number itself is not portable and is not claimed to be.

Nothing was pushed.

## 8. Delivery

Work commit `5e90b81` — *Measure a cold Vision start as what it is, not as a stall*.
Close-out commit `0896589` — *Record the cold-start addendum's redeploy, and the eight clean runs*
(its own hash was added by the commit after it, because a file cannot carry the hash of the commit
that contains it).

`jarvis/vision/ocr.py` is shipped code, so the bundle was rebuilt and re-deployed:

| check | result |
|---|---|
| `codesign --verify --deep --strict` on the deployed bundle | valid, satisfies its Designated Requirement |
| the fix present in the staged source, twice over (`_COLD_START_SECONDS`) | in `~/Downloads/Vantage/Vantage.app`, and again after extracting `Vantage.app.zip` into a temp dir |
| `GET /health` | `status ok`, **148 tools** |
| `GET /tools` | **148** entries |
| archives | `Vantage.app.zip` 1,173,369 B and `vantage-source.zip` 2,561,327 B, both rebuilt from this tree; the source archive carries this addendum |
| suite after the fix | **2387 passed · 0 failed** — eight consecutive clean full-suite runs (76.8–98.9 s) |

Nothing was pushed, and no user-visible behaviour changed beyond OCR being given a truthful budget on
its first call.
