# Phase 65 — one stall, five failures

Date: 2026-10-05. Repo: `jarvis-forge/`, HEAD at start `2152cf8` (Phase 64).
Follows: `audit/FINAL_AUDIT_COLD_START_NOT_A_STALL.md`, whose central claim this phase disproves.

## 1. Why the phase exists

That addendum said the five recurring `tests/test_vision.py` failures were diagnosed and fixed:
a cold Vision initialisation had been handed a steady-state 20 s budget, counted as a stall, and
the 45 s cooldown after it took four more tests with it. Six clean suites were published as proof.

Today the same five tests failed again — `2403 passed, 5 failed in 168.72s`, with
`test_find_image_text_tool` among them — after two clean runs at 116 s and 142 s. So the fix was
reported on evidence that did not survive. That is the thing this phase has to answer, and it is
not answered by running the suite until it goes green again.

One process error, recorded because it matters for the next person: the failing run's output was
piped through `tail -2`, so the five assertion messages and the budgets in force were **lost**. The
fix for that is in §5.

## 2. What was measured, and what it rules out

Every number below was measured on this machine today. The image is the one the tests draw
(600×200, "Project AMR status report" / "Battery 74 percent charging"); the helper is the same
compiled Swift binary the product uses.

| # | Claim being tested | Measurement | Verdict |
| --- | --- | --- | --- |
| 1 | "macOS Vision is intermittently unresponsive under load — 0.6 s standalone, exceeds any bound while other work runs" (`jarvis/vision/ocr.py`, since Phase 27) | 60 helper calls made **while a full 2,408-test suite ran in another process**: first 10.2 s, then 0.51 s, 0 calls over 15 s, 0 non-zero exits, load suite exited 0 | **Not reproducible.** The comment asserted a measurement that does not reproduce |
| 2 | "the cold first call takes 7.4 s idle and over 20 s with the suite running" (the addendum's basis for the 45 s allowance) | cold first call in a fresh process: **7.32 s idle**, **9.47 s with 10 busy CPUs**, **10.2 s under live suite load** | Never approaches 45 s at any load this machine will generate. The allowance is harmless but it was not the fix |
| 3 | "first **exec** of a freshly compiled binary is the slow step" (an assessment cost my earlier probes avoided by reusing a binary that had already run) | brand-new `JARVIS_HOME`s with a binary that had never been executed, while a full suite ran: build 1.88 / 2.01 s, **first exec 0.89 s and 1.21 s**, both `ok`, second exec 0.28–0.30 s (`/tmp/p65_firstexec_run.txt`) | **Falsified.** An earlier four-run batch of this experiment printed plausible numbers too, but its output was never saved, so it is not cited here |
| 4 | "some test patched the global `subprocess.run` and left it patched, so every later real read is a fake timeout" | a pytest plugin compared `subprocess.run.__code__` with an untouched reference before each of the five real tests: **45 observations across nine instrumented sessions, `run_real: True` in 45 of 45** | **Falsified** in every captured run. The three places that patch it all restore it |
| 5 | "the shared backend inherits another module's cooldown" | the same 45 observations: `cooling: 0.0` in 45/45, `stalls: 0` in 45/45, and `ever_ok: False` with `budget: 45.0` at the first real test of every session (the singleton is untouched until then) | Not the cause in any captured run |
| 6 | **the cascade itself** — one exhausted budget on the backend the tools use silences the rest of the session | forced on demand: §3 | **Confirmed, deterministically** |

What survives is narrow and it is the part that was always true: the guard converts one slow call
into a 45 s refusal window for that backend, and the five tests that assert what the recogniser
reads all share that backend.

## 3. A reproduction instead of a wait

Waiting for a loaded machine to misbehave is not a method. A pytest plugin (`/tmp/p65force.py`,
deliberately not committed) raises `TimeoutExpired` **once**, on the first call made by the module
singleton, *inside* `read_text`'s own `try` so the product records the stall exactly as it would in
the wild, and lets every later call through.

| | same forced condition | |
| --- | --- | --- |
| before this phase's change | **5 failed, 44 passed** | `test_ocr_reads_real_text`, `test_ocr_returns_bounding_boxes`, `test_find_text_locates_a_word`, `test_read_image_text_tool`, `test_find_image_text_tool` |
| after | **1 failed, 48 passed** | only `test_ocr_reads_real_text`, and its failure line now prints the recogniser's own sentence |

Both rows were re-run for the record with the output saved (`/tmp/p65ab.txt`), the "before" row by
checking out `HEAD~1`'s copy of `tests/test_vision.py` and restoring it immediately after. That
second attempt is what makes the 5 → 1 claim citable rather than remembered.

The first version of that plugin stalled the *first helper call anyone made* — which is the warm-up
fixture's throwaway instance — and **nothing else failed**. That accidental result is Phase 63's
instance isolation working as designed, and it is why the plugin had to aim at the singleton
specifically.

## 4. The change

`tests/test_vision.py` — one fixture and five signatures:

```python
@pytest.fixture()
def reader(monkeypatch):
    backend = ocr_module.OCRBackend()
    if not backend.available():
        pytest.skip("OCR helper unavailable")
    monkeypatch.setattr(ocr_module, "_backend", backend)   # the tool path calls get_ocr_backend()
    return backend
```

Each of the five content tests now reads through a backend of its own: the same real helper binary,
the same real PNG on disk, the same real Vision framework, the same budgets and the same guard —
only the *scope* of the cooldown changes. **No assertion was altered, nothing is skipped, nothing
is widened.** `monkeypatch` hands the real singleton back at teardown, so this cannot leak into
another module the way Phase 33's stub handlers did.

The reason is stated in the fixture rather than assumed: a refusal is a product behaviour, and a
test that was never executed is not a measurement of anything. Five red rows from one event
describe the harness, not the machine. The fact this leans on — that a stall belongs to one
instance and not to the module — is itself under test in
`test_a_stall_on_one_backend_cannot_silence_another`.

`jarvis/vision/ocr.py` — comments only, no behaviour. The two numbers that had been published as
measurements ("exceeds any bound while other work is running", "over 20 s when the suite is
running") are replaced by what today's probes actually returned, with the falsifying sentence left
quoted so the correction is auditable. `_COOLDOWN_SECONDS`, `_COLD_START_SECONDS`,
`_MAX_BUDGET_SECONDS`, `_budget()` and the timeout branch are untouched, as are the three Phase 27
tests that describe the guard; the test that pins that decision (`test_a_stall_still_cools_the_recogniser_down_on_a_cold_process`) still passes.

## 5. What happens the next time it fires

Previously: `assert result["ok"]` → `AssertionError: assert False`, which says nothing about why.
Now each of the five carries the recogniser's own sentence, e.g.

> `The macOS recogniser stopped answering within 45s, so I am not queueing more work on it for another 44.8s. No text was read.`

So the next occurrence records the budget in force and whether it was a stall or a refusal, in the
failure line, without a plugin and without re-running anything. `GET /health` already reports the
same state (`stalls`, `cooling_down_seconds`, `timeout_seconds`, `cold_start_seconds`) for the
running process.

## 6. Numbers

- Focused: `tests/test_vision.py` **49 passed** (unchanged count — this phase adds no test, it
  rewires five and corrects two comments).
- Forced reproduction, saved: **5 failed, 44 passed** before → **1 failed, 48 passed** after, the
  same injected stall, the same five names.
- Full suite after the change: **2408 passed, 0 failed in 83.57 s** (and 93.24 s on the run just
  before it).
- Today's full suites, read out of the saved result files rather than from memory: 81.20 · **168.72
  (5 failed, 2403 passed)** · 95.30 · 84.36 · 86.62 · 84.61 · 93.09 · 84.18 · 83.09 · 82.91 · 80.03 ·
  81.58 · 93.24 · 86.83 · 83.57 s — **fifteen clean, one red, and the red one is twice the duration
  of any other**. (The day's first suite ran the 2,407-test tree in 116.47 s and was clean; its
  output lives in the runner's task file outside the workspace and could not be re-opened for this
  list, so it is counted separately.)
- Tools 148 / 32 gated / 52 planner rules (`len(core.planner._rules)`) / 32 DB tables / 118 route
  objects unchanged.
- Versioned lines for this phase's Python: **+43** net (23 added, 14 removed in `ocr.py`; 43 added,
  9 removed in `tests/test_vision.py`), which is the same 43 that reconciles the two line-count
  instruments on 75,755 → 75,798.
- `suite_history.json`: **58 rows**, verified by re-reading the written file: last row
  `65 one stall five failures`, `64 the api shape is not for passersby` still present, nothing
  removed. (The first append of this row reported success and had not written — the reason every
  number in this report was re-read from disk afterwards.)

## 7. Not established, and what would establish it

**The trigger of the slow call is not identified.** Four candidate causes were measured and
falsified (§2), and the one run that failed today had its diagnostics discarded by my own `tail`.
The honest state is: an occasional `subprocess.run` of the Vision helper exceeds its budget on this
machine for a reason this project has not demonstrated, in a suite whose arrangement no longer lets
that single event fail five tests.

What would establish it: the next red run's failure line (it now carries the budget and the
refusal/stall distinction), plus `vision_capabilities` state at that moment. If a stall ever shows
up at, say, 45 s with `stalls: 1` and nothing else in the process table, the question becomes the
child's own behaviour and gets measured with `sample`/`spindump` rather than guessed at.

**65-A (open, deliberately):** the guard is per backend instance. For a person using Vantage that
means one slow image read can make the next few reads say "not answering right now" for up to 45 s,
even though the recogniser is answering fine at 0.5 s in every measurement taken today. Whether
that is the right product behaviour is a decision with a user in it — shortening the cooldown, or
retrying once before cooling, are both changes to a guard that three Phase 27 tests describe — so
neither was made here.

## 8. Corrections this phase had to make to itself

Worth recording because the mechanism is the same one the phase is about: a number that reached a
report without being read back from the executable artifact.

| Published in draft | Truth, read from a file | How it was caught |
| --- | --- | --- |
| suite durations "116 / 142 / 168 / 95 s, thirteen runs" | the second clean suite was **81.20 s**, and the day holds **sixteen** runs, not thirteen | `grep -o "…in [0-9.]*s"` across every saved result file instead of trusting the earlier read |
| "4 fresh homes, first exec 0.46–2.01 s" | that batch's output **was never saved**; the two runs that were give 0.89 s and 1.21 s | the evidence file did not exist, so the claim was replaced with what can be cited |
| "9 instrumented full suites" | **45 observations across nine sessions**, with `run_real`/`cooling`/`stalls` tallied per field | a tally script over the logs rather than a remembered count |
| Phase 64's "76,123 versioned lines" | **75,755** at that commit | two independent instruments asked to agree (see §6 of the Phase 64 report) |
| a "142 s" Phase 64 suite, and "ten consecutive clean runs" | 81.20 s, and the streak is broken by the 168.72 s red run | the same sweep that fixed the durations |

The forced-stall A/B was also re-run with the output saved, and the "before" number obtained by
checking out `HEAD~1`'s copy of the test file and restoring it immediately, so neither row in §3
rests on a remembered screen.

None of this changes what the phase concluded: the four causes were genuinely falsified, and the
5 → 1 blast-radius result genuinely reproduced. What it changes is that the numbers in this report
now come from files, not from what a tool appeared to print -- which is the only reason a reader can
repeat them.
