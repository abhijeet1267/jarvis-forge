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
| 3 | "first **exec** of a freshly compiled binary is the slow step" (an assessment cost my earlier probes avoided by reusing a binary that had already run) | 4 brand-new `JARVIS_HOME`s under suite load: build 0.70–0.73 s, first exec **0.46 / 0.46 / 0.52 / 2.01 s**, all `ok` | **Falsified** |
| 4 | "some test patched the global `subprocess.run` and left it patched, so every later real read is a fake timeout" | a pytest plugin compared `subprocess.run.__code__` to the untouched reference before each of the five real tests, in **9 instrumented full suites**: `run_real: True` every time | **Falsified** in the runs captured. The three places that patch it all restore it |
| 5 | "the shared backend inherits another module's cooldown" | same 9 instrumented runs: `cooling: 0.0`, `stalls: 0` before every real test, and `ever_ok: False` at the first (the singleton is untouched until then) | Not the cause in any captured run |
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
- Forced reproduction: 5 failed → 1 failed at the same injected condition.
- Full suite after the change: **2408 passed, 0 failed in 93.24 s**.
- Today's full-suite record, all of it: 116.47 s clean · 142.25 s clean · **168.72 s with the five
  failures** · 95.30 s clean · 84.36 · 86.62 · 84.61 · 93.09 · 84.18 · 83.09 · 82.91 · 80.03 s clean ·
  93.24 s clean after this phase. Thirteen runs, one failure, and that one at the longest duration.
- Tools 148 / 32 gated / 52 planner rules / 32 DB tables / 118 route objects unchanged.
- `suite_history.json`: 57 rows, Phase 65 appended to the file the additive way; no row removed.

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
