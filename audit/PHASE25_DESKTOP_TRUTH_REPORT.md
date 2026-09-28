# Phase 25 — The desktop app that "exits 1", measured

Report date: 2026-09-28. Continues Phases 9-24.

No brief. The scope came from the one item in the backlog that claimed something was
outright broken: **"desktop bundle exits 1"**, carried verbatim through Phase 11, 12, 13,
14, 15, 16… from the Phase 9/10 finding, and repeated in `README.md` as
`Packaged desktop app | BROKEN — exits 1`.

---

## 1. The claim was stale

Built and launched a fresh bundle:

```
$ python build_app.py
Built: .../dist/Jarvis Forge.app          codesign verify: ok

$ JARVIS_HOME=$(mktemp -d) "dist/Jarvis Forge.app/Contents/MacOS/JarvisForge" &
$ curl -s -H "X-JARVIS-Token: $TOK" -H 'Origin: http://127.0.0.1:8765' http://127.0.0.1:8765/health
HTTP=200 time=0.012448s
```

Two processes, as designed: the bundle's `launcher.py` (the control panel) and the
`run.py` server it spawns. And the self-test ran:

```
JARVIS_SELFTEST=1 "dist/Jarvis Forge.app/Contents/MacOS/JarvisForge"
  [ok  ] project directory        /Users/abhijeetmiskin/AppData/jarv/jarvis-forge
  [ok  ] python interpreter       .../envs/default/bin/python
  [ok  ] tkinter / Tk             Tk 9.0.3
  [ok  ] control panel widgets    4 widgets constructed
  [ok  ] server                   127 tools, agents [...], model=rules
  [ok  ] text to speech           macos_say
  [ok  ] speech to text           macos_speech, transcript
  [FAIL] microphone               blocked by macOS permission — ...
  [FAIL] speech recognition       macOS Speech blocked this process
  7/9 checks passed
```

So the two remaining failures are macOS not having granted permissions on this machine —
an environment fact, not a defect. **The "exits 1" line had been false for some time.**
Phase 9's P0-D1 (a `project_path.txt` baked against the pre-relocation tree) had been
fixed by rebuilding, and the ancestor walk had grown a level. The README kept repeating a
finding nobody had re-checked in fifteen phases. That is the same failure this project
exists to catch, pointed at the audit trail itself: a claim about health, unobserved.

## 2. What was genuinely still broken

Measuring the stale claim surfaced two live ones, both of a kind that a *successful* run
hides.

### 2.1 P0-D3 — the self-test could not run on the install that needs it

`main()` resolved the project directory **before** the `JARVIS_SELFTEST` branch:

```python
try:
    project_dir = resolve_project_dir()
except RuntimeError as exc:
    _fatal(str(exc))          # native alert, then:
    return 1                  # ... the self-test below is never reached
```

So the nine checks that exist to explain a broken app were gated behind the one step a
broken app fails first. My own successful run above could not see this, because
resolution succeeded — the defect only appears in exactly the situation the tool is for.

Fixed by moving the self-test ahead of that gate: it now builds a controller, carries the
resolution error as `project_dir_error`, and never starts a server when there is no
project to start it from. Measured on a deliberately broken copy of the built bundle:

```
[FAIL] project directory   Could not locate the jarvis-forge project directory.
                           Set JARVIS_PROJECT_DIR or rebuild the app with build_app.py.
[ok  ] python interpreter   ...
[ok  ] tkinter / Tk         Tk 9.0.3
[ok  ] control panel widgets 4 widgets constructed
[FAIL] server              not started: Could not locate the jarvis-forge project directory. ...
3/5 checks passed
not attempted: text to speech, speech to text, microphone
```

Two details worth the care:

- **The row carries the reason**, not just the path the user already knows is wrong.
- **`not attempted:` is printed.** Without it, "3/5 checks passed" reads like a smaller
  suite ran successfully. The denominator shrank because there was no server to ask, and
  the report says so instead of implying three quiet passes and two honest failures.
  Marking those four rows `FAIL` would have been its own lie — the app does not know
  whether the microphone works if it never got far enough to ask.
- A plain launch (no `JARVIS_SELFTEST`) still refuses, still alerts, and is covered by a
  test asserting that the fix did not turn a broken install into a silent start.

### 2.2 The ancestor walk stopped one level short

`resolve_project_dir()` looked at `BUNDLE_ROOT`, its parent and its grandparent.
`BUNDLE_ROOT` is `<checkout>/dist/Jarvis Forge.app/Contents`, so that reach is
`Contents → .app → dist` — one level short of the checkout, which meant a build sitting
inside the very tree it came from still depended entirely on the pointer stamped at build
time. Relocating the checkout would break an app that had never moved. Now walks the root
plus three ancestors, pinned by tests for both layouts (an `/Applications` copy, which
*must* use the recorded pointer, and a `dist/` copy, which must not need it).

### 2.3 P1-D1 — the installer promised a prompt the app never shows

`install_app.sh` ended with:

> First launch will ask for Microphone and Speech Recognition permission.

Nothing in the tree calls `AVCaptureDevice.requestAccess`. The helper's own comment says
why: it deliberately *queries* consent, because a CLI helper requesting it "either never
answers or aborts the process" — and `install_app.sh` told every new user to wait for a
dialog that never comes, then to grant access to an app that never asked. The self-test
measures `denied`/`blocked` with no prompt in sight.

The wording now matches the mechanism, and gives the one command that reports the truth:

```
The app does not ask for microphone or speech-recognition access on launch --
it reports whatever macOS already granted. If voice input shows blocked:
  1. System Settings -> Privacy & Security -> Microphone -> enable Jarvis Forge.app
  2. quit and relaunch the app
  3. check what it can actually see:
       JARVIS_SELFTEST=1 "/Applications/Jarvis Forge.app/Contents/MacOS/JarvisForge"
```

A test pins this from both ends: the installer must not promise the prompt, and the Swift
helper must still only query. If someone later wires up a real request, that test fails
**on purpose**, so the promise and the mechanism have to move together.

## 3. What was *not* done

The app could be made to actually ask for microphone access — the bundle already declares
`NSMicrophoneUsageDescription`, and macOS shows prompts from apps with a bundle identity.
That was not attempted here, because the claim it would fix is a *wording* claim, the
current behaviour reports itself honestly, and a change to how TCC prompts are raised on
someone's machine is not a documentation fix dressed as one. Left as an open item, with
the reason.

Publishing a prebuilt binary is likewise still out: the bundle is ad-hoc signed, which is
fine for the machine that built it and not for distribution (Apple Developer identity,
notarization, a stable bundle id for TCC). The README's *Downloads* section now says that
instead of the stale "it doesn't work".

### 2.4 An undeclared build dependency, found by trying to put the build in CI

The honest next step after "the bundle actually works" is to keep it working in CI. That
attempt immediately raised the question of what the build *needs*: `build_app.py` shells
out to `app/make_icon.py`, which does `from PIL import Image`, and the build **fails hard**
if that subprocess returns non-zero. Pillow was in neither `requirements.txt` nor
`pyproject.toml`. So `python build_app.py` — the command the README tells a reader to run —
only worked on machines that happened to already have Pillow.

Fixed by declaring `pillow>=10.0` and adding a CI step that builds the bundle into a temp
directory, so a clean checkout that cannot build it is a red pipeline rather than a
sentence in a README. Two tests pin the pair: the builder must still fail loudly rather
than swallow a missing icon, and `requirements.txt` must name what `make_icon.py` imports.

## 4. Regressions

`tests/test_phase25_desktop_truth.py` — **17 tests**, 15 written alongside the fix and 3 red first
(the self-test ordering, the reason-in-the-row, the not-waited-on controller); two more
were added as the fix exposed them, and three of *my own* assertions were wrong and
corrected rather than the code bent to them:

- `BUNDLE_ROOT` is `…/Contents`, not the `.app` — my fake bundle layout was one level off,
  which made a resolution test pass for the wrong reason.
- `mic_state.swift` mentions `requestAccess` **in a comment** explaining the omission, so
  a naive string search for it failed on correct code; the test now strips comments and
  looks for a call site.
- A test asserting "3/5 checks passed" would have been a false comfort; it became the
  `not attempted:` requirement in §2.1.

Suite: 1343 → **1360 collected, 1359 passed, 0 failed, 1 skipped, 0 xfailed**, identical across two runs. The
`1 skipped` is still `test_research.py` refusing to pretend arXiv was reachable.

One guard earned its keep during this phase:
`test_bundled_launcher_has_not_drifted_from_source` (written in Phase 10 because the built
bundle once shipped a microphone line the source no longer had) went red the moment
`app/launcher.py` changed, and `build_app.py` had to be re-run so `dist/` matches the
source again. The bundle that is measured is the bundle that ships.

## 5. Everything else still stands

No product behaviour outside `app/launcher.py` and `install_app.sh` changed. The 17-command
acceptance sweep produced the same terminal states as Phases 21-24 (7 VERIFIED / 4
UNAVAILABLE / 2 NOTHING EXECUTED / 2 NEEDS DETAIL / 1 FAILED / 1 NOT RECOGNISED), and the
safety matrix is still **5 of 6** with `turn on heater` printed as `FAIL`.

## 6. Carried forward

Closed here: the stale **desktop bundle "exits 1"** claim, **P0-D3** (self-test
unreachable on a broken install), the one-level-short ancestor walk, **P1-D1**'s false
promise in the installer text, and **25-F** — the build's undeclared Pillow dependency, now
declared and built on every push.

Still open: microphone and speech-recognition consent, which only the machine's owner can
grant — and the related choice about whether the app should *ask* rather than report ·
**22-G** thermal appliances need no confirmation when uniquely resolved · **23-E** the
planner's `or "amr_main"` robot default · **15-F** arXiv's client refusal · **14-D** a
partly-routable sentence answering one half without flagging the other · no `read_file`
route · no notification tool · no `ProjectGraph` delete API · **15-H** which calendar
store was read · hardware **BLOCKED** · LLM planner · multi-step unattended workflows ·
16-F's security-event surface.

## 7. Git and browser

Committed with this phase: `app/launcher.py`, `install_app.sh`, `requirements.txt`,
`.github/workflows/ci.yml`,
`tests/test_phase25_desktop_truth.py`, `benchmarks/*` (including the phase-25 history
point), `assets/*.svg`, `README.md`, this report. Pushed to `origin/main` as a
fast-forward; nothing force-pushed. CI is recorded after it runs, not before.

**LIVE BROWSER DRIVE: NOT PERFORMED.** The desktop evidence in §1-§2 is a real built
bundle launched as a subprocess and reached over HTTP, plus the bundle's own self-test
output — but the dashboard page was not opened in a browser, and the control-panel window
was never clicked. "Tk 9.0.3 / 4 widgets constructed" is what the shipped self-test probes,
not what a user saw on screen.

Scratch bundles were built into temp directories and removed; `dist/` was rebuilt so the
drift guard compares like with like; `~/.jarvis-forge` never written.
