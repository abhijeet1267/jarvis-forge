# Phase 51 — "open Mail" and "open Calendar" reach the applications

Opened 2026-10-04 on the instruction *fix open Mail and Calendar routes* -- the two wrong
routes Phase 49a recorded as **49a-D**, out of the 14 utterances the build prompt's §6 is short.
The other 12 are untouched here on purpose: two of these were not gaps but **routes that
answered a different question than the one asked**, and that class is worth closing properly
before widening.

## 1. Measured first

Against the real registry (134 tools), before any edit:

| sentence | what the planner did | what exists |
|---|---|---|
| `open Mail` | `reply_kind=declined`, "I can search your mail, read a message, summarise recent ones…" | `open_application`, Mail.app installed |
| `open the mail app` | same decline | same |
| `open Calendar` | `open_url https://calendar.google.com` | Calendar.app installed |
| `open the calendar` | `open_url https://calendar.google.com` | same |
| `open Google Calendar` | `open_url https://www.google.com` | the web calendar exists |
| `open Mail.app` | `open_url https://Mail.app` | Mail.app installed |

Each defect is a different mechanism, which is why one fix would not have covered them:

* **Mail** -- the mail agent's help rule (`_email`) sits at position 40 of the rule chain and
  the application rule at 28, so the app rule ran first and simply did not know Mail. The
  registry had nine applications; Mail and Calendar were not among them. The sentence was
  declined by a rule quoting capabilities it had just refused to use.
* **Calendar** -- a name collision. `calendar` is in `NAMED_SITES` (the web product) *and* is
  the macOS application, and the URL rule runs before the application rule.
* **Google Calendar** -- `NAMED_SITES` was iterated in dict order, so `google` matched before
  `google calendar` could. The brand-qualified name lost the half that identified which Google
  thing was asked for, and returned the search home page.
* **Mail.app** -- the domain-shaped-token test has no idea about `.app`. `Mail.app` looks like a
  domain, and the rule built `https://Mail.app`. `jarvis/tools/browser.py` opens with the
  sentence "Nothing is guessed into a domain… inventing `flurbledink.com` sends the user's
  browser, and their session cookies, to whoever registered that name." The rule broke its own
  module's first line.

## 2. The rules chosen

1. **A registered application beats a site of the same word**, unless the sentence asks for the
   web (`web`, `website`, `site`, `online`, `browser`, a named browser, or a brand qualifier like
   `google`). `open Calendar` → app; `open the calendar site`, `open calendar in the browser`,
   `open Google Calendar` → page.
2. **The lookup is exact, not fuzzy.** `AppRegistry().resolve("gmail")` returns Mail, because
   "mail" is a substring of "gmail" -- so a fuzzy precedence check would have taken gmail away
   from its own site. `AppRegistry.get()` is the exact index, and that is what the rule uses.
3. **Longest known site name wins.** `open Google Calendar` → `https://calendar.google.com`,
   and `open stack overflow` keeps working for the same reason.
4. **`.app` is a bundle.** When the token's suffix is `app`, the URL rule steps aside and the
   application rule resolves the part before it.
5. **A launch is only "open" once the process has been seen.** This one was not in the ask; it
   arrived with the proof, and is §9 of the build prompt ("handler success != world success").

## 3. What the machine caught after the fix

The first version of the launch check grepped the process table for `/Calendar.app/`. It passed
its tests, passed live, and was **wrong** -- found by looking at `ps` on the real machine after
the app had been closed:

```
/System/Applications/Calendar.app/Contents/PlugIns/CalendarWidgetExtension.appex/Contents/MacOS/CalendarWidgetExtension
```

Calendar.app was not open. A widget process that lives *inside* the bundle was enough to satisfy
`"/calendar.app/" in out`, so the tool would have said "Calendar is open" on the strength of a
helper the user never launched. The probe now matches the main executable path,
`/{target}.app/Contents/MacOS/`, and `test_a_helper_process_is_not_the_application` holds the
exact `ps` line above as its fixture.

Worth stating plainly: this defect was **introduced by this phase**, its test was green, and the
first live run agreed with it. The suite passing and the machine agreeing are not the same
observation.

## 4. What was built

| file | change |
|---|---|
| `jarvis/registry/apps.py` | `Mail` and `Calendar` registered (macOS, no `cli` -- neither takes a folder) |
| `jarvis/tools/browser.py` | `google calendar` added to `NAMED_SITES`, with why the bare word changed owner |
| `jarvis/core/planner.py` | `_open_url`: `.app` steps aside; site names matched longest-first; a registered application outranks a site unless the sentence says web |
| `jarvis/tools/apps.py` | `_process_observed` + a bounded 6 × 0.2 s poll; the no-folder launch returns `ok` only when the main executable is seen, else `unverified` |
| `dashboard/phone.html`, `dashboard/index.html` | the button labelled "Open a site" now types `open the {site} site` -- after this change a bare "calendar" would have opened an application from a button that promises a web page |
| `tests/test_phase51_two_routes_answered_wrong.py` | 52 tests |

## 5. Live, over HTTP, on an isolated instance

Never against the user's running app. `JARVIS_HOME=$(mktemp -d)`, port 8799, and the two
applications really launched. Neither Mail nor Calendar was running before, and both were closed
after.

```
open Mail        -> "Mail is open on your macos machine."      EXECUTE  52 ms  verified=True
open Calendar    -> "Calendar is open on your macos machine."  EXECUTE  33 ms  verified=True
open Mail.app    -> "Mail is open on your macos machine."      EXECUTE  31 ms  verified=True
open Calendar    -> 279 ms  (after the probe was tightened)
open Mail        -> 402 ms  (after the probe was tightened)
```

Independent check, not the tool's own word:

```
/System/Applications/Mail.app/Contents/MacOS/Mail
/System/Applications/Calendar.app/Contents/MacOS/Calendar
```

Audit rows in that instance: five `open_application` / `success`, and the two honest failures
described next.

```
open Flurbly.app -> "That did not work: File not found: <home>/Flurbly.app"   read_file
close Mail       -> "Could not quit Mail: command timed out after 20.0s"      close_app FAILURE
close Calendar   -> "Quit Calendar."                                           close_app
```

No invented domain, and no claimed success. The Mail quit genuinely failed -- first-run Mail
raises its own account dialog and does not answer the AppleScript; it was closed by hand and the
failure is recorded as **51-C**, not smoothed over.

## 6. Browser

The delivered bundle was rebuilt, re-signed (`seal: valid`), redeployed to
`~/Downloads/Vantage/`, relaunched, and confirmed to be serving the new page
(`/phone` contains `say: "open the {site} site"` and the new slot prompt) and the new source
(`Contents/MacOS/` probe present in the staged tree).

At 375 × 812 the phone client renders with all six quick actions, no overflow, and **0 console
errors**. The button was **not** clicked: the session's permission classifier blocked the tap as
outside the instruction's scope, so this phase has no live click-through. The claim that the
button's sentence routes to `open_url` is made by
`test_a_button_promising_a_site_says_so_to_the_router`, which reads the shipped HTML and runs the
typed sentence through the real planner for `calendar`, `google` and `github.com`.

## 7. Numbers, re-measured

| | before | after | how |
|---|---|---|---|
| Test suite | 1961 passed · 0 skipped | **2013 passed · 0 failed · 0 skipped** (61.5 s) | `JARVIS_HOME=$(mktemp -d) .venv/bin/python -m pytest -q` |
| Phase 51 tests | 0 | **52** | `tests/test_phase51_two_routes_answered_wrong.py` |
| Test modules | 77 | **78** | `ls tests/test_*.py \| wc -l` |
| Phase reports | 52 | **53** (this file) | `ls audit/*.md \| wc -l` |
| Lines | 64,614 — 39,572 source · 25,042 tests | **65,655 — 40,244 source · 25,411 tests** | `wc -l` |
| Registered applications | 9 | **11** | `len(AppRegistry().all())` |
| Tools | 134 | **134** | unchanged; `open_application` still risk 1, still no confirmation |
| Routes | 113 / 107 paths | **113 / 107 paths** | no new surface |

## 8. Open items

* **51-A** `open Flurbly.app` -- an unknown bundle -- now falls through to `read_file` and
  answers "File not found: `<home>`/Flurbly.app". Honest (it invents no domain and claims no
  success), but it names a path the user never wrote. The right answer is "I know no application
  called Flurbly".
* **51-B** The app-vs-site precedence reads the **static** application registry, not installed
  facts -- deliberately, since CI has no Calendar.app and parsing must not touch disk. On a
  machine without the app, `open Calendar` routes to the app and fails rather than falling back
  to the web page.
* **51-C** `close_app` has no answer for an application that will not respond: first-run Mail
  timed out at 20 s and the tool reported the timeout as the whole story. Quitting is not
  verified either way.
* **51-D** The launch probe is macOS-path-shaped. On Linux it compares the process basename, and
  on Windows `ps` does not exist, so a successful launch there is reported as *unverified* --
  correct, but it means the same sentence carries different evidence strength per platform.
* **51-E** `open the calendar site` opens the Google web calendar because that is the only
  address `NAMED_SITES` holds for the word. There is no route to iCloud's web calendar by name.
* **51-F** The other twelve §6 gaps are untouched: standard folders, Contacts, System Settings,
  `is Safari open?`, `search Google for X`, and the paraphrase set. Phase 49a's 49a-D stays
  open, narrowed to those.

## 9. Where this leaves the build prompt

§36 step 6 (Basic Mode) needed one more thing than a command list: the commands were partly
there and the *routes* were not. Two of the six §6 groups now answer as written, and the
mechanism -- a registered application outranking a site, an exit code not being an observation
-- is what the remaining twelve need. `open Contacts`, `open System Settings` and the folder set
are the same shape as Mail: the capability exists and no sentence reaches it.
