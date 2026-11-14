# 2026-10-18 — Dashboard 404 root cause + fix

User reported `/dashboard` 404 after `uv run run.py serve` (the terminal printed the
"Vantage core -> .../dashboard" banner, so it looked like it started, but the browser
404'd).

## Root cause (verified live)

The success banner printed BEFORE the port was bound. A **stale backend already bound to
:8765** (an earlier `run.py`, or the packaged `.app` launcher) made the new process print
the banner, then fail to bind and exit quietly. The browser hit the STALE process, which
404'd. `build_app.py` stages `dashboard/` correctly (STAGED_ITEMS line 105), so the
packaged app itself is fine — the 404 was the stale-port race, not a missing dashboard.

## Fix (run.py)

Added a loud, early `port_is_free` pre-check that exits 1 with
"REFUSED to start -- port N is already in use (pid X)" + the exact recovery command
(`lsof -ti :N | xargs kill`), placed BEFORE the success banner prints. A losing second
start can no longer masquerade as a working start.

## Verified live

- :8765 held -> second `uv run run.py serve` exits 1, prints the refusal, prints NO banner.
- :8765 free -> starts, serves /dashboard 200.

## Regression test

tests/test_phase57_the_machine_stays_reachable.py::
test_the_run_script_probes_the_port_before_printing_success
(asserts the port probe + refusal print precede the success banner). Phase57: 56 passed.

## Bundle integrity

My run.py edit tripped tests/test_phase95_release_integrity.py::
test_run_py_includes_recent_changes (the shipped bundle's run.py was older than the
fixed source — the test doing its job). Fixed by rebuilding the bundle:
`.venv/bin/python build_app.py` (codesign verify: ok). Integrity test now passes.

## Final state

- Full suite green: **2726 passed, 0 failed, 40 skipped**.
- A clean backend is serving :8765; /dashboard returns 200.
