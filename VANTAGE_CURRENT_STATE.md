# Vantage — full working report, Phase 57 close-out

Written 2026-10-04, refreshed at each phase close; this pass is 2026-10-05. §2A–§2F are Phase 52,
53, 54, 55, 56 and 57. Every number below was produced by a command on this machine during this
session or the immediately preceding phases, and the command is named next to it. Where a
capability exists but was **not** re-measured today, it says so rather than implying it was.

---

## 1. Executive summary

| | |
|---|---|
| What it is | A personal AI operating system for macOS: one command pipeline, 145 governed capabilities (30 of them confirmation-gated), a laptop panel, a phone client, a signed `.app` delivered as files |
| Current phase | **Phase 57 — the laptop stays reachable after its window closes** (closed) |
| Test suite | **2273 passed · 0 failed · 0 xfailed · 0 skipped** — 66.98 to 74.22 s over three runs at this close |
| Spec §6 coverage | **37 of 37** — every utterance the build prompt lists reaches a capability |
| Delivered | `~/Downloads/Vantage/` — `Vantage.app` (running, seal valid), `Vantage.app.zip`, `vantage-source.zip`, `READ ME FIRST.txt` |
| Commits | `0cdc770` (Ph 56), `176bcfb` (Ph 55), `377e79f` (Ph 54 correction), `67d9530` (Ph 54), `35c9abf` (Ph 53), `3708f85` (Ph 52 correction), `92efc6b` (Ph 52), `f047eb2` (Ph 51), `452e9c0` (Ph 50) — **local only, nothing pushed** |
| Biggest known gap | Off the wifi there is nothing: the phone surface is a served web page, not a native app, with no keychain, biometrics, push or relay. Persistence now exists as a capability but has never been left **installed** on this machine (57-A -- a user decision), and the permission ask still cannot land because it lives in the helper (57-G, carried from 56-A). |

---

## 2A. Phase 52 in full — the §6 sentences about the laptop

### 2.1 What was asked and what was measured first

Continuing Phase 51's remaining list. The build prompt's §6 lists 37 utterances. All 37 were
run through `RulePlanner(build_default_registry()).parse(...)` before any edit:

| group | before | after | closed by |
|---|---|---|---|
| applications (13) | 9 / 13 | **13 / 13** | registry entries for Firefox, Contacts, Messages, System Settings |
| folders (4) | 0 / 4 | **4 / 4** | new `open_user_folder` tool |
| status (7) | 5 / 7 | **7 / 7** | route fixes — the answers already existed in `get_time` and `get_machine_snapshot` |
| web (5) | 4 / 5 | 4 / 5 | deferred: `search Google for X` needs a capability, not a route |
| observation (2) | 1 / 2 | **2 / 2** | new `get_frontmost_application` tool |
| Vantage about itself (6) | 2 / 6 | 2 / 6 | deferred to Phase 53 deliberately |
| **total** | **21 / 37** | **32 / 37** | |

The 16 gaps fell into three classes, and that classification is why the phase touched four
kinds of file: a missing **name** (registry), a missing **route** (planner regex), a missing
**capability** (new tool), and a missing **permission-aware answer** (found live, below).

### 2.2 What was built

| file | change | why it is shaped that way |
|---|---|---|
| `jarvis/registry/apps.py` | 4 applications registered (9 → 15 total) | `open_application` already worked; the names were absent. Launch names are what macOS answers to, not guesses |
| `jarvis/core/planner.py` | `_time` + `_machine_snapshot` widened; two new rules `_open_standard_folder`, `_frontmost_app` (48 → 50 routes) | The folder word must be the whole object of the sentence, or "open downloads.zip" becomes a folder request |
| `jarvis/tools/apps.py` | `open_user_folder`, `get_frontmost_application`, `bundle_installed`, bounded AppleScript deadline | See 2.3 and 2.4 |
| `jarvis/tools/agents.py` | `/System/Applications` added to `BUNDLE_ROOTS` | See 2.5 |
| `tests/test_phase52_section_six_on_the_laptop.py` | 52 tests | |

### 2.3 The folder allowlist — the design decision worth the most

`open_user_folder` takes a **name** from a five-entry dictionary (`desktop`, `downloads`,
`documents`, `pictures`, `home`) and builds the path from the home directory. It never accepts
a path. Consequences, all asserted by tests:

- `open ../../.ssh`, `open /etc`, `~/Library/Mail`, `passwords`, `""` → refusal that names what
  is allowed.
- A refused folder **launches nothing** — the test records every `run_command` argv and asserts
  the list is empty.
- `open Downloads` is **not** an alias of `open_project_folder`: one reveals a directory in
  Finder, the other hands a workspace to an editor. Phase 49a refused to blur them and this
  phase kept that.
- The parameter is canonicalised in the planner, so "my home folder" and "home" store the same
  value — a stored action must not depend on how the sentence happened to read.

### 2.4 Three defects only the live run found (the suite was green)

| # | symptom, measured | cause | after, measured |
|---|---|---|---|
| 1 | `open Downloads` → **20,250 ms**, "Finder did not answer (command timed out after 20.0s)" | the verification AppleScript inherited `run_command`'s 20 s default **inside a retry loop of four** → one sentence could take 80 s | **3,219 ms**, `UNKNOWN`, and the message says a permission prompt is probably waiting |
| 2 | `open Firefox` → "was told to open, but I could not confirm it" | launches are detached on purpose (a GUI app holds its pipe open), so `open -a`'s exit **1** for an absent app is never read | `FAILED` — "Firefox is not installed on this machine… my own application list is out of date" |
| 3 | `is Mail installed` → "**Mail is not installed on this laptop**" reported as a *success* | `BUNDLE_ROOTS` searched `/Applications`, `~/Applications`, `~/.qoder/bin` — but Mail, Contacts and Calendar live in `/System/Applications` | "Mail is installed (version 16.0) and is not running right now." |

Defect 1 broke this project's own Phase 34 rule ("one sentence, one deadline") while adding a
capability. Defect 3 predates the phase (the probe is from Phase 35) and was only visible
because §6 happens to name Apple's own applications.

**Two cases were kept apart on purpose** and both are tested: *not installed* is a failure with
a reason; *installed but not observed* stays `UNKNOWN`. Collapsing them is how a system starts
lying in one direction or the other.

### 2.5 What was NOT claimed

The tempting story for defect 1 was "the app lacks Automation permission" — Phase 50 proved
that macOS attributes permissions per responsible process. **The measurement did not support
it**: the same AppleScript run straight from a terminal also died with `AppleEvent timed out
(-1712)`. Finder answers no AppleScript from anyone on this machine right now, so
`open Downloads` is reported as UNKNOWN and that is the accurate answer, not a symptom of who
asked.

### 2.6 An error in this report's own numbers

The first draft said "24 of 37 worked, 13 did not" and "§6 at 30 of 37". Re-counting the GAP
rows at close-out: **21 worked, 16 did not**, and coverage is **32 of 37**. The code was fine;
the arithmetic in the write-up was not. Both the README row and
`audit/PHASE52_SECTION_SIX_ON_THE_LAPTOP_REPORT.md` now carry the corrected figures with the
correction noted in place.

### 2.7 Three guards failed on purpose, and what that taught

- `test_the_readme_counts_the_planner_routes_it_ships` caught the README still advertising 48
  routes. Corrected to 50. This is the guard doing exactly its job.
- Two Phase 49 guards had frozen the **tool total at 134** to prove an alias does not create a
  capability. A frozen total cannot survive legitimate growth (Phase 52 added two tools), so
  they now assert the invariant they were named for: registered specs equal distinct canonical
  names, `/tools` publishes exactly the registry, and no alias appears as its own row. That is
  a stronger statement than `== 134`, not a weaker one.

### 2.8 Live transcript (isolated instance, never the user's app)

`JARVIS_HOME=$(mktemp -d)`, port 8797/8798, master token from that home. Contacts was launched
by the drive and quit afterwards; nothing was written to any folder.

```
what date is it?             "It is 11:53 on Sunday, 04 October 2026."              6 ms
what is my hostname?         "Laptop condition: Abhijeets-MacBook-Air-2.local…"    49 ms
show the current application "Qoder is the application in front right now."       794 ms
open Contacts                "Contacts is open on your macos machine."           275 ms  verified=True
open Downloads   (after fix) UNKNOWN, "Finder did not answer in time…"          3219 ms
open Firefox     (after fix) FAILED,  "not installed on this machine"           1422 ms
is Mail installed (after fix) "Mail is installed (version 16.0)…"                 112 ms
```

---

## 2B. Phase 53 in full — Vantage answering questions about itself

**Measured first.** Four of the six §6 self-state sentences reached nothing, and three reached
something worse than nothing:

```
are you running            -> check_agent_status(app="You")
what is the current task   -> ask_question(topic="current task")
what are you doing         -> ask_question(topic="you doing right now")
what is your status        -> ask_question(topic="your status")
```

A question about Vantage answered by probing for an installed application literally named "You",
or handed to the general-knowledge tool, is a confident answer to the wrong question — worse than
a refusal, because a refusal at least tells the user something is missing.

**No new state store.** Five capabilities read what the process already owns:

| tool | risk | source | what it refuses to do |
|---|---|---|---|
| `get_pending_confirmations` | READ | `core.policy.pending()` | show a confirmation past its TTL, or synthesize one from audit rows |
| `get_vantage_status` | READ | the objects `/health` builds from | name a provider that is not configured |
| `get_recent_activity` | READ | `core.audit.recent()` | smooth `denied` / `blocked` / `unverified` into a success count |
| `get_current_task` | READ | `core.tasks.list()` + step rollup | claim work is running because a request arrived |
| `stop_current_task` | LOW | `core.tasks.cancel()` | say it stopped something without reading the status back |

`stop_current_task` is LOW and confirmation-free on purpose: §30 of the build prompt requires a
stop to be immediately executable, and PHYSICAL stays reserved for the emergency stop.
`cancel_schedule` was deliberately kept out of this — a schedule is a thing that runs later, a
task is one execution now, and conflating them cancels the wrong object while reporting a success.
A test asserts stopping a task leaves `scheduled_jobs` untouched.

**What the live run taught that no test suggested:** a command stopped at the permission gate
writes **no task row at all**. So "what is the current task" truthfully answered *"nothing is
running"* while an approval sat waiting — true, and useless. Both facts are now said:

```
Nothing is executing right now, but 1 action(s) is waiting for your approval --
ask me for pending approvals to see what they are.
```

**Also measured rather than assumed:** `LifecycleReport` defines `__bool__`, so `emergency_stop`'s
`if tasks.cancel(...)` is correct and not a latent bug — checked by building a rejected report and
testing it, not by reasoning about it.

**Live transcript** (isolated instance, ephemeral port chosen after the first choice turned out to
be occupied by an unrelated server — a 401 from something you did not start is not a test
subject):

```
delete /tmp/p53target/delete-me.txt  -> CONFIRM "This needs your confirmation before I run it."
what is the current task             -> "Nothing is executing right now, but 1 action(s) is
                                         waiting for your approval…"                    1 ms
show pending confirmations           -> "1 action(s) waiting: delete_file (CONF-77a0e54a…).
                                         Each expires 300 seconds after it was created." 1 ms
are you running                      -> "Vantage is running in Basic Mode: deterministic rules,
                                         no model. 141 capabilities across 15 agents,
                                         1 waiting for approval…"                       1 ms
what did you just do                 -> "4 most recent action(s): 3 success,
                                         1 awaiting_confirmation…"                      1 ms
stop the current task (idle)         -> "Nothing was stopped: no task is running."      1 ms

-- the cancel path, against a job with one finished and one pending step --
what is the current task -> Current task: "p53 live stop proof" -- running, 1 of 2 step(s) done.
stop the current task    -> Stopped "p53 live stop proof" -- the job is cancelled. Steps that
                            already ran are not undone (1 done). Not started or interrupted:
                            delete_file.                                          verified=True
sqlite read-back         -> ('JOB-P53LIVE', 'cancelled')      <- independent of the tool
```

**Browser: not performed, and no UI claim is made.** No page changed this phase.

**Five of this phase's own test assertions were wrong while the product was right** — `verified`
is a field not an output key; a Basic-Mode check that banned the word "model" and so failed on
"no model"; an empty-history check expecting "nothing" where the tool says "no recorded actions";
a no-op stop asserted non-SUCCESS when the real requirement is that it must not *claim* a stop;
and a helper that lost an import to a bulk edit, producing 16 `NameError`s that looked like
product failures. Each was fixed in the test, with the reason, and two of them changed what the
test asserts to the thing that actually matters.

**The suite caught what the module did not:** four self-state tests assumed an empty approval
queue and no active jobs. Alone they passed; in the full run they failed, because the session
database is shared. They now clear that state explicitly — the third time this project has been
bitten by the same leak.


## 2C. Phase 54 in full — the two safety doors

`emergency_stop` was a real PHYSICAL-tier tool reachable only by typing a sentence; the phone,
which the build prompt names first for a stop, could not pull it. `PermissionEngine.revoke`
wrote an explicit denial that **no product surface ever called** — the permission engine had one
door and no handle. This phase added no capability and no route: 141 tools and 51 routes before
and after, HTTP surface 107 → 111 paths.

Two decisions carry it.

**A report is not a result.** The stop counted a job as stopped when `cancel()` returned an
accepted report. It now re-reads the row and sorts on that, and the test forces the failure by
patching a cancel that accepts *and rolls the row back* — under the old code that claimed
success.

**Restore is not grant.** The obvious `grant(tool)` writes `granted=1`, and Phase 49a's rule is
that a standing grant downgrades CONFIRM to EXECUTE for reversible tools — so un-revoking would
have left the capability *more* trusted than before it was revoked. Restore deletes the denial
rows instead; proven live, where `delete …` came back as **CONFIRM**, not EXECUTE.

And `emergency_stop` itself cannot be revoked (409, no row written, a `denied` ledger entry): a
door lockable from outside is not a safety door.

Live, on an isolated instance, with SQLite read back independently of the tool:

```
POST /emergency_stop (no token)        -> 401
POST /permissions/not_a_tool/revoke    -> 404
POST /permissions/emergency_stop/revoke-> 409 "revoking it would remove a safety door"
"delete /tmp/p54t/victim.txt"          -> CONFIRM            (before)
POST /permissions/delete_file/revoke   -> {"revoked":true,"state":{"blocked":true,"standing_grant":false}}
"delete /tmp/p54t/victim.txt"          -> BLOCK  "permission for delete_file has been revoked."
POST /permissions/delete_file/restore  -> {"restored":true,"denials_removed":1,"state":{"revoked":false,"standing_grant":false}}
"delete /tmp/p54t/victim.txt"          -> CONFIRM again      (declared policy, not a grant)
POST /emergency_stop                   -> verified: True, cancelled: 1, stuck: []
sqlite                                 -> ('JOB-P54LIVE', 'cancelled')
audit                                  -> emergency.stop: success; tool.revoke: denied, success; restore_tool: success
/tmp/p54t/victim.txt still on disk     -> nothing was deleted
```

**A Phase 53 defect fell out of a Phase 54 test:** `self_state.py` counted finished steps with
`("success", "done")`, but the enum value is **`succeeded`** — so every task report said "0 of N
step(s) done". Phase 53's own live proof had hand-written the wrong status string, which is why
both the suite and the live run agreed with the bug. Fixed against the real vocabulary.

**Browser:** both pages were driven at 375 and 1440 against the isolated instance — the stop
control present and wired on both, the panel's new Policy column showing 140 Revoke controls
with `emergency_stop` rendered as *safety interlock*, 0 console errors on either page. The two
buttons were **not clicked**: the session's permission classifier blocked both, and the refusal
was respected rather than routed around. So they are proven wired and rendered, not proven
pressed.


## 2D. Phase 55 in full — a search that searches

`search Google for FastAPI` was §6's last utterance without a capability, and the tempting fix —
route it at `open_url` with `https://www.google.com` — is a lie that renders perfectly: a browser
opens, a page loads, nothing was searched, and the reply reads like a success.

The risk tier was measured rather than guessed: **all 13 EXTERNAL tools are confirmation-gated**,
so EXTERNAL here means "changes something outside". A search changes nothing, and its closest
precedent is `open_url` — reaches out, reads, LOW, ungated. A search behind a per-query approval
would be a search nobody uses and would not make anything safer.

Three outcomes, kept distinct (the same rule Phase 15 established for mail and calendar):

```text
answered with something     -> results, and the source is named
answered with nothing       -> ok, count 0: "an empty answer from one source,
                               not a claim that nothing exists"
could not be reached/read   -> FAILED: "This is the network, not an empty result."
```

Live, real network, no mocks:

```
search Google for FastAPI  -> "duckduckgo-instant-answer had no result for 'FastAPI'. That is an
                               empty answer from one source…"                          281 ms
search the web for the heat death of the universe
  -> 2 real results (Wikipedia, DDG) in 143 ms
outage, against a black-hole address (a real connect attempt)
  -> FAILED in 5.01 s: "This is the network, not an empty result."
```

`FastAPI` returning nothing is the interesting result: the Instant Answer API has no article for
it, and the tool said so rather than inventing a summary or quietly opening a browser. The 5.01 s
outage is inside the sentence deadline Phase 34 set — the case that used to cost 20 seconds.

The query is confined to its own urlencoded parameter, and the test asserts the parsed contract
rather than pattern-matching the string. **My first assertion was wrong and the code was right:**
I checked that `evil.example` did not appear in the URL, when its appearing *inside the encoded
value* is correct behaviour — the defect would be a second parameter or a changed host.

**§6 is now 37 of 37**, re-run utterance by utterance through the planner.


## 2E. Phase 56 in full — asking for a permission, on purpose

Gap E. Phase 50 built the permission card and deliberately made it **read-only**, because the
dashboard polls it and a status check that pops a system dialog every poll is not a status check.
This phase adds the other half: a request a person starts.

The whole phase rests on one rule — **the answer comes from re-reading the state, never from what
the request reported.** That rule was forced by macOS's own inconsistency, measured rather than
assumed:

```text
AVCaptureDevice.requestAccess / SFSpeechRecognizer.requestAuthorization
    -> give back a bool for "the user decided yes"; the status query is still the authority
AXIsProcessTrustedWithOptions(prompt)
    -> grants NOTHING. Opens System Settings and returns the trust flag as it was
       BEFORE the user acted. Reporting it would print "granted" about an unfinished dialog.
CGRequestScreenCaptureAccess
    -> can return true while the running process still needs a relaunch to be covered
```

So `request()` ends in `_read_state(cap)`, and the test that pins the rule patches a request
claiming `granted` against a re-read saying `DENIED` and requires the answer to be `DENIED`.

**Four capabilities can be asked** (microphone, speech recognition, screen recording,
accessibility — the last `prompt_only`); **three refuse with a reason that reaches the user**:
automation ("the only probe is sending an event, which is the action itself, not a settings
page"), notifications (the framework needs the app's own bundle and run loop), local network
(macOS does not gate a server for it).

**Loopback-only, and that is a separate gate from the host check.** A phone on the wifi passes the
host gate because its `Host` *is* an address this machine owns — which is correct for reading a
dashboard and wrong for raising a dialog. A system prompt appears over the whole screen in front
of the laptop, so only someone sitting there may raise it. Tested from a LAN-shaped peer address,
not argued.

Then the real binary said something no test could. The suite patches the helper seam, so it cannot
see whether the helper *works*:

```
status mode  -> speech=not_determined  microphone=denied   (works)
request mode -> exit -6 (SIGABRT), no stdout               (does not work)
```

The Speech framework aborts a process that is not an app bundle — Phase 50's
`UNUserNotificationCenter` finding, met again on the request side. **A helper binary cannot raise
these prompts; only the app process can.** The implementation was changed to carry that rather
than hide it:

```
ok: False | state: UNKNOWN | granted: False
"I could not ask macOS about speech recognition, so nothing about it has changed
 (the helper could not ask (exit code -6)). Set it yourself in System Settings →
 Privacy & Security → Speech Recognition, then check again."
```

`UNKNOWN`, not `DENIED` — a failure to ask and a refusal are different facts, and reporting the
first as the second would send someone to System Settings to undo a decision that was never made.

**So this phase ships the door and not the room behind it.** The route, the guard, the reasons,
the re-read rule and the panel controls are real and tested; the ask itself cannot succeed until it
runs inside `Vantage.app`, which is Phase 57. Driving the microphone / screen-recording / speech
prompts was **not done** and is recorded as `BLOCKED_BY_USER_DECISION` — doing it unattended would
be the exact un-consented prompt this phase exists to prevent.

One safety assertion was deliberately **narrowed**, and it is stated here rather than left in the
diff: Phase 50's guard banned a prompting call anywhere in the Swift file. Phase 56 legitimately
adds a request mode to the same binary, so the file-wide ban would have blocked real work. The
guard now protects what it was always protecting — *the status read must not prompt* — enforced by
explicit `// REQUEST PATH` / `// STATUS READ PATH` markers, with the prompting calls still required
to exist and now required to be **only** on the request side.

Three of my own defects, all found by the suite or by running things:
a test stub indexed the helper's arguments wrongly (`cmd[2]` where the call is
`[binary, "request", key]`) so the first version of the abort test **executed the real helper**;
a guard asserted `AXIsProcessTrusted` must not appear in the request path, which is unsatisfiable
because `AXIsProcessTrustedWithOptions` contains it; and `TestClient`'s default socket peer is
`"testclient"`, neither loopback nor real, so a loopback-only rule read every test caller as
remote until the fixture set the peer explicitly.

142 tools and 52 routes unchanged — **this phase adds no capability**; 28 gated, **no gate moved**.

Rebuilt, re-signed and re-delivered at the close, then checked on the running `~/Downloads/Vantage/Vantage.app`
with **GETs only** plus one GET expecting 405 (route present, not invoked): `/health` 142 tools /
Basic Mode, `/permissions` carrying the three new fields with the same seven states as before, and
the panel HTML containing every control the user-facing readme now describes. No POST was sent to
the delivered app — Phase 54's §11a exists precisely because a "read-only" check once wrote an
audit row by POSTing at it.


## 2F. Phase 57 in full — the laptop stays reachable after its window closes

Gap F, and the thing Phase 56 needed and did not have: a process that outlives its window. The
`.app` had been a launcher since the beginning — it starts the server, and its death stops it —
which was the correct fix for Phase 40's orphan server and the reason a phone in your hand talks
to a machine that has to be sitting open.

Five decisions, each with a reason that came from measurement:

```text
user LaunchAgent, not a daemon   ~/Library/LaunchAgents, runs as you, no elevation
KeepAlive{SuccessfulExit:false}  a crash is retried, a clean stop is respected;
                                 KeepAlive=true turns a port conflict into a loop
closed variable list             no token, and no inherited JARVIS_GUARDIAN_PID --
                                 the watchdog exits in 2 s when its parent is gone,
                                 and launchd would be the parent
the path guard did not move      LaunchAgents stays protected for ordinary writes;
                                 persistence is its own gated capability instead
EXTERNAL + confirmation          both writes change what runs unattended
```

Then the machine corrected me four times, and all four are now tests:

1. **`launchctl print gui/<uid> <label>` does not fail.** It dumps the whole domain and exits 0,
   so my status said "loaded" on a machine that had never been installed, and could read an
   unrelated service's pid as this job's. The target is now one argument *and* the reply must name
   this label — an answer that is not about this job is not evidence.
2. **uvicorn starts the application before it notices the port is taken.** The losing instance
   logged `scheduler started`, then `[Errno 48] address already in use`, then exited **3** — the
   double-scheduler the guard exists to prevent, plus a restart loop. The port is probed before the
   app wakes; re-measured live, the losing run logs zero `scheduler started` and exits 0.
3. **`bootout` is not synchronous.** One immediate re-read found the job still in the table and
   reported a refusal about a job that had stopped, leaving the plist behind. Teardown now gets a
   bounded window (3 s) that bounds the *wait* without deciding the *answer*.
4. **A polling card erased its own answer.** The press wrote its reply into a note rendered inside
   the card, and the re-read on the next line rewrote that card — default text back, so on screen
   nothing had happened. `#permNote` had the same defect from Phase 56. Both notes now live outside
   the polled container, verified 12 s later in a real browser.

**One design decision changed mid-phase, in the stricter direction of consistency.** The first test
banned *every* sentence from reaching `enable_host_persistence`. That is not how this registry
works — 27 other gated capabilities are reachable by sentence precisely because a person approves
the card. So the ambiguous phrasings ("start vantage on login", "keep running when I close the
window") get the **state**, and only the unambiguous "install the login item" reaches the gated
tool, where the gate is the confirmation. The route table asserts the gated-ness from the registry,
not from prose.

**Three frozen `== 28` gate counts became an invariant** (every EXTERNAL and DESTRUCTIVE capability
requires confirmation; the only ungated PHYSICAL one is the stop, on purpose). A total can be
satisfied by removing one gate and adding two elsewhere. The negative control was run rather than
assumed: loosening `delete_file` is caught, gating `emergency_stop` is caught.

Live against the real launchd, on an isolated home and port, then removed:

```
enable()  -> state=loaded pid=14049 ; launchctl: state = running
/health   -> status ok | tools 145 | scheduler running     (no window, no terminal)
plist     -> token present? False | JARVIS_GUARDIAN_PID present? False
disable() -> not_installed ; file gone ; port closed ; no residue on the machine
```

**And what it does *not* claim:** the login item has never been left installed for the real app —
that is 57-A, a user decision, with the control in place to make it. No reboot was performed
(57-F). The permission ask still cannot prompt, because it still lives in the helper (57-G).

Measured after: **2273 tests passing**, 145 tools (+3), 30 gated (+2, none removed), 52 routes,
119 HTTP route objects / 113 paths, 84 test modules, 59 phase reports, 70,190 lines.


## 3. Working status of the whole product, as measured today

All values from the **delivered app** over HTTP (read-only GETs) unless stated.

### 3.1 The pipeline — intact and enforced

```
User → Intent → RulePlanner (50 routes) → PermissionEngine → [Confirmation] → Tool
     → Verification (success | failed | unknown) → Memory → Audit → Response
```

- `Verification` is the only authority on success. A handler returning is not the world
  changing — that separation is what Phases 51 and 52 kept having to re-learn.
- `llm_enabled: false` on this machine right now (`/health`). **Basic Mode is the live path**:
  every sentence above was answered by deterministic rules with no model call.

### 3.2 Surface inventory

| thing | value | how measured |
|---|---|---|
| Tools | **145** | `/health`, `/tools` |
| Agents | **15** — calendar, car, coding, design, email, home, laptop, laptop_center, memory, phone, research, robotics, scheduler, vision, watch | `/health` |
| Risk spread | READ 77 · LOW 38 · EXTERNAL 15 · DESTRUCTIVE 12 · PHYSICAL 3 | registry, counted this close |
| Confirmation-gated | **30** of 145 — gates added in Phase 57, none removed (§7 of that report proves the direction) | registry |
| RulePlanner routes | **52** | `len(core.planner._rules)` |
| Registered applications | **15** | `AppRegistry` |
| HTTP surface | 119 route objects / **113 paths** (Phase 54 the stop and the policy doors, Phase 56 the permission request, Phase 57 the start-up read) | `len(app.routes)` |
| Database tables | **32** | read-only `sqlite_master` count on the delivered app's database |
| Memories indexed | 52 / 52, coverage 1.0, hybrid retrieval | `/health` |
| Scheduler | running, 30 s tick, 67 jobs | `/health` |
| Test suite | 2273 passed, 0 failed, 0 skipped; 66.98–74.22 s over three close-out runs | `pytest -q` |
| Test modules / phase reports | 84 / 59 | `ls` |
| Code volume | 70,190 lines — 41,887 source · 28,303 tests | `find jarvis dashboard app … \| xargs wc -l` |

### 3.3 Permissions, as the OS reports them right now (`/permissions`)

| capability | state |
|---|---|
| speech recognition | **GRANTED** |
| screen recording | **GRANTED** |
| microphone | NOT_DETERMINED |
| accessibility | **REQUIRES_MANUAL_SYSTEM_SETTINGS** |
| automation | UNKNOWN |
| notifications | UNKNOWN |
| local network | NOT_REQUIRED |

The card also prints **which process was asked** — `about_bundled_host: True`, read by Vantage
running from `~/Downloads/Vantage/Vantage.app`. Same helper, same machine, terminal caller:
speech NOT_DETERMINED, screen recording denied, microphone DENIED, accessibility GRANTED. Four
capabilities, four different answers, which is why a status without a subject is not a fact.

Since Phase 56 each row also carries `requestable`, `prompt_only` and `no_request_reason`, so a
client can tell "you may ask macOS" from "only System Settings can change this" from "there is no
ask to make". The ask is a **separate, deliberately-pressed action** (`POST /permissions/{name}/request`,
loopback-only) and is still not a side effect of the poll that fills this table. **It has not been
observed raising a dialog on this machine** — the helper aborts before prompting (56-A), and the
prompt itself was left to the person sitting at the laptop.

Two consequences that are live behaviour, not caveats:
- `show the current application` works from a terminal-launched server and will answer with the
  Accessibility permission name **from the bundle** until it is granted.
- `open Downloads` cannot be confirmed by anyone here (Finder answers no AppleScript).

### 3.4 Networking and the phone

- `/health` publishes three phone URLs: loopback, `172.20.168.82` (current lease) and
  **`http://abhijeets-macbook-air-2.local:8765/phone`** — the mDNS name, which survives a lease
  renewal (Phase 48). A `.local` name is accepted only after resolving to an address this
  machine owns.
- Auth: bootstrap code → `/auth/session` → short-lived session token; the master token never
  leaves the process. Host-header and origin gates refuse a forged `Host:` with 400.
- Pairing is two-sided: `/devices/pair/request` → pending → Mac-side `approve`/`deny`. Scanning
  a QR is not authorisation.
- Device records: `POST /devices/{id}/revoke` exists and **the host refuses to be revoked or
  forgotten** (Phase 46a), because revoking `laptop_main` once took the whole product down.

### 3.5 Audit — one honest red light

`/metrics` on the delivered app: `sampled: 500`, `chain_checked: 495`, **`chain_intact: false`**,
and `by_result` includes **`forged: 5`**. This is the documented C-2/C-2a state: deliberate
tamper-test rows are in this machine's real history, so the hash chain correctly reports
BROKEN. The dashboard shows "Audit chain: BROKEN" for exactly that reason. It is the verifier
working, not a broken verifier — but it also means **the user's history contains rows that were
inserted to test detection**, and cleaning them is a decision, not a fix.

### 3.6 Voice

Working and proven **on-device, no key, nothing sent over a network** — measured in Phase 47
(2026-10-04) with a real spoken clip through the delivered app, and `/voice/status` still
reports `enabled: true` with wake words today. The Phase 43–46 diagnosis ("macOS denied the
microphone") was wrong for four phases: the real causes were a main-thread deadlock in the
Swift helper and an x86_64 build on an arm64 machine. Not re-driven by ear today.

### 3.7 What is **not** built or not reachable

| gap | evidence | why it matters |
|---|---|---|
| ~~No emergency-stop route~~ | **closed in Phase 54** — `POST /emergency_stop` calls the registered tool, needs no confirmation, and re-reads the job rows before claiming a stop | menu-bar and lock-screen surfaces still need the native shell |
| ~~No tool-level revoke surface~~ | **closed in Phase 54** — `POST /permissions/{tool}/revoke` and `/restore`, with the denial re-read after the write and `emergency_stop` itself refused | no expiry and no bulk form (54-C) |
| ~~Vantage cannot describe itself~~ | **closed in Phase 53** — approvals, status, recent actions, current task and a verified stop are now answerable | was blocking phone-side approvals and task state |
| No native mobile app | the phone client is a served web page (`/phone`) | §2B of the build prompt asks for iOS/Android with keychain/keystore, biometrics, push |
| No relay, no push, no biometrics | none in the tree | remote-away-from-LAN use is not possible today |
| No provider adapters in use | `llm_enabled: false`; OpenAI/Gemini adapters per §22–24 not built | LLM planning is off; Basic Mode carries everything |
| No Computer Use capability | §26 not started | by design — after Basic Mode and pairing are stable |
| ~~No first-run permission **request** path~~ | **narrowed in Phase 56** — `POST /permissions/{capability}/request` exists, is loopback-only, refuses with a reason, and answers from a re-read. The **ask itself cannot succeed from the helper process** (the Speech framework aborts a non-bundle caller), so no dialog has been observed on this machine — 56-A | §3's wizard still needs the ask moved *into* the app process, which now exists but does not host it yet — 57-G |
| ~~No host persistence~~ | **built in Phase 57** — `jarvis/persistence.py` writes a user LaunchAgent with no secret and no inherited watchdog, `GET /persistence` reports what launchd says, and enable/disable are confirmation-gated capabilities. Proven live: launchd started a Vantage answering `/health` with 145 tools and a running scheduler, with no window open — then it was removed and the machine was left as found | never left installed for the real app (57-A, a user decision); "loaded" does not yet read `last exit code` (57-B); no reboot was performed (57-F) |
| `.app` ships no Python runtime | it uses this machine's interpreter (Phase 40) | "installable on another Mac" is not proven |
| CI | **not measured this session** — no verdict claimed | |

---

## 4. Feature catalogue (what you can actually ask, today)

Reachable by typed sentence, the mic, the phone page, or `POST /command`. Every action goes
through the same gates.

**Laptop facts** — battery, storage, system status, machine condition (hostname/os/cpu/memory/
disk/battery), time and date, running-app probe (`is Safari running` → installed, version,
running), frontmost application, screenshot, processes.

**Applications** — the 15 names the registry actually holds: Safari, Chrome, Firefox, Finder,
Terminal, Notes, Calendar, Contacts, Mail, Music, Messages, System Settings, VS Code, Cursor,
Calculator (open and quit). Brave and Edge are **not** registered, so "open Brave" is not a
capability today. Opening a project folder in an installed editor is separate, and it **reads
the editor's own record** to prove the folder opened rather than trusting the launch.

**Files** — search by name, find-and-open, list a directory, read, write, append, copy/move,
compress, delete (DESTRUCTIVE, confirmed). Path guards live in `jarvis/security/paths.py`:
**10 protected roots** (`/`, `/System`, `/Library`, `/usr`, `/etc`, `/Applications`, …) and
**29 protected subpaths** (`.ssh`, `.gnupg`, `.aws`, `.kube`, `.git-credentials`, `.npmrc`,
`Library/Keychains`, `Library/Cookies`, shell rc files, `Library/LaunchAgents`,
`Library/LaunchDaemons`, …). Refused, never merely confirmed — a guard that asks permission is
a guard that can be talked out of it.

**Web** — open a known site by name or full address (14 names, longest-match wins), open the
*site* wording for a word that is also an app, open a browser by name, and afterwards ask the
browser what it is showing — with `unverified` when it will not answer. **Never** a guessed
domain.

**Email & calendar** — search/read/summarise/draft/send (local maildir and local calendar; an
unreachable server is reported as an outage, not as an empty day).

**Memory** — remember, recall, recall-by-day, overview; hybrid retrieval with an index that
reports its own coverage.

**Research** — arXiv search and storage, citations that fail rather than invent, notes,
gap register, briefs.

**Coding** — drive Claude Code with an approval card first; workspace/project status, code
search.

**Automation & devices** — scheduled jobs with a real re-dispatcher, briefings and reminders;
paired phone/watch/car/home/robot tools, each refusing to act on a device the sentence did not
identify.

**Governance** — 28 confirmation-gated tools, 128-bit confirmation IDs with a 300 s TTL,
explicit approve/deny, pre-execution revalidation, hash-chained audit, tri-state results,
memory deletion scopes, redaction of secrets in logs, `POST /emergency_stop` (a panel button since
Phase 54, which re-reads the job rows before claiming a stop), per-tool revoke/restore on the
policy engine plus the panel's Policy column (Phase 54), a user-started macOS permission
**request** that answers only from a re-read (Phase 56), and a login item that is EXTERNAL-tier,
confirmation-gated and reported from launchd rather than from the request (Phase 57).

**Surfaces** — laptop panel (`/dashboard`, 1440 px, quick actions from the live registry, tool
filter with a Policy column, devices with Forget, Permissions card with "Grant access" and "Open
System Settings", Start-up card with "Install the login item", emergency stop, voice state), phone
client (`/phone`, 375 px, six quick actions, mic, approvals list, emergency stop), manifest +
service worker + icons, CLI, and the `.app` launcher -- which since Phase 57 no longer has to be
the only thing keeping the server alive.

---

## 5. What is needed, in order, with the reason

1. ~~Vantage talking about itself~~ — **done in Phase 53.** Approvals, status, recent
   actions, the current task and a verified stop are now answerable, which unblocks the phone-side
   governance in §14/§16/§30 of the build prompt.
2. ~~An emergency-stop route and a tool-revoke handle~~ — **done in Phase 54**: both doors
   exist, are audited, and re-read the state they claim to have changed.
3. ~~A web-search capability~~ — **done in Phase 55.** `search_web` asks a real search service,
   and an outage is reported as an outage. §6 is 37 of 37. A *ranked* web index still needs a
   keyed provider (55-B).
4. ~~Host persistence~~ — **built in Phase 57.** A user LaunchAgent, no secret in the launch
   config, no inherited watchdog, one server per data directory, and the state read back from
   launchd. What it deliberately does not do is *stay on*: installing it for real is 57-A, a user
   decision, and the panel control plus the confirmation card exist to make it.
5. ~~A deliberate, user-initiated permission **request** path~~ — **built in Phase 56**: route,
   loopback-only guard, per-capability reasons, re-read authority, panel controls, audited.
   **The §3 first-run wizard is still not built** (56-B), and Phase 57's host process did not
   remove the blocker — the ask still runs in the helper, which macOS will not let prompt (57-G).
   Read-only status remains, and asking is still never a side effect of a poll.
6. **Native mobile shell** (keychain/keystore, biometrics, push, deep links), then the
   **relay** for off-LAN use, then **provider adapters** (OpenAI, Gemini, local) behind the
   same PermissionEngine, then **Computer Use** last, as §26 orders.
7. **Decisions the user owns, not fixes**: whether to clear the 5 deliberate `forged` audit
   rows that make the chain read BROKEN; whether to grant the bundle Accessibility (which
   unlocks `show the current application` and Finder confirmation); whether to clean the
   **9 `phone_*` rows** measured today on the delivered app — 13 device rows in total, of which
   only one is a real handset. **New in Phase 56:** whether to press **Grant access** on the panel
   while sitting at the laptop, which is the only way to observe the request path raising a real
   dialog. Until that is done the honest statement is that the ask is implemented and tested
   against stubs and one real abort, and **has not been observed prompting this machine.**

Carried open items, all still true: **49a-A…F**, **50-A…F**, **51-A…F**, **52-A…F**,
**53-A…F**, **54-A…F**, **55-A…E**, **56-A…F**, **57-A…G** — each written up in its phase report
and indexed in `README.md`.

---

## 6. How work is done here (the rhythm that produced all of the above)

1. **Measure first**, against the real registry, before writing anything.
2. Write the test **red**, and let it name the defect in its own assertion message.
3. Fix the truth, never the gate: no permission check or confirmation is weakened to make a
   test pass, and no existing test is edited merely to go green (Phase 52's two count-guards
   were changed to assert an invariant, with the reason recorded).
4. **Prove it live** over HTTP on an isolated `JARVIS_HOME=$(mktemp -d)` — never a write
   against the user's running app — and independently check the world state (`ps`, the editor's
   own storage) instead of trusting the tool's reply.
5. Check it in a **real browser** when a page is involved; several defects in this project's
   history have only been visible there.
6. **Re-measure and re-publish every number** — README, badges, charts, `suite_history.json`.
   A metric that is not re-run is a claim that is quietly rotting.
7. Write `audit/PHASEnn_*.md`, commit **locally only**, rebuild the bundle, redeploy, relaunch,
   re-verify the extracted archive's seal, and update `~/Downloads/Vantage/`.
8. Report what failed as failure. `close Mail` timed out and that is in the Phase 51 report;
   the button tap that the session's permission classifier blocked is in the Phase 51 report as
   *not performed*.

Standing rules that have not moved: no GitHub pushes; no secrets in commits; no fabricated
hardware, account or verification state; `JARVIS_HOME=$(mktemp -d)` for every project Python
run; nothing written to `~/.jarvis-forge` by hand; monkeypatches restored; historical reports
edited only additively.

---

## 7. Where to look for the detail

| question | file |
|---|---|
| What the product is and how to use it | `README.md`, `~/Downloads/Vantage/READ ME FIRST.txt` |
| Why each phase did what it did | `audit/PHASE*.md` (59 reports, newest first in the README table) |
| What is still open | the open-items block at the end of `README.md` |
| Why a permission ask must not be a poll | `audit/PHASE50_*.md` (the read-only rule) and `audit/PHASE56_ASKING_ON_PURPOSE_REPORT.md` (the request path, and why a helper cannot use it) |
| Suite size over time | `benchmarks/suite_history.json`, `assets/chart-suite-growth.svg` |
| Safety and failure matrices | `audit/*.csv` (4 matrices) + Phase 22/31/32 reports |
| This phase in the audit trail | `audit/PHASE52_SECTION_SIX_ON_THE_LAPTOP_REPORT.md` |
