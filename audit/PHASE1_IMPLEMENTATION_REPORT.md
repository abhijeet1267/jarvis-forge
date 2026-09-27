# JARVIS Forge — Phase 1 Implementation Report

**Status of this document:** partial. Phase 1B is complete and verified.
Phases 1C and 1D have **not been started**. Nothing in §C–§F below should be
read as claiming otherwise, and the acceptance gate in §L is therefore not met.

Created during Phase 1 of the security + truthful-execution closure.
Project root: `/Users/abhijeetmiskin/AppData/aisys/jarvis-forge`

---

## A. Baseline (measured before any Phase 1 edit)

```
collected : 976
passed    : 975
failed    : 0
skipped   : 1   (live arXiv test, network-gated)
duration  : 12.9 s
python    : 3.11.16  (./.venv/bin/python)
git       : not a repository ("fatal: not a git repository") — initialised
            deliberately NOT done, per Phase 1 rule 23
```

The instruction document quoted 968/967. The measured baseline differs and was
recorded as measured, per rule 24.

---

## B. Files modified

Phase 1B only:

| File | Change |
|---|---|
| `jarvis/security/paths.py` | component-wise protected-path matching; `iter_string_values()`; `_parts()` / `_contains()` |
| `jarvis/security/policy.py` | `_path_guard` now scans every string value recursively instead of three parameter names |
| `jarvis/core/planner.py` | `search_files` no longer injects `directory: "~"` when the user named no place |
| `jarvis/tools/files.py` | search results filtered through `protection_reason` (Spotlight path had no pruning, unlike the walk path) |
| `tests/test_paths.py` | look-alike and component-matching tests; walker test |
| `tests/test_policy.py` | 11-case protected-value matrix; safe-work contrast tests; three new fixture tools |
| `tests/test_planner.py` | one test's expectation updated to the new contract |
| `tests/test_pipeline.py` | end-to-end BLOCK-before-execution test and an everyday-work contrast test |

No other file was touched. Full suite after the change: **1001 passed, 1
skipped** (+26 tests).

---

## C. CR1 — false structured success

**Not addressed in 1B.** Scheduled as Phase 1C. The defect is unchanged:
`jarvis/tools/base.py` `ok()` still hardcodes `verified=True`, and
`agents/laptop.py` / `agents/laptop_center.py` still synthesise a
`verification_note` from the platform name. The hardware no-op still audits as
`result=success`.

---

## D. CR2 — authorization bypasses

**Not addressed in 1B.** Scheduled as Phase 1D. The direct-handler routes and
`CommitBody.authorized` are unchanged.

---

## E. CR9 — path security (CLOSED)

### Before

`_path_guard` read only `params["path"]`, `params["source"]`,
`params["destination"]`. Measured on the real engine, three sibling tools
refused the same directory and one did not:

```
list_directory  -> BLOCK    Library/LaunchAgents is a protected private location
read_file       -> BLOCK    Library/LaunchAgents is a protected private location
copy_file       -> BLOCK    Library/LaunchAgents is a protected private location
search_files    -> EXECUTE  low-risk action          <-- bypass by argument name
run_allowlisted_command -> CONFIRM (should be BLOCK) <-- bypass: path hidden in args
```

Root cause of the leak itself: `search_files` has two backends. The `os.walk`
fallback prunes dot-directories; **mdfind (Spotlight) does not**, so a
home-rooted search returned the name of a file inside `.ssh`. Proven against a
synthetic home under `/tmp` — no real private file was read, listed, or named:

```
GUARD_SSH True   GUARD_LAGENT True   GUARD_PLAIN False
DEFAULT_ROOT decision=EXECUTE
DEFAULT_ROOT leaked_protected=True
CONTROL unprotected_found=True
```

### After

Refusal follows the **value**, at any depth, in any container, and the
protected-path test is component-wise rather than substring-wise.

### One design decision worth flagging

`search_files` defaults its root to the home directory, and the planner used to
put `directory: "~"` into every search plan. A value-based guard would therefore
have blocked *every* search — the flagship feature. Making the guard tolerate
`~` would have been the weakening that rule 8 forbids, so the fix went elsewhere
instead: the planner now omits `directory` unless the user actually named a
place, and the **tool** filters its own results. A home-rooted read stays
available; a named protected location is refused; and the default root can no
longer surface protected entries.

### Live verification (isolated `JARVIS_HOME`, throwaway server, now stopped)

```
LIVE | A-blocked | BLOCK   | I cannot do that: Library/LaunchAgents is a protected private location.
LIVE | A-ssh     | BLOCK   | I cannot do that: .ssh is a protected private location.
LIVE | B-blocked | BLOCK   | I cannot do that: Library/LaunchAgents is a protected private location.
LIVE | B-ok      | CONFIRM | This needs your confirmation before I run it. Action: run_allowlisted_command
LIVE | C-search  | EXECUTE | I found 402 file(s) matching 'report'. Most recent: DiagnosticReports (1 min ago).
LIVE | C-list    | EXECUTE | /private/tmp contains 95 item(s).
```

A and B are the two attacks. `B-ok` proves the guard did not over-reach — an
ordinary external command still confirms rather than being refused. `C-*` prove
everyday work is intact.

### Test evidence

`tests/test_pipeline.py::test_protected_search_is_blocked_before_execution`
installs a handler that raises `AssertionError` if called, then asserts BLOCK —
so non-execution is proven mechanically, not inferred from a message.

The widened guard was also checked against false positives:
`~/Documents/LaunchAgents_backup.txt` and four other look-alikes are explicitly
asserted *not* protected, because substring matching on protected names would
refuse ordinary files and push users toward disabling the guard.

### Residual, stated plainly

The guard inspects string **values**. A path assembled from fragments across
separate parameters (e.g. a base in one key and `".ssh"` in another) is not
reconstructed, and non-string scalars are skipped. Relative paths resolve
against the server's working directory, which is the project root, so a bare
`.ssh` argument does not resolve into the real `~/.ssh`.

---

## F. CR10 — privacy deletion

**Not addressed in 1B.** Scheduled as Phase 1D.
`jarvis/security/privacy.py` still converts a failed table delete into
`removed[table] = 0` and returns normally.

---

## G. Verification model

Not implemented yet (Phase 1C). No SUCCESS / FAILED / UNKNOWN tri-state exists
today.

---

## H. Tests

| Command | Result |
|---|---|
| `./.venv/bin/python -m pytest tests/test_policy.py tests/test_paths.py -q` | 52 passed |
| `./.venv/bin/python -m pytest tests/test_pipeline.py -q` | 30 passed |
| `./.venv/bin/python -m pytest -q` | **1001 passed, 1 skipped** |

One pre-existing test was edited (`test_search_treats_only_real_paths_as_directories`)
because the contract it pinned changed deliberately; its intent — a scope phrase
is not a path — is still asserted, now by the absence of a `directory`
parameter. No test was deleted and no assertion was loosened to accommodate the
change.

## I. Live tests

See §E. Server was started with `JARVIS_HOME=/tmp/jp1 JARVIS_PORT=8941` and
stopped afterwards; the scratch home was removed. No real hardware was
addressed and no credential appeared in any output.

## J. Remaining risks

| Risk | Status after this session |
|---|---|
| CR1 false structured success | **open** — Phase 1C |
| CR2 authorization bypasses | **open** — Phase 1D |
| CR3 token readable by any local process | **open** — deferred, needs a design decision |
| CR4 audit chain launderable by blanking a hash | **open** — deferred; unchanged by 1B |
| CR6 confirmation not session-bound | **open** — explicitly out of Phase 1 |
| CR7 scheduler writes no audit records | **open** — unchanged |
| CR8 Vision OCR intermittent | **open** — bounded, unchanged |
| CR9 protected-path guard bypass | **closed** |
| CR10 privacy delete hides failure | **open** — Phase 1D |
| CR11 no version control | **open** — no rollback exists for these edits |

## K. Documentation

`STATUS.md`, `README.md` and `PROJECT_DETAILS.md` were **not** updated, per rule
30 ("only after code and tests are proven correct"). They still describe the
pipeline as including verification, which remains true once 1C lands and is
false today — recorded here rather than papered over.

## L. Final phase decision

```
PHASE 1 BLOCKED
```

Not "conditional pass". Phase 1B is complete and verified, but the acceptance
gate in §34 requires CR1, CR2 and CR10 to be closed, and none of them is.
`BLOCKED` here means *incomplete*, not *stuck*: no stop condition from §35 was
hit, and 1C/1D can proceed.
