# Phase 57 — the laptop stays reachable after its window closes

|                               |                                                                   |
| ----------------------------- | ----------------------------------------------------------------- |
| Phase                         | 57                                                                |
| Date                          | 2026-10-05                                                        |
| Starting commit               | `7c828fa` (Phase 56 close-out)                                    |
| Ending commit                 | `a20e39c` (the work) → the close-out commit named in §13          |
| Tests before                  | 2218 passed · 0 failed · 0 skipped                                |
| Tests after                   | **2273 passed · 0 failed · 0 skipped** (§10)                      |
| Tools before / after          | 142 / **145** (one READ, two EXTERNAL, both confirmation-gated)   |
| Planner routes before / after | 52 / **52** (three new branches inside the existing self rule)    |
| HTTP surface                  | 118 / 112 paths → **119 / 113**                                   |
| Confirmation-gated            | 28 → **30** — gates added, none removed (§7 proves the direction) |
| Push status                   | **nothing pushed; local commits only**                            |

Gap F of the operating prompt, and the prerequisite Phase 56 discovered the hard way: a macOS
privacy prompt cannot be raised by a helper binary, only by the app process — and until this phase
there was no app process that outlived its window.

---

## 1. What "persistence" was measured to mean before anything was written

Every claim below was checked in the code before it was designed against:

- `app/launcher.py:392` sets `JARVIS_GUARDIAN_PID` on the server it spawns, and
  `jarvis/process.py:23-39` exits within two seconds when `os.getppid()` no longer matches it.
  That is the mechanism that keeps Phase 40's orphan server from happening. **It is also a trap:**
  a process started by launchd is parented by launchd, so inheriting that variable would make the
  new service kill itself on every login.
- There is no persistence code anywhere in the tree — no launchd, no `SMAppService`, no login
  item. The only `.plist` in the bundle is its own `Info.plist`.
- `Library/LaunchAgents` and `Library/LaunchDaemons` are in `PROTECTED_SUBPATHS`
  (`jarvis/security/paths.py:65-66`), headed _"persistence: code that runs without the user
  present"_. So an ordinary file write there is refused, and five test modules pin that refusal.
- `run.py` had **no bind-failure handling**: `uvicorn.run(...)` was called bare.
- `registry/devices.py:26` fixes `HOST_DEVICE_ID = "laptop_main"` and `device_id` is a primary key,
  so a second server cannot create a second host row. That is why double-start is silent rather
  than loud — the visible symptom would have been "two schedulers, one database".

## 2. The five decisions, and the reason each one is shaped that way

1. **A user LaunchAgent, not a daemon.** `~/Library/LaunchAgents`, runs as the user, at login, no
   elevation. `agent_path()` has a test asserting it is never the Daemons folder.
2. **`KeepAlive = {SuccessfulExit: false}`, not `true`.** A port conflict exits non-zero today, so
   `true` would turn one laptop into an infinite restart loop. A crash is retried; a clean stop is
   respected.
3. **No secret in the launch config.** The plist writes five variables and nothing else — an
   explicit closed list, because "inherit the environment" is exactly how a token ends up in a
   world-readable file. A test asserts the token string never appears, and that
   `JARVIS_GUARDIAN_PID` never appears. This follows the Phase 45a precedent: the key lives beside
   the data at 0600, not inside the bundle and not in launch config.
4. **The path guard was not widened.** `jarvis/persistence.py` writes its own file directly and
   does not import the file tools — and two tests assert both halves: the file tools still refuse
   a write into `LaunchAgents`, and the persistence module still does not route through them.
   Widening the guard would have handed every sentence the power to install a login item.
5. **Enable and disable are EXTERNAL and confirmation-gated; reading is READ.** In this registry
   EXTERNAL means "changes something outside", and every EXTERNAL tool is gated. A login item is
   the strongest case for that gate in the product.

## 3. Four defects only the real machine told me about

The suite was green for the first three. All four are now pinned by tests.

**3.1 `launchctl print gui/<uid> <label>` does not fail.** It prints the _whole domain_
(`service count = 444`) and exits 0. My `_launchd_state` treated exit 0 as "loaded", so the very
first live call reported `loaded_without_file` on a machine where nothing had ever been installed
— and `pid` was free to be lifted out of an unrelated service's entry. Measured side by side:

```
$ launchctl print gui/501 com.jarvisforge.vantage    -> gui/501 = { type = login ... }   exit 0
$ launchctl print gui/501/com.jarvisforge.vantage    -> Bad request. Could not find service   exit 0
```

Both exit 0. So the target is now one argument _and_ the reply has to name this label — an answer
that is not about this job is not evidence. A test asserts the argv shape and feeds an exit-0
domain dump expecting `loaded: False`.

**3.2 uvicorn starts the application before it discovers the port is taken.** The losing instance
logged `scheduler started`, `Application startup complete`, then
`[Errno 48] ... address already in use`, and exited **3**. So the thing the single-instance guard
exists to prevent — a second scheduler over one database — happened anyway, _and_ the non-zero
exit would have made launchd retry it forever. Fixed by `port_is_free()`: `serve_with_claim` asks the port before the application is allowed to wake up. Re-measured live
after the fix: `grep -c 'scheduler started'` on the losing run returns **0**, exit **0**.

**3.3 `bootout` is not synchronous.** `disable()` booted the job out, re-read once, still found
it, and reported _"launchd still reports the job loaded"_ about a job that had in fact stopped —
while leaving the plist on disk. Teardown now gets a bounded window (`DEFAULT_SETTLE_S = 3.0`,
150 ms polls) that bounds the _wait_ without deciding the _answer_: a job that outlasts the window
is still a refusal with the file left where it is, and that case has its own test.

**3.4 A card that polls erased the answer it had just been given.** In the browser, pressing
_Install the login item_ set the note to "It is waiting for you on the Approvals card", and the
re-read on the next line rewrote the card's `innerHTML` — including the note — back to its default
text. On screen: nothing happened. `#permNote` had the same defect from Phase 56, for the same
reason. Both notes now live outside the container a poll rewrites, the press's answer survives at
least one 8-second refresh cycle (verified 12 s later in the page), and _Check again_ replaces the
stale answer with "Re-read from launchd just now." rather than leaving last time's verdict on
screen.

## 4. One design decision I changed mid-phase, and why

The first test for this phase asserted that **no sentence at all** could reach
`enable_host_persistence`. That is not the architecture: 27 other gated capabilities are reachable
by sentence precisely _because_ a person approves the card first, and Phase 32 already guarantees
the words accepted and the thing approved are the same thing. Banning the sentence would have made
persistence the one capability whose gate was the route instead of the approval — and a gate you
cannot exercise from the pipeline is a gate nobody can audit.

What stays banned is the **ambiguous** phrasing. `"start vantage on login"`,
`"make vantage a login item"`, `"keep running when I close the window"` all get the _state_, because
a question and a command are the same words to a regex and only one of them is a command. The
unambiguous `"install the login item"` reaches the gated tool. Both halves are tested, and the
gated-ness is asserted from the registry, not from prose.

## 5. Single instance, and what two servers would actually have done

The device row cannot duplicate, so the damage was never going to show up as "two hosts". It is
`jarvis/persistence.py:acquire_claim` — a `server.claim` file beside the data holding
`pid/host/port/started` and nothing else (a test asserts no secret lands in it, which is why the
refusal message is printed and the file is not). A live holder blocks a second start; a dead one is
reclaimed, because a laptop that cannot restart without a human is a laptop that stays down; and a
release from the wrong pid does not steal the lock.

Live, on isolated homes, never against `~/.jarvis-forge`:

```
start  (port 8899)          -> claim written: {"pid": 874, "host": "127.0.0.1", "port": 8899}
second start, same home     -> exit 0, "another Vantage server (pid 874) is already running…"
                               claim still held by 874 (the refusal did not steal it), one listener
second start, other home    -> exit 0, "something is already answering on 127.0.0.1:8899…"
                               'scheduler started' count in its log: 0
kill -TERM the winner       -> claim released, port closed
kill -9  the winner         -> stale claim left; next start reclaims it and /health answers
```

## 6. The login item, against the real launchd

Scoped deliberately: an isolated `JARVIS_HOME`, port 8905, and a `disable()` at the end that leaves
nothing behind. Nothing here touched `~/.jarvis-forge` or the delivered app.

```
1.  status before : not_installed
2.  enable()      : ok=True state=loaded pid=14049
3.  status after  : loaded | launchd pid 14049
4.  launchctl print says: state = running | pid = 14049 | state = active
5.  lsof :8905 LISTEN: 14049
6.  /health from the service: status ok | tools 145 | scheduler running
7.  claim written by the service itself: {"pid": 14049, "host":"127.0.0.1","port":8905}
8.  token in the plist? False | JARVIS_GUARDIAN_PID in the plist? False
9.  disable()     : ok=True state=not_installed
10. status last   : not_installed | file present: False | listeners: none
```

Line 6 is the headline: **launchd started a Vantage that answers `/health` with 145 tools and a
running scheduler, with no window and no terminal involved.** Line 8 is the two rules that matter
for safety — no credential in launch config, and no inherited watchdog — observed in the file the
real installer wrote, not asserted from the template. Line 9-10 and the post-run check
(`ls ~/Library/LaunchAgents`, `lsof`) show the machine returned to how it was found.

## 7. Three guards replaced, and the reason stated

`test_no_gate_was_loosened` (Ph 54, Ph 56) and `test_no_existing_gate_was_touched` (Ph 55) each
asserted `sum(gated) == 28`. Adding two legitimately-gated tools turned them red — and their
purpose is not "28", it is "nobody removed a gate". A frozen total is a weak guard with a sharp
edge: it can be satisfied by taking one gate away and adding two somewhere else.

They now assert the invariant instead: every EXTERNAL and every DESTRUCTIVE capability requires
confirmation, and the only ungated PHYSICAL one is the emergency stop, deliberately. Measured
before writing it that way: EXTERNAL 15 gated / 15, DESTRUCTIVE 12 / 12, PHYSICAL 3 with
`emergency_stop` the sole exception.

Negative control, run rather than assumed:

```
loosen delete_file    -> caught: loose=['delete_file']
gate emergency_stop   -> caught: physical_ungated=[] (the assertion expects exactly it ungated)
untouched             -> passes
```

## 8. Browser verification (the panel changed)

Real page, real server, 1440×900, isolated instance:

- the Start-up card rendered `Vantage on this laptop · only while the window is open`, with the
  server's own detail line — the state was read from launchd, not written by the client;
- `document.documentElement.scrollWidth - clientWidth` = **0**, no horizontal overflow;
- console errors: **0**;
- pressing _Install the login item_ produced an approval card reading
  `enable_host_persistence · enable_host_persistence on laptop_main`, and the Start-up card
  **still read "only while the window is open"** — the press did not change the world;
- after 12 s (more than one 8-second poll cycle) the press's note was still on screen (§3.4);
- pressing **Deny** left `~/Library/LaunchAgents` with no Vantage plist and `GET /persistence`
  answering `not_installed / installed: False / loaded: False`.

## 9. What this unblocks

**56-A**, in principle. There is now a process that outlives its window and that launchd starts,
which is the only kind of process macOS will let raise a privacy prompt. The ask itself still runs
in the compiled helper, so the prompt still does not land — moving it is not a Phase 57 change and
is recorded below rather than claimed here.

## 10. Numbers, re-measured

|                    | before                                                       | after                                                            | how                                                                                                                                                                    |
| ------------------ | ------------------------------------------------------------ | ---------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Test suite         | 2218 passed                                                  | **2273 passed · 0 failed · 0 skipped** (71.4 s)                  | `JARVIS_HOME=$(mktemp -d) .venv/bin/python -m pytest -q`                                                                                                               |
| Phase 57 tests     | 0                                                            | **55**                                                           | `tests/test_phase57_the_machine_stays_reachable.py`                                                                                                                    |
| Test modules       | 83                                                           | **84**                                                           | `ls tests/test_*.py \| wc -l`                                                                                                                                          |
| Phase reports      | 58                                                           | **59** (this file)                                               | `ls audit/*.md \| wc -l`                                                                                                                                               |
| Lines              | 69,120 — 41,726 · 27,394                                     | **70,190 — 41,887 source · 28,303 tests**                        | `find jarvis dashboard app -type f \( -name '*.py' -o -name '*.html' -o -name '*.js' -o -name '*.swift' \) -not -path 'app/build/*' \| xargs wc -l`, same for `tests/` |
| Tools              | 142                                                          | **145**                                                          | registry counted live                                                                                                                                                  |
| Risk spread        | READ 76 · LOW 38 · EXTERNAL 13 · DESTRUCTIVE 12 · PHYSICAL 3 | **READ 77 · LOW 38 · EXTERNAL 15 · DESTRUCTIVE 12 · PHYSICAL 3** | registry                                                                                                                                                               |
| Confirmation-gated | 28                                                           | **30**                                                           | registry; §7 asserts no gate was removed                                                                                                                               |
| Planner routes     | 52                                                           | **52**                                                           | three branches inside one existing rule                                                                                                                                |
| HTTP surface       | 118 / 112 paths                                              | **119 / 113 paths**                                              | `len(app.routes)`                                                                                                                                                      |

**Ending commit:** `a20e39c`, then the close-out commit described in §13. Full suite at this close:
**2273 passed · 0 failed · 0 skipped**, timed 66.98 s, 71.43 s, 71.47 s and 74.22 s.

## 11. Open items

- **57-A — the login item has never been left installed for the real app.** It was installed,
  started, observed answering and removed, all against an isolated home and an odd port, because
  leaving it on changes what happens every time this machine logs in. Whether to turn it on for
  real is the user's decision, and the panel control exists to make it: press _Install the login
  item_, approve the card.
- **57-B — "loaded" is not "started successfully".** `launchctl print` reports a _last exit code_
  and `state = not running`; `status()` reads only whether the job exists and its pid. A service
  that crashes at every login would be reported as `loaded`, which is the optimistic-success shape
  this project keeps having to design against. Reading the exit status is the fix.
- **57-C — the port probe and the bind are two moments, not one.** `port_is_free()` closes its
  probe socket before uvicorn binds, so a second process can win the race in between. Handing
  uvicorn a pre-bound socket would close it; not done here because it changes TLS and bind-address
  handling that Phase 36 pinned.
- **57-D — no ceiling on restarts after a genuine crash.** `KeepAlive` retries with launchd's own
  throttle and no maximum, which is the right default for a transient fault and the wrong one for
  a persistent one. There is no failure counter and no "it has failed N times" report.
- **57-E — the switch is laptop-side only.** `/persistence` is a panel card; the phone shows
  nothing about it, so "will the laptop answer when I'm away?" cannot be asked from where it
  matters most. Reaching it from the phone is a design question, not an oversight to fix silently.
- **57-F — no reboot or login was performed.** The proof is that launchd starts and supervises the
  job when asked, on this machine, now. Whether it comes back at login is the same mechanism one
  step further out, and it was not observed.
- **57-G — 56-A stands.** The permission ask still lives in the helper, which macOS will not let
  prompt; a long-running host now exists to move it into, but this phase did not move it.

## 12. Next

Phase 58 — native mobile. Two things this phase changes about its prerequisites: there is now a
process that can outlive the window (so a native shell has something to talk to), and
`READ ME FIRST.txt` still has to be regenerated rather than hand-maintained (56-F), which Phase 58
should fix rather than deepen.

## 13. Redeployed and re-verified at the close

The bundle was rebuilt from `a20e39c` (`codesign verify: ok`), the old copy moved aside, the new one
deployed, relaunched, and the aside copy removed only after the new one answered. Checks on the
delivered app were **GETs only** — the Phase 54 §11a rule — and no POST reached the user's real
data directory.

| check                                                      | result                                                                                                                                                                                                                     |
| ---------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `codesign --verify --deep --strict` on the deployed bundle | valid, satisfies its Designated Requirement                                                                                                                                                                                |
| same after extracting `Vantage.app.zip` into a temp dir    | valid — the archive, not just the folder                                                                                                                                                                                   |
| `GET /health`                                              | `status ok`, **145 tools**, scheduler running with 67 jobs, `llm_enabled: false`                                                                                                                                           |
| `GET /persistence`                                         | `not_installed`, `supported: true`, path `~/Library/LaunchAgents/com.jarvisforge.vantage.plist`, `answered_by: /bin/launchctl print gui/501/com.jarvisforge.vantage` — a state with its subject named, per Phase 50's rule |
| the claim, written by the shipped build itself             | `{"pid": 47936, "host": "0.0.0.0", "port": 8765, …}` with 47936 matching the process lsof reports on 8765                                                                                                                  |
| machine left as found                                      | no Vantage plist in `~/Library/LaunchAgents`, nothing listening on the test ports, `/tmp` aside copies removed                                                                                                             |

`READ ME FIRST.txt` was updated in the same pass (145 capabilities, and a Start-up section that says
what the card reports and what will never appear in the launch config) — and 56-F still stands: that
file is hand-maintained in the delivery folder rather than generated from the repo.
