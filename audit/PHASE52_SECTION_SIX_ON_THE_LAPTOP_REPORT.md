# Phase 52 — the §6 sentences about the laptop

Opened 2026-10-04 as the continuation of Phase 51's remaining list (51-F): the build prompt's
§6 utterances that still reached nothing. Measured first, all 37 of them, against the real
registry:

**24 of 37 worked. 13 did not.** They split three ways, and the split decided the work, because
a route is not a capability and neither is a regex.

| class | utterances | what was actually wrong |
|---|---|---|
| the application registry did not know the name | `open Firefox`, `open Contacts`, `open Messages`, `open System Settings` | `open_application` works; `DEFAULT_APPS` had 9 entries and these were not among them |
| the answer existed and the sentence had no route | `what date is it?`, `what is my hostname?` | `get_time` already replies "It is 11:53 on Sunday, 04 October 2026"; `get_machine_snapshot` already returns a `hostname` field |
| no capability at all | `open Desktop/Downloads/Documents/Pictures`, `show the current application` | needed two tools |

Deliberately left for the next phase: the five self-observation sentences (`show pending
confirmations`, `show Vantage status`, `show recent actions`, `stop the current task`,
`search Google for X`). Mixing a new command surface into a routing phase makes it impossible
to say which change broke what.

## 1. What was built

| file | change |
|---|---|
| `jarvis/registry/apps.py` | Firefox, Contacts, Messages, System Settings registered (9 → 15 applications; the other two came in Phase 51) |
| `jarvis/core/planner.py` | `_time` answers "what date is it"; `_machine_snapshot` answers the hostname phrasings; two new rules, `_open_standard_folder` and `_frontmost_app` (48 → 50 routes) |
| `jarvis/tools/apps.py` | `open_user_folder` and `get_frontmost_application`, plus `bundle_installed` and a bounded AppleScript deadline |
| `jarvis/tools/agents.py` | `/System/Applications` added to `BUNDLE_ROOTS` |
| `tests/test_phase52_section_six_on_the_laptop.py` | 52 tests |

**The folder allowlist is the design.** `open_user_folder` takes a *name* from a five-entry
dictionary and builds the path from the home directory; it never takes a path. So
`open ../../.ssh`, `open /etc`, `~/Library/Mail` and `passwords` are refusals that name what
is allowed, and a refused folder launches nothing -- asserted, not assumed. It is also not an
alias of `open_project_folder`: one reveals a directory in Finder, the other hands a workspace
to an editor, which is the distinction Phase 49a refused to blur.

## 2. Three defects the live drive found and the 2,013-test suite had not

The suite was green before this section. All three were found by running the sentences.

### 2a — a sentence that took 20.25 seconds

```
open Downloads → 20250 ms → "Finder did not answer (command timed out after 20.0s)"
```

The verification query was `osascript -e 'tell application "Finder" to get name of every
window'` with no timeout argument, so it inherited `run_command`'s 20 s default -- and it is
called in a retry loop of four, which could have reached **80 seconds for one sentence**.
Phase 34's rule is one sentence, one deadline; this phase broke it while adding a capability.
The query now carries a 3 s budget, a timeout is treated as an answer rather than a reason to
wait again, and the same budget applies to the frontmost-app query.

```
open Downloads → 3219 ms → "Downloads was told to open, but Finder did not answer in time,
                            which usually means the Automation permission prompt is still
                            waiting for you -- look at the screen before believing me."
```

Cross-checked from a second caller: the same AppleScript run straight from this terminal also
died with `AppleEvent timed out. (-1712)`. Finder is not answering on this machine, so
`UNKNOWN` is the accurate answer here, not a symptom of who asked -- which is the opposite of
Phase 50's finding, and saying so matters because the tempting story is "the app lacks
Automation permission" and the measurement does not support it.

### 2b — "not installed" was invisible because the launch is detached

`open -a Firefox` exits **1** with `Unable to find application named 'Firefox'`. But launches
are detached on purpose (a GUI app holds its pipe open for as long as it runs), so
`run_command` returns 0 without ever seeing that exit code. The only evidence left was the
process observation from Phase 51, which correctly refused to claim success -- and then said
nothing more useful than "could not confirm". Firefox is genuinely absent here, and the user
deserves that fact:

```
open Firefox → "Firefox is not installed on this machine, so there is nothing for me to open.
                The name came from my own application list, which means that list is out of
                date."  → FAILED
```

The two cases stay distinct and both are tested: **not installed** is a failure with a reason;
**installed but unseen** remains `UNKNOWN`. Neither is allowed to become the other.

### 2c — the installed check denied that Apple's own apps exist

```
is Mail installed → "Mail is not installed on this laptop, so there is nothing for me to
                     report about it."        (ok=True, found=False)
```

`BUNDLE_ROOTS` searched `/Applications`, `~/Applications` and `~/.qoder/bin`. Mail, Contacts
and Calendar live in `/System/Applications`. So for half of §6's application list, a tool
answered a factual question with a confident wrong fact -- and reported it as a success,
because "absent" is a complete answer rather than a failure of the check. After the fix, live:

```
is Mail installed → "Mail is installed (version 16.0) and is not running right now."
```

This one predates the phase (the probe is from Phase 35) and was only visible because §6
happens to name Apple applications.

## 3. Live, over HTTP, on an isolated instance

Never against the user's running app. `JARVIS_HOME=$(mktemp -d)`, port 8797/8798. Contacts was
launched by the first drive and quit after; the Downloads window Finder was asked about is
unconfirmable either way, and nothing was written to any folder.

```
what date is it?            → "It is 11:53 on Sunday, 04 October 2026."          6 ms
what is my hostname?        → "Laptop condition: Abhijeets-MacBook-Air-2.local…" 49 ms
show the current application→ "Qoder is the application in front right now."    794 ms
open Contacts               → "Contacts is open on your macos machine."         275 ms  verified
open Downloads (after)      → UNKNOWN, 3219 ms
open Firefox (after)        → FAILED "not installed", 1422 ms
is Mail installed (after)   → "Mail is installed (version 16.0)…"               112 ms
```

`show the current application` answered through System Events, which needs Accessibility --
measured by the Phase 50 card as `REQUIRES_MANUAL_SYSTEM_SETTINGS` for the bundled app and
`GRANTED` for a terminal. So this sentence works from a terminal-launched server today and will
answer with the named permission from the delivered app until Accessibility is granted. That is
the §3 behaviour the build prompt asks for, and it is recorded as an open item rather than
smoothed over.

## 4. Numbers, re-measured

| | before | after | how |
|---|---|---|---|
| Test suite | 2013 passed · 0 skipped | **2065 passed · 0 failed · 0 skipped** (65.1 s) | `JARVIS_HOME=$(mktemp -d) .venv/bin/python -m pytest -q` |
| Phase 52 tests | 0 | **52** | `tests/test_phase52_section_six_on_the_laptop.py` |
| Test modules | 78 | **79** | `ls tests/test_*.py \| wc -l` |
| Phase reports | 53 | **54** (this file) | `ls audit/*.md \| wc -l` |
| Lines | 65,655 — 40,244 source · 25,411 tests | **66,334 — 40,506 source · 25,828 tests** | `wc -l` |
| Tools | 134 | **136** (two new capabilities, both gated) | `len(core.tools.names())` |
| RulePlanner routes | 48 | **50** | `len(core.planner._rules)` |
| Registered applications | 9 | **15** | `len(AppRegistry().all())` |
| §6 coverage | 24 of 37 | **30 of 37** | measured list, §1 |

Three count-guards failed on purpose and were handled honestly. The README's route count was
stale (48 → 50) and was corrected. Two Phase 49 guards had frozen the *tool total* at 134 to
prove that an alias does not create a capability; a frozen total cannot survive legitimate
growth, so they now assert the invariant they were named for -- registered specs equal distinct
canonical names, `/tools` publishes exactly the registry, and no alias appears as its own row.
That is a stronger statement than `== 134`, not a weaker one.

## 5. Open items

* **52-A** `open Downloads` cannot be confirmed on this machine at all: Finder answers no
  AppleScript, from any caller. The folder probably opened; the tool says it does not know.
* **52-B** `get_frontmost_application` depends on Accessibility, which the delivered app does
  not have (Phase 50's card). From the bundle it will answer with the permission name, not an
  application.
* **52-C** `open_user_folder` covers five named folders and nothing else, by design. "open my
  screenshots" is not a route, and the refusal does not offer a path.
* **52-D** The application list is still a static registry. `bundle_installed` can now say "my
  list is out of date", but nothing reconciles the list against what is installed at startup --
  so Firefox stays registered on a machine without it, which is the honest-but-noisy outcome.
* **52-E** `System Settings` launches under that name only; on macOS 12 and earlier the bundle
  is `System Preferences.app`, and the alias resolves to a launch name that will not exist.
* **52-F** The five self-observation sentences remain: pending confirmations, Vantage status,
  recent actions, stop the current task, `search Google for X`. That is the next phase.

## 6. Where this leaves the build prompt

§6 is now 30 of 37. The seven that remain are all Vantage talking about itself, which is the
part §14, §16 and §30 of the same document depend on: a phone cannot show pending approvals,
task state or recent actions until the laptop answers those questions about itself.
