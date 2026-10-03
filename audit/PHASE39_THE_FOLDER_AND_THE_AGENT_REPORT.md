# Phase 39 — the folder you named, the agent you asked, and an interface that works

**Date:** 2026-10-03 · **Closes:** "it open vscode etc but in vscode its not opening folder,
not give cmds in claude code + make its application for laptop and make its ui better with frontend"
**Committed as:** `7d685ff` (editor), `59fdc11` (coding agent), and this report's commit (interface)

Four asks in one sentence, so the phase has three parts and each was measured before being
touched.

## 1. "it open vscode but not the folder" — two defects, not one

```
jarvis/tools/apps.py:35   run_command(["open", "-a", target])  -> cannot carry a folder at all
planner.py:1156           \b(open|launch|start|run)\s+(.+)     -> resolves the APP, drops the rest
planner.py:1003           (continue|resume|open).*project      -> claims any sentence holding
                                                                  "open" and "project"
`code` on PATH            -> MISSING; the app's own CLI is at
                            /Applications/Visual Studio Code.app/Contents/Resources/app/bin/code
```

So the editor opened exactly what macOS hands a bare `open -a` — whatever was focused last —
and a folder named `my-project` was *declined* rather than dropped. That third line is the one
the user would have hit constantly: their own directories are named like `agent-test-project`.

**Fixed:** `AppSpec` gained a `cli` map resolved against the installed bundle (the PATH shim is
an install step most people never take); `open_app` takes a `path`, resolving it as a directory
or against the workspaces already open, and refuses to invent one; the planner keeps the folder
in both word orders; and the project rule yields whenever a real application is named.

**Success is read from the editor, not from an exit code.** VS Code writes
`workspaceStorage/<hash>/workspace.json` the moment a window takes a directory. That is what the
tool polls, and when it does not appear the result is `unverified` with a reply saying so.

**Then the live check caught what 17 unit tests could not:** every one of them faked
`run_command`, and the real bundled CLI holds its inherited stdout open for as long as VS Code
runs — so the capturing caller waited 20 seconds for the editor to *quit* and reported
"Could not open Visual Studio Code: command timed out" on a launch that had worked.
`run_command` gained `detach` for launching applications; `open_app` uses it.

Live, through the running app over the LAN address:

```
"open vscode with /tmp/vantage-live2-j02z"
  -> "Opened vantage-live2-j02z in Visual Studio Code."
  -> VS Code's own record: {"folder": "file:///tmp/vantage-live2-j02z"}
```

## 2. "not give cmds in claude code" — the entry point exists now

Phase 35 left this unstarted because there was nothing to call. There is:
`~/.local/bin/claude`, 2.1.260, with `-p/--print`, `--output-format json`, `--add-dir`,
`-c/--continue`. Its own help says the workspace-trust dialog is skipped in non-interactive
mode — which is precisely why `run_coding_agent` is `EXTERNAL` and confirmation-gated rather
than a silent read.

`--dangerously-skip-permissions` is present on this machine and is never added; a test asserts
the argv rather than trusting intent, because a happy-path test would not notice if it were.
The tool reports the agent's own answer: `is_error` is a failure, unparsable output is
`unverified`, a non-zero exit carries the agent's words, and a missing CLI is a capability
statement, not a crash.

Live: *"Reply with exactly VANTAGE-LIVE-OK"* → `Verification.SUCCESS`, 1 turn, **$0.14033**.
From the phone side: *"ask claude to add a README to /tmp"* → planned `run_coding_agent`, held
for confirmation, nothing executed. Its preview read
`run_coding_agent on laptop_main | prompt=... | path=...`, which is open item 32-B arriving on a
6-inch screen, so it now says `ask the coding agent "add a README to the project" in <folder>`.

Three test premises were wrong rather than the product, and each is worth recording because the
temptation was to weaken the assertion: the gate **blocks** a protected directory (correct — so
that became its own test); `unverified()` deliberately returns `ok=True` with
`verification=UNKNOWN` (the convention, not a bug); and two tests registering a workspace named
`agent-test` made the name genuinely ambiguous, where refusing to guess is the right answer.

## 3. "make its ui better" — measured, then fixed, then looked at

Audited before restyling, because "better" without a baseline is a palette swap:

```
:hover 1   :focus 0   @media 0   prefers-color-scheme 0   prefers-reduced-motion 0   aria- 0
```

No keyboard focus ring anywhere. Nothing adapted below desktop width. Light only, while the
phone client is dark. A finished command was silent to a screen reader. The devices panel
rendered *literally nothing* when no device was paired, and confirmations said "none".

**Changed:** every hard-coded colour promoted to a token, so the dark block restates the same
set — a dark mode that forgets a variable is invisible text, and a test compares the two token
sets rather than trusting symmetry. Added `:focus-visible`, hover/active feedback on the
controls, three breakpoints, a reduced-motion guard, `aria-live`/`role="status"` on the state
and reply, a labelled command input, and empty states that name the next action.

**Two mistakes I made here, both caught by checking rather than assuming:**
- An edit duplicated `async function loadDevices() {` — a syntax error that looked fine in a
  diff and would have left the whole dashboard dead. It is now guarded twice: the page's script
  is run through `node --check`, and a test rejects any redeclared function name.
- My first browser measurement reported dark mode "not applying" and 676px overflow. Both were
  true of the page being served — the app serves its **own staged copy**, not the working tree,
  so I was measuring the old build. Re-measured against a dev server, where the real remaining
  bug appeared: `1fr` grid tracks refuse to shrink below their widest content, so one long audit
  row pushed the page past a phone viewport. `minmax(0, 1fr)` and `overflow-wrap` fixed it.

**Verified in Chrome** (not asserted): 375×812 dark — `bg rgb(13,17,23)`, `scrollWidth ==
clientWidth == 375`, zero overflowing elements; 1440×900 light — three columns, no overflow,
**no console messages**; empty confirmations panel renders "Nothing waiting. Anything risky I am
asked to do appears here for you to approve or deny."

## 4. The laptop application

Already self-contained from Phase 36; this phase rebuilt it and relaunched it. One operational
hazard found the hard way twice: `build_app.py` deletes the bundle, so rebuilding while the app
is running kills the user's instance — and my `pkill -f run.py` then caught the launcher
mid-start, leaving a window with a dead server. The recovery is a clean quit, rebuild, relaunch,
which is what the final numbers below were measured against.

## 5. Regressions and re-measured numbers

```
new tests        20 (editor) + 16 (agent) + 14 (interface) + 1 (README route count) = 51
full suite       1775 passed · 0 failed · 0 xfailed · 0 skipped
                 (the run before this one: 1772 passed · 1 failed · 1 skipped — the failure was
                 this report's own count, and the skip was arXiv timing out, not a defect)
safety matrix    6 of 6
tools            129 (read 69 · low 32 · external 13 · high 12 · physical 3)
confirmation-gated  28 (was 27; the coding agent is the new one)
routes           108 handlers / 106 distinct paths
planner routes   42 (the README said 40; the guard now reads the live rule list)
test modules     64          reports 41
size             60,272 lines of versioned Python/HTML/JS (38,426 source · 21,846 tests)
```

The README row for `RulePlanner` said "40 deterministic routes" and had said it since before
Phase 35 added `_agent_status`. Nothing noticed, because no command checked it. The new guard
compares the printed number with `len(core.planner._rules)`, and was confirmed to go red at 40
before being allowed to say 42.

## 6. Carried forward

* **39-A** `detach` returns as soon as the process starts, so a launch that dies immediately
  after exec is still reported by the observation loop, not the exit code — but an editor that
  records nothing within 5 s is reported as unverified even when it did open. The observation
  window is a guess about a third-party app's timing.
* **39-B** only VS Code and Cursor have a storage path this tool can read. Opening a folder in
  any other app is unverifiable by design and will say so.
* **39-C** the coding agent's own permission mode is left at its default, so a first file edit
  prompts inside the CLI — which nobody sees in a non-interactive run. What that means in
  practice is untested beyond the one read-only prompt.
* **39-D** the dashboard's dark palette was chosen to match the phone client, not audited for
  WCAG contrast ratios. The token test proves completeness, not legibility.
* **39-E** the IP changed mid-phase (172.20.169.34 → 172.20.169.185) and the URL previously
  handed to the user went dead on its own — 38-B arriving within a day. The QR and the panel
  address absorb this; mDNS would remove it.
* Unchanged: 38-A…D, 37-A…E, 36-A…E, 35-A…C, 34-A…C, and the `-rs` CI change.
* **39-F** (raised by §7) the runner cannot be used as evidence at all until the account is
  settled: the jobs for `e1fcf7a` were never started. Nothing in this repository's CI can be
  claimed green, and nothing can be claimed red either, while that holds.

## 7. Git

Parent `59fdc11`. Committed: `jarvis/tools/apps.py`, `jarvis/tools/coding_agent.py`,
`jarvis/tools/base.py`, `jarvis/tools/__init__.py`, `jarvis/core/models.py`,
`jarvis/core/planner.py`, `jarvis/security/policy.py`, `jarvis/registry/apps.py`,
`dashboard/index.html`, the three Phase 39 test files, `tests/test_phase25_desktop_truth.py`
(the route-count guard), `README.md`, `benchmarks/*`, `assets/*.svg`, this report.

**CI verdict:** run `37096835264`, commit `e1fcf7a`, **the runner never started the work.**
Both `suite (3.11)` and `suite (3.12)` report `failure` with `runner_name` empty, **0 steps**,
and a lifetime of ten seconds (04:31:39 → 04:31:49Z). The check-run annotation is explicit:

> The job was not started because recent account payments have failed or your spending limit
> needs to be increased. Please check the 'Billing & plans' section in your settings

Re-running the workflow produced the same ten-second, zero-step, no-runner pair 314 s later, so
this is not a queue timeout or a transient capacity refusal — although the second annotation,
"jobs targeting macOS arm64 runners may experience longer queue times", is also present and is
not the operative one. The push of this addendum (`78c833a`) was refused the same way under
four minutes later (04:40:30 → 04:40:37Z, `suite (3.11)`, `suite (3.12)`, no runner, no steps),
which is the useful property of the condition: it is steady, so a red badge here carries no
information about the code either way. **Nothing in this report's numbers is therefore claimed to be
runner-verified for this commit.** The distinction matters here more than usual, because CI on
this project is load-bearing rather than decorative: it is the only place the suite runs on a
machine with no battery, no ffmpeg and no Spotlight index, and the bundle build step exists to
catch exactly the drift that killed Phase 37's guard.

What *was* executed is this machine, on the pushed commit with a clean tree:

```
git rev-parse HEAD   e1fcf7abf25cb40bfc18568c3bfb9695c2fb96e8
git status --short   (empty)
JARVIS_HOME=$(mktemp -d) .venv/bin/python -m pytest -q -rs
                     1775 passed, 1 warning in 50.82s          # 0 failed, 0 skipped
JARVIS_HOME=$(mktemp -d) .venv/bin/python benchmarks/safety_run.py   6 of 6
JARVIS_HOME=$(mktemp -d) .venv/bin/python benchmarks/acceptance_run.py 17 of 18 measured
.venv/bin/python assets/build_charts.py && … build_diagrams.py       7/7 SVG, 129 tools
```

The last commit a runner actually executed is `40c5c89` (Phase 38's addendum): both suites
**success** in 12m44s. Until the spending limit is raised or the payment cleared, every push
will report `failure` without having tested anything, and that verdict should be read as
*unmeasured*, never as *broken*.

