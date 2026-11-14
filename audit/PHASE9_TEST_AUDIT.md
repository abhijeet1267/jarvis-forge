# PHASE 9 — PHASE 2: Test Truth

Environment: `/Users/abhijeetmiskin/AppData/jarv/jarvis-forge`, project venv
`.venv/bin/python` = CPython 3.11.16, pytest 8.4.2, `asyncio_mode = auto`,
`testpaths = ["tests"]`. All runs used `-p no:cacheprovider` so the audit wrote no
`.pytest_cache` state.

## 2.1 Headline: the documented result is not the actual result

```console
$ .venv/bin/python -m pytest -q -p no:cacheprovider
126 failed, 875 passed, 1 skipped, 1 warning in 22.07s
```

|           | Claimed in brief / STATUS.md | Measured | Delta    |
| --------- | ---------------------------- | -------- | -------- |
| passing   | 975                          | **875**  | **−100** |
| skipped   | 1                            | 1        | 0        |
| failing   | 0 (implied)                  | **126**  | **+126** |
| collected | 976                          | **1002** | +26      |

The suite does not pass. The claim "975 passing + 1 skipped" is **FALSE against the
current tree**. Per the brief, failures were classified before any fix was attempted.

## 2.2 Failure classification by error type

```console
$ grep -oE "^E +[A-Za-z]+Error" pytest_output | sort | uniq -c | sort -rn
 105 E   NameError
   3 E   AssertionError
```

All 105 `NameError`s carry one identical message:

```console
$ grep -oE "NameError: name '[A-Za-z_]+'" pytest_output | sort | uniq -c
 105 NameError: name 'Verification'
```

The remaining 21 are `assert` failures whose assertion text shows the pipeline returning
a failure/unverified `ActionResult` where a success was expected — i.e. the same root
cause observed one layer up, through the orchestrator instead of through the traceback.

## 2.3 Root cause — one missing name, `jarvis/tools/base.py:16`

`jarvis/tools/base.py` imports:

```python
# line 16
from ..core.models import ActionResult
```

but references `Verification` at runtime on lines **103, 110 and 117**
(line 87 is only an annotation, harmless because of `from __future__ import annotations`).
`Verification` is defined and exported at `jarvis/core/models.py:210`
(`class Verification(str, Enum)`) — it is simply never imported into this module.

Proven by direct execution, not inference:

```console
$ .venv/bin/python -c "
import jarvis.tools.base as b
print('Verification in module namespace?', hasattr(b,'Verification'))
try: b.ok('demo.tool','did the thing')
except Exception as e: print('ok() RAISED:', type(e).__name__, e)"

Verification in module namespace? False
ok() RAISED: NameError name 'Verification' is not defined
unverified() RAISED: NameError name 'Verification' is not defined
fail() ok? False
```

Why this single name sinks 126 tests: `ok()` and `unverified()` in `jarvis/tools/base.py`
are the canonical `ActionResult` constructors for **every** tool in the catalogue. Line 103
is executed unconditionally on the normal path

```python
if verification is None:
    verification = Verification.SUCCESS if verified is not False else Verification.UNKNOWN
```

so **any** tool call that reaches a success return raises. `fail()` does not touch the
missing name, which is why the suite is 875-green rather than catastrophically red:
negative-path tests (refusals, offline reporting, validation errors, permission denials)
still exercise working code.

The irony is load-bearing for the audit's conclusions: this is the exact function whose
docstring was written to stop tools from falsely claiming success —

> `verified` used to be hardcoded `True` and placed _after_ `**output`, so a tool reporting
> `verified=dst.exists()` wrote into the output dict and the field silently claimed success
> anyway — which is how an offline robot command with no bridge ended up audited as a success.
> — `jarvis/tools/base.py:90-99`

The honesty fix that closed the offline-robot false-success hole is itself the thing that
crashed. It fails **closed** (crash → no claim), so this is a correctness/availability
defect, not a safety bypass. That distinction is why it is filed P0-correctness while
still permitting the audit to proceed rather than halt.

## 2.4 Causality proved by experiment, not argument

Step 1 — runtime injection of the missing name, via an audit-only pytest plugin outside
the repo (`/tmp/p9fix/p9fix.py`), injecting in a `pytest_configure` hook so
`tests/conftest.py` had already isolated `JARVIS_HOME` and `JARVIS_API_TOKEN`:

```console
126 failed, 875 passed, 1 skipped        (control: plugin loaded, no injection)
 59 failed, 942 passed, 1 skipped        (treatment: name injected)
```

122 of the 126 failures disappear when, and only when, that one name exists in the
module namespace. Set difference of failing node IDs: 122 fixed, 4 still failing.

Step 2 — the decisive clean-room measurement. The in-place injection run was contaminated
(see 2.6), so the experiment was repeated on a byte-exact shadow copy of the tree with
caches excluded, and the **source-level** one-line fix applied there only:

```console
$ rsync -a --exclude .venv --exclude __pycache__ --exclude .pytest_cache \
         --exclude dist --exclude app/build ./ /tmp/p9shadow/
$ sed -i '' '16s/import ActionResult/import ActionResult, Verification/' \
         /tmp/p9shadow/jarvis/tools/base.py     # in the SHADOW ONLY
$ cd /tmp/p9shadow && .../jarvis-forge/.venv/bin/python -m pytest -q -p no:cacheprovider
3 failed, 997 passed, 2 skipped, 1 warning in 14.14s
```

| Configuration                               | Result                              | Wall time  |
| ------------------------------------------- | ----------------------------------- | ---------- |
| real tree, as found                         | 126 failed, 875 passed, 1 skipped   | 22.1 s     |
| real tree + runtime injection               | 59 failed, 942 passed, 1 skipped    | 8.2 s      |
| **clean copy + source fix**                 | **3 failed, 997 passed, 2 skipped** | **14.1 s** |
| clean copy, unfixed (reproducibility check) | 126 failed, 874 passed, 2 skipped   | 21.7 s     |

**Attribution: 123 of the 126 failures are caused by the one missing import.**
The real tree was never modified; `/tmp/p9shadow` held all experiments.

## 2.5 The 3 residual failures — stale tests, not product regressions

```console
tests/test_home.py:228: in test_control_simulate_reports_simulated
    assert result.output["verified"] is True
E   KeyError: 'verified'
tests/test_home.py:241: in test_control_online_unverified_not_claimed_done
    assert result.output["verified"] is False
E   KeyError: 'verified'
tests/test_robotics.py:221: in test_motion_online_unverified_not_claimed_done
    assert result.output["verified"] is False
E   KeyError: 'verified'
```

Classification: **stale test** (brief category 3). `verified` moved from the `output`
dict to a first-class `ActionResult` field — the very refactor described in
`jarvis/tools/base.py:90-99` — and these three assertions were never repointed.
`result.verified` is the live accessor.

These three deserve more than a footnote. Their titles are
`*_reports_simulated`, `*_online_unverified_not_claimed_done`,
`*_online_unverified_not_claimed_done` — they are precisely the honesty assertions that a
simulated home/robot action is labelled simulated and that an unverified online motion is
not reported as done. Since they now abort on a `KeyError` before asserting anything,
**those three safety guarantees are currently unverified by the suite**, in the same
domain (robotics/home actuation) that produced the historical false-success incident
recorded in the docstring. Severity: P1 — a green suite after the import fix would still
leave three honesty properties silently untested.

## 2.6 Contamination found in the audit process itself (methodological finding)

The in-place injection run produced a _different_ failure set (59) than the clean-copy
run (3), and the two are almost disjoint — only 4 node IDs overlap. The cause was
investigated rather than waved away:

- Not test ordering: `test_api.py` alone fails 37/67 with the injected name, yet passes
  67/67 when the same run also calls `get_settings()`/`get_token_store()` during
  `pytest_runtest_setup`. A `--assert=plain` rerun still gave 59, ruling out pytest's
  assertion-rewrite cache as the mechanism.
- The 8-minute wall time of the clean-copy-with-injection run (481 s) against 14 s for the
  source-fixed run shows late injection leaves a large amount of code taking exception
  paths that the real import never would.

Conclusion: **runtime injection is not a faithful stand-in for the source fix**, because
`Verification` is resolved through `base.__globals__` at call time and the surrounding
module import graph differs. Every quantitative claim in this file that matters is taken
from the clean-copy source-fixed run, and that is why the 997/3/2 row is the one carried
into the final report. The 59-row is retained only as evidence of the 122-failure
attribution.

## 2.7 Secondary finding: bytecode caches from the pre-move location

306 `.pyc` files in the tree still name the dead path
`/Users/abhijeetmiskin/AppData/aisys/jarvis-forge`, under at least three distinct
interpreter/pytest combinations (`cpython-311-pytest-8.4.2`,
`cpython-313-pytest-9.1.1`, `cpython-313`). Tracebacks in the current 3.11 run still
_display_ the old path, e.g.:

```console
/Users/abhijeetmiskin/AppData/aisys/jarvis-forge/tests/test_api.py:42: AssertionError
```

while the file on disk is `/Users/abhijeetmiskin/AppData/jarv/jarvis-forge/tests/test_api.py`.
The current results are reproducible and the clean-copy run matches the real tree's
headline (126 failed / 875 passed, ±1 skip), so no conclusion in this audit rests on
suspect bytecode. But stale caches spanning three interpreters in an unversioned tree are a
reproducibility hazard: a future edit can be shadowed by a cache Python still considers
valid, and the failure would be reported against a path that does not exist. Severity P2.

## 2.8 Suite-shape observations

- Collection is stable at **1002 tests**; the "1 skipped" becomes "2 skipped" in a copy
  with `dist/` excluded, i.e. one skip is the desktop-bundle test standing down when the
  bundle is absent — a correctly-behaved environment skip.
- The single warning is a third-party deprecation, not project code:
  `StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated`.
- `pytest --collect-only -q` and `pytest -q` agree on 1002, so there are no
  collection errors hiding behind the failures.
- Test runtime is dominated by one file: the suite runs in ~14 s fixed / ~22 s broken,
  and the two privacy/voice suites account for most of the fixed-run cost (70 s when
  forced to run in isolation under one probe configuration — re-measured in Phase 12).

## 2.9 Verdict on "test truth"

| Question                           | Answer                                                                                                                      |
| ---------------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| Does the suite pass today?         | **No.** 126 failed / 875 passed / 1 skipped.                                                                                |
| Is that a real regression?         | **Yes — one real code defect** (missing import), worth 123 failures. Fails closed.                                          |
| Are there environment limitations? | 1 test skips when `dist/` is absent (correct behaviour). No dependency failures; all pinned deps import cleanly under 3.11. |
| Stale tests?                       | **Yes, 3**, all reading a field that moved (`output["verified"]` → `.verified`).                                            |
| Flaky?                             | Not in the product suite. The instability observed was in the _audit method_ (2.6), and was eliminated.                     |
| Documentation mismatch?            | **Yes** — "975 passing, 1 skipped" is not true of this tree.                                                                |
| Dependency problem?                | None detected. The tree needs 3.11; `python3` on PATH is 3.14 and caches exist from 3.13.                                   |
| Unknowns                           | None material to the count.                                                                                                 |

## 2.10 Proposed repair — NOT applied (Phase 15 compliance)

Smallest safe fix, one line, `jarvis/tools/base.py:16`:

```python
-from ..core.models import ActionResult
+from ..core.models import ActionResult, Verification
```

Expected effect, measured in 2.4: 126 failed → **3 failed**, 875 → **997 passed**.
No behavior change beyond restoring `ok()`/`unverified()`. Not implemented: the brief
forbids modifying production code in this phase, and the defect fails closed rather than
open, so no catastrophic-safety exception applies. Authorisation requested.

Follow-on, separate from the import fix: repoint the three stale assertions in
`tests/test_home.py:228`, `tests/test_home.py:241`, `tests/test_robotics.py:221` from
`result.output["verified"]` to `result.verified`, which restores the three currently
untested honesty guarantees.

## 2.11 Exact commands

```bash
cd /Users/abhijeetmiskin/AppData/jarv/jarvis-forge
.venv/bin/python --version                    # 3.11.16
.venv/bin/python -m pytest --version           # pytest 8.4.2
.venv/bin/python -m pytest -q -p no:cacheprovider
.venv/bin/python -m pytest --collect-only -q -p no:cacheprovider
.venv/bin/python -c "import jarvis.tools.base as b; print(hasattr(b,'Verification'))"

# causality, outside the repo
mkdir -p /tmp/p9fix && cat > /tmp/p9fix/p9fix.py <<'EOF'
def pytest_configure(config):
    import jarvis.tools.base as _b
    from jarvis.core.models import Verification as _V
    if not hasattr(_b, "Verification"):
        _b.Verification = _V
EOF
PYTHONPATH=/tmp/p9fix .venv/bin/python -m pytest -q -p no:cacheprovider -p p9fix
PYTHONPATH=/tmp/p9fix .venv/bin/python -m pytest -q -p no:cacheprovider -p p9control   # control

# decisive clean-room run
rsync -a --exclude .venv --exclude __pycache__ --exclude .pytest_cache \
      --exclude dist --exclude app/build ./ /tmp/p9shadow/
/usr/bin/sed -i '' '16s/from ..core.models import ActionResult/from ..core.models import ActionResult, Verification/' \
  /tmp/p9shadow/jarvis/tools/base.py
cd /tmp/p9shadow && /Users/abhijeetmiskin/AppData/jarv/jarvis-forge/.venv/bin/python \
  -m pytest -q -p no:cacheprovider
```
