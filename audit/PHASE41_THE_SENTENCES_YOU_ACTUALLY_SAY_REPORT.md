# Phase 41 — the sentences a person actually says

## 1. What was asked

> its not an automus like i want control on system, want ai like iron man have like i say
> hey vantage open github site its open auto login, give summary of that data, if i say
> check storege, serch my .... file and open it etc

and, from the app itself, the sentence that triggered the look:

> Project continuation needs the design and file agents, which are not in this build yet.

Six asks were named. Each was run through the planner **before any code was written**, so
the phase would fix what is actually broken rather than what the feature list said:

| Sentence | Measured before | What that meant |
|---|---|---|
| `open my project` | **declined** with the note above | `design`, `laptop` and `coding` agents are all registered, and Phase 39 already opens a folder in the editor and proves it. The excuse was false. |
| `check storage` / `how much storage is left` | `not_understood` | no tool existed for disk space at all |
| `open github.com` | routed to **`read_file`** with `path=<home>/github.com` | a hostname was turned into a filename in the user's home directory — a wrong action, not a missing one |
| `open the github site in chrome` | `open_app(chrome)` | the app launched, the site was thrown away |
| `find my resume and open it` | `not_understood` | `search_files` and an open path both existed; nothing joined them |
| `hey vantage …` | `not_understood` | the wake list is `["jarvis", "hey jarvis", "ok jarvis"]` — the product was renamed in Phase 37 and the words it answers to were not (37-B, now user-visible) |

## 2. What was built, and what it refuses to do

**`open_project_folder`** — the named project, or the one open project, into the editor. With
nothing open it says so and names the way in; with two open it asks which, by name. The
verification is the one Phase 39 built: the editor's own storage record, not an exit code.

**`check_storage`** — `df -k`, read back through one regex, reported in the binary unit `df`
counts in, because "41.7 free" without a unit is a coin flip against what the Finder shows.
A phone's storage is refused rather than answered with the laptop's numbers.

**`open_url`** — a domain becomes a page; a bare name becomes a page only if it is in a
short, checkable list. **A name that is not on that list is refused, and says why**: guessing
`flurbledink` into `https://flurbledink.com` would point the user's logged-in browser session
at whoever registered that domain, which is a worse outcome than not understanding.

**`find_and_open`** — `search_files`, then the top hit handed to LaunchServices. The finding
is verified; the opening is **not**, and says so. `open` exiting 0 proves the request was
accepted, nothing here proves a window took the file.

## 3. Two things found by running it rather than reading it

**The Apple event that hangs.** The first live run of `open_url` did not return. Asking
Google Chrome `get URL of active tab of front window` blocked past a wall-clock minute — a
browser without a front window, or a macOS that has not authorised the sender, does not
answer and does not fail. That is the same class of defect Phase 34 closed for network reads,
so the query now carries its own 4 s deadline and the answer names the reason it could not
confirm. Proven again live: `open github.com` returned in **4.12 s**, `ok=True`,
`verification=unknown`, reply *"Asked Google Chrome to open https://github.com; I could not
confirm the page — it did not answer in time."* The bounded-test is non-vacuous: removing the
`timeout=` argument makes it fail with `unbounded wait: timeout=None`.

**A leaked workspace, again.** `test_the_one_open_project...` was green alone and red in a
full run. The cause was in the Phase 33 file: its `open_workspace` fixture registered
directories in a process-wide registry and never closed them, so by the time this phase's
tool asked "how many projects are open?" four test workspaces were the answer. Fixed at the
source (the fixture now closes what it opened) and the new tests additionally isolate the
registry for their own duration, because a premise this tool depends on cannot be left to
whatever the session happened to leave behind.

## 4. Live proof

Server on `:8799`, `JARVIS_HOME` a scratch directory, requests over HTTP:

```
open my project        → "Opened ironman-41 in Visual Studio Code."
                         verification=success, and VS Code's own record says
                         {"folder": "file:///private/tmp/ironman-41"}
check storage          → "Storage: 40.8 GiB free of 228.3 GiB on / (23% used,
                         /dev/disk3s3s1)"   0.02 s   cross-checked against `df -k /`
how much storage is left → same answer, same tool
is my disk full          → same answer
check my phone storage → "I cannot read that device's storage: no phone is paired…"
open github.com        → 4.12 s, opened, UNVERIFIED with the reason (see §3)
open the wikipedia site in chrome → https://www.wikipedia.org, browser named
open the flurbledink site → not understood; no domain was invented
find my zshrc and open it → found .zshrc.bak, and reported the system's own refusal:
                            "No application knows how to open URL file:///…/.zshrc.bak"
```

That last one is worth keeping in the report. The sentence worked, the file was found, and the
answer was a failure carrying LaunchServices' actual complaint — not a success, and not a
shrug.

## 5. Regressions and numbers

```
new tests        14 (project + storage) + 16 (site + find-and-open) = 30
full suite       1815 passed · 0 failed · 0 xfailed · 1 skipped
                 (the skip is arXiv timing out in this run; the same test passed live twice earlier
                 in the phase, so the skip is reported rather than smoothed over)
safety matrix    6 of 6
tools            133 (read 70 · low 35 · external 13 · high 12 · physical 3)
confirmation-gated  28 (unchanged -- nothing added here needs a gate; all four are LOCAL/READ)
planner routes   45 (was 42; the README said 43 mid-phase and the guard caught it again)
API routes       108 handlers / 106 distinct paths
test modules     68          reports 43
size             61,551 lines of versioned Python/HTML/JS (38,867 source · 22,684 tests)
```

The route-count guard has now caught a stale published number in three consecutive phases.
The number being wrong is small; the pattern is the thing — a printed count has no way to stay
true unless something re-measures it, and no external check exists while the runner cannot
start (39-F, 40-E).

## 6. Carried forward

* **41-A — "hey Vantage" still does not wake it.** The wake list is still `jarvis`/`hey
  jarvis`/`ok jarvis` (37-B). This phase fixed what a sentence *means*; it did not change what
  the machine *listens for*, because that list also carries the voice pipeline's behaviour and
  deserves its own measurement. The typed path is equally affected: `hey vantage open github`
  is not understood.
* **41-B — "give summary of that data" is not answered.** A page's content is not reachable
  from here without either the browser's cooperation (which §3 shows is not granted) or a
  fetch, and a summary with the model off would be an extract of whatever HTML happened to
  load — for a logged-in site, that is not the same data the user is looking at. Recorded
  rather than faked. `summarize_email`'s extractive rule is the pattern to follow if this is
  built.
* **41-C — "auto login" is the user's own session, not a credential store.** Opening a site in
  their browser reuses whatever the browser already remembers; nothing here stores or types a
  password. That is deliberate: the alternative turns a personal assistant into a
  credential-bearing app, which is a different security conversation.
* **41-D — the confirmation channel is per-browser and unauthorised right now**, so `open_url`
  returns UNKNOWN on this machine until Automation permission is granted (System Settings →
  Privacy & Security → Automation). Every site open costs its 4 s deadline in the meantime.
* **41-E — `open -a "Google Chrome" <url>` reached the Chrome that was already running**, which
  on this machine is an automation-profile instance started by a debugging tool, not the
  user's own window. Which profile a URL lands in is not something this tool chooses today.
* **41-F — `find_and_open` has no observation channel** for "a window took this file", so it
  can never be verified as written. `lsappinfo` or an accessibility query could be one; that
  would need the same permission conversation as 41-D.

## 7. Git

Committed on `main`, not pushed (40-E). Files: `jarvis/tools/storage.py`,
`jarvis/tools/browser.py`, `jarvis/tools/apps.py`, `jarvis/tools/files.py`,
`jarvis/tools/__init__.py`, `jarvis/core/planner.py`, `jarvis/registry/apps.py`,
`tests/test_phase41_the_sentences_you_actually_say.py`,
`tests/test_phase41c_open_the_site.py`,
`tests/test_phase33_questions_that_carry_their_own_answer.py` (the leak), `README.md`.
