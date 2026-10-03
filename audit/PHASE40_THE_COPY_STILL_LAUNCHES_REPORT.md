# Phase 40 — the application, as files, that still work when you copy them

## 1. What was asked

> no need of push on github in folder make applicatins files so i can downlode them

Two instructions: stop publishing, and hand over the programs as files. The deliverable for
this phase is therefore a folder, not a commit: `~/Downloads/Vantage/`.

Before that, one correction to record. I read the message above as silence and started the
next item from the plan (a stable hostname for the phone). Nothing was written for it, and
the phase was dropped when the actual request was clear. One measurement survived from it and
is carried forward as **40-A**, because it is a real finding rather than an abandoned draft:
the laptop refuses its own Bonjour name — `Host: abhijeets-macbook-air-2.local:8899` is
**400**, the same verdict the gate gives `evil.example.com`, which is why 38-B ("nothing
advertisesthe laptop by name yet") is not only a missing feature but a gate that would reject
the fix as currently written.

## 2. Measured first: the copy I made at 10:10 was already broken

`ditto` of `dist/Vantage.app` into the Downloads folder launched, served `/phone`, answered a
command — and failed its own integrity check:

```
codesign --verify ~/Downloads/Vantage/Vantage.app
  → Vantage.app: a sealed resource is missing or invalid
codesign -dvvv …  → Sealed Resources version=2 rules=13 files=145
find … -name '*.pyc' | wc -l  → 127
```

145 files were sealed at build time; 127 more appeared afterwards, all of them compiled
Python, all of them inside the bundle. The build *stages* no bytecode (`STAGED_SKIP` holds
`__pycache__`, the copy filter drops `.pyc`), so the bundle was clean when it was signed and
got dirty by being **used**: the server the panel spawns imports the staged source and writes
`__pycache__` next to it. `PYTHONDONTWRITEBYTECODE=1` in the spawn environment is the whole
fix — assigned, not defaulted, for the same reason `PYTHONUNBUFFERED` is.

Why it matters rather than being cosmetic: an ad-hoc-signed, un-notarised app is already the
one Gatekeeper hesitates over, and *"Vantage is damaged"* is what an invalid seal turns that
hesitation into. A copy of the app must survive being copied *and* being run.

## 3. Then the app orphaned a server on the wifi address

The delivered app was quit with `tell application "Vantage" to quit`. The panel went away;
the server did not:

```
ps -o pid,ppid -p <server>       PPID 1        # adopted by launchd
lsof -iTCP:8765 -sTCP:LISTEN     1 process     # still bound
curl http://172.20.169.185:8765/health          200   # still answering the wifi
```

Nothing on screen could stop it any more, and LAN mode had been on, so an HTTP server that can
change the machine was reachable from the network with only a token in front of it.

**The first fix did not work, and saying so is the point.** `SIGTERM`/`SIGINT` handlers were
installed on the panel, the bundle was rebuilt and re-delivered, and the same quit produced
the same orphan. Of course it did: Tk does not die by signal. Its quit path invokes Tcl's
`exit` command, which leaves the interpreter from C — no Python signal handler runs, no
`atexit` callable runs, and `controller.stop()` was only ever wired to the Quit button.

So the guarantee is made twice, because the two halves fail differently:

* `root.createcommand("exit", …)` — the panel's own exit is the cleanup, whatever invoked it.
  The Quit button and the window close keep doing what they did.
* `jarvis/process.py` — the server is told its parent's pid (`JARVIS_GUARDIAN_PID`) and
  watches it. `guardian_gone()` checks reparenting first, because reparenting is the shape
  actually observed. This is the half that covers the deaths no in-process hook can: SIGKILL,
  a crash, a logout.

```
osascript quit   → panel 0, server 0, listening 0, curl: connection refused   (immediate)
kill -9 <panel>  → panel 0, server 0, listening 0                              (within ~6 s)
```

The bundle-drift guard (`test_bundled_launcher_has_not_drifted_from_source`) went red during
this work for the reason it exists: I edited `launcher.py` and `run.py` and left the built app
pointing at the old ones. Full suite after rebuilding: green.

## 4. What is in the folder

```
~/Downloads/Vantage/
  Vantage.app          4.0 MB  the laptop application, seal valid, 0 .pyc inside
  Vantage.app.zip      1.0 MB  the same thing, compressed (a real zip this time)
  vantage-source.zip   1.2 MB  the source tree (also a real zip; the 10:10 copy of this
                               file was a gzip tarball wearing a .zip name)
  READ ME FIRST.txt            laptop, phone, what to do when the address changes
```

Verified on the delivered copy rather than on the build output:

```
codesign --verify                    valid on disk / satisfies its Designated Requirement
JARVIS_SELFTEST=1 …JarvisForge       7/9 checks (the two failures are macOS microphone and
                                     speech permissions, not code)
/command "what is my battery"        "Your laptop battery is at 100% (fully charged)."
GET /health over 172.20.169.185      200
GET /phone over 127.0.0.1            200
.pycount after three launches        0        seal after use: still valid
```

The instructions file states the checkbox by the label it actually carries (*"Allow devices
on my wifi to connect"*) and the button by the text the page renders (*"Pair with this
laptop"*), because the first draft of it invented both.

## 5. Regressions and numbers

```
new tests        6 (the copy still launches) + 5 (the panel dies, the server dies) = 11
full suite       1786 passed · 0 failed · 0 xfailed · 0 skipped
safety matrix    6 of 6
tools            129 (read 69 · low 32 · external 13 · high 12 · physical 3)   unchanged
planner routes   42                                                            unchanged
API routes       108 handlers / 106 distinct paths                             unchanged
test modules     66          reports 42
size             60,667 lines of versioned Python/HTML/JS (38,472 source · 22,195 tests)
```

Nothing published: this phase adds no tool, no route and no risk tier. The count that moved
is tests, and the thing that changed is whether the artifact a user can hold is trustworthy.

## 6. Carried forward

* **40-A** the laptop has no address that survives a DHCP lease. Its own Bonjour name is
  refused by the `Host` gate — measured 400, identical to the forged-hostname case — so the
  fix is not just advertising `…local`, it is teaching the gate to accept exactly one
  machine-owned name without reopening DNS rebinding. 38-B and 39-E are the same defect.
* **40-B** `.local` does not resolve on Android browsers, so even a correct name would help
  iPhones and desktop machines first. Not measured — no handset was reachable from here.
* **40-C** the guardian's exit is `os._exit(0)`, chosen because a thread cannot ask uvicorn
  for a graceful shutdown. In-flight requests are dropped and the WAL is left to SQLite;
  a stop that drains would need the signal path the panel already has.
* **40-D** `codesign --verify` is checked by hand, not in the suite: `dist/` is gitignored,
  so the CI-side guard remains the one 29-D recorded as never running.
* **40-E** the account cannot start runner jobs (39-F), and the user has now asked for no
  pushes regardless. Every number above is measured on this machine.
* Unchanged: 39-A…E, 38-A…D, 37-A…E, 36-A…E, 35-A…C, 34-A…C, and the `-rs` CI change.

## 7. Git

Committed on `main`, **not pushed** — the request was explicit about that, and the runner
could not have checked the work anyway (39-F). `git log` will show this phase's commit ahead
of `origin/main`; pushing it is a separate decision.
