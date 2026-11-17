# Vantage.app as a standalone bundle

What is inside `dist/Vantage.app`, how it finds its own files, how to run it, and which
parts of "standalone" are verified by a test and which are not.

Measured 2026-11-17 against `dist/Vantage.app` (41 MB, ad-hoc signed
`com.jarvisforge.app`). The acceptance harness that produced the numbers is
`benchmarks/v22_standalone/standalone_accept.py`.

## Build it

```bash
.venv/bin/python build_app.py          # -> dist/Vantage.app
.venv/bin/python build_app.py --install  # also copies to /Applications
```

The build refuses to finish if `codesign` fails. That is deliberate: without a stable code
identity macOS cannot persist a permission grant, so an unsigned bundle would "work" until
the first prompt and then silently lose what it was given.

## What the bundle contains

| Path | What it is |
| --- | --- |
| `Contents/MacOS/JarvisForge` | Swift/AppKit launcher, Mach-O arm64, the bundle's `CFBundleExecutable` |
| `Contents/Resources/launcher.py` | Python launcher: starts the server, shows the control window, opens the dashboard |
| `Contents/Resources/app/` | The staged backend: `jarvis/`, `dashboard/`, `scripts/`, `run.py`, requirements |
| `Contents/Resources/app/native/desktop_executor/vantage-desktop` | The compiled native desktop executor |
| `Contents/Resources/site-packages/` | The Python dependencies, copied from the build venv (89 entries) |
| `Contents/Resources/python_path.txt` | The interpreter the build ran under |
| `Contents/Resources/site_packages_path.txt` | Where the packages were staged |
| `Contents/Resources/project_path.txt` | The checkout at build time — a fallback, not the primary source |
| `Contents/Info.plist` | Bundle id, version, and the microphone/speech/Apple Events usage strings |

`tests/`, `.git/`, `.venv/`, `dist/`, `build/` and `__pycache__` are deliberately not
staged. Build tools and the browser-automation package (`playwright`, ~161 MB) are also
excluded from `site-packages`.

## How it resolves resources

Resolution is **relative to the bundle**, never from a recorded absolute path, which is
what makes a copy work:

1. **Launcher.** `Contents/MacOS/JarvisForge` locates an interpreter and `execv`s it on
   `Contents/Resources/launcher.py`. Because it is a Mach-O binary inside the signed
   bundle, macOS attributes what it spawns to `com.jarvisforge.app`. A shell script as
   `CFBundleExecutable` could not hold that identity — that was measured, and it is why
   the launcher is compiled.
2. **Dependencies.** The launcher sets `PYTHONPATH` to
   `<bundle>/Contents/Resources/site-packages` and `PYTHONNOUSERSITE=1`, so the staged
   packages win over anything on the machine. This path is computed from the launcher's
   own location; an earlier build recorded the absolute path and a copy then imported
   packages from the bundle it was cloned from.
3. **Backend source.** `launcher.py` prefers `Contents/Resources/app` when it contains
   `run.py` and `jarvis/`. `project_path.txt` and a search of the bundle's ancestors are
   fallbacks.
4. **Native executor.** The backend asks `jarvis.desktop.executor.executor_path()`, which
   searches from the `jarvis` package's own location: inside the bundle that resolves to
   `Contents/Resources/app/native/desktop_executor/vantage-desktop`. A dev checkout uses
   the same rule and finds its in-repo binary.
5. **Data.** State lives outside the bundle, under `JARVIS_HOME` (default
   `~/.jarvis-forge`): database, `api_token`, logs, and the compiled microphone helper.

## Run it

```bash
open dist/Vantage.app
# or, without a control window:
JARVIS_HEADLESS=1 dist/Vantage.app/Contents/MacOS/JarvisForge
```

Useful environment variables: `JARVIS_HOME` (data directory), `JARVIS_PORT` /
`JARVIS_HOST` (bind address), `JARVIS_HEADLESS=1` (no window), `JARVIS_SELFTEST=1`
(run the built-in checks and exit).

**Do not point a test build at port 8765.** That is the installed app's backend; taking it
breaks the user's running session. `scripts/dev_port_guard.sh claim` returns a free port in
8798–8810 and `check <port>` reports who holds a busy one.

## What it needs from macOS

The bundle is **ad-hoc signed** (`codesign --sign -`), not Developer ID signed and not
notarized. First launch from a copied location may need right-click → Open, or
`xattr -d com.apple.quarantine`.

Desktop control additionally needs, granted separately in System Settings → Privacy &
Security:

- **Accessibility** — reading UI elements, and pressing elements.
- **Post-event access** (Accessibility the same entry covers it) — clicking and typing.
- **Screen recording** — window list, screen capture, and resolving click targets.

The grant attaches to a **code identity**, not a path. Because the bundle is ad-hoc
signed, its identity (CDHash) is a function of its contents: a byte-identical copy carries
the same identity and inherits the same answers. Rebuild the bundle and the contents
change, so the CDHash changes and the old grant may no longer apply. That is why the
executor reports its own identity (`GET /desktop/identity`) alongside the permissions.

## What is verified, and what is not

**Verified** by `benchmarks/v22_standalone/standalone_accept.py` (last run 2026-11-17),
which copies the bundle to `/tmp`, launches the copy with an isolated `JARVIS_HOME` on a
guarded port, and inspects the result:

- The copy satisfies its own signature and keeps `com.jarvisforge.app`.
- The copy answers `GET /health` (1.6 s cold start in that run) with all six steps' evidence
  recorded.
- The listening backend's working directory is
  `<copy>/Contents/Resources/app`, and its reported `source_root` is that same staged
  directory — it does not serve from the checkout.
- `GET /desktop` reports the executor at
  `<copy>/Contents/Resources/app/native/desktop_executor/vantage-desktop`.
- The copy runs on Homebrew Python 3.11 with `PYTHONPATH` pointing at its **own** staged
  `site-packages` (read from the live process's environment).

**Verified separately, by hand:**

- A copy with `python_path.txt` pointed at a different Python 3.11 (`/opt/homebrew/bin/python3.11`)
  starts and serves from inside its own bundle. The staged packages are
  `cpython-311` binaries, so the **interpreter major/minor is a real requirement**.

**Not verified — do not claim these:**

- **Desktop input from a fresh identity.** This harness cannot grant macOS permissions. In
  the recorded run `click`, `type`, `read_ui` and `screen_capture` all reported true,
  but that is because the copy's ad-hoc CDHash is identical to the original's, so macOS
  answered for the same identity. It does **not** show that a newly built or
  differently-signed copy would be granted anything. Treat desktop input on a fresh bundle
  as untested.
- **A machine without Python 3.11.** The interpreter is not bundled — only the packages
  are. The staged extensions are `cpython-311` binaries
  (`pydantic_core/_pydantic_core.cpython-311-darwin.so`), so the interpreter's major/minor
  is a hard requirement. Measured: pointed at Python 3.15 the copy never answered
  `/health`, and its log showed `cannot import name 'ArgsKwargs' from
  'pydantic_core._pydantic_core'`. Pointed at a plain `/opt/homebrew/bin/python3.11` it
  started and served normally. Packaging a relocatable CPython framework is a larger job
  and is not done.
- **Truly clean shutdown.** The harness fails step 6 on the current build: the launcher's
  stop path raises `NameError: name 'log' is not defined` (`app/launcher.py` uses `log`
  in five places and never defines or imports it), so it exits 1 without terminating the
  backend. The backend still dies, and the port is still released, because `run.py`'s
  guardian thread notices the parent is gone and exits. Measured in a separate probe: the
  backend was still alive immediately after the launcher received SIGTERM (the launcher
  never reached its own `terminate()`), and the port was free a few seconds later. That is
  the safety net working, not the shutdown path working.
- **The control window.** The build venv has no `_tkinter`, so the launcher always falls
  back to headless on this machine. The Tk path has never run here.

## Known defects surfaced by this work

Both are in `app/launcher.py`, at `HEAD`, and are present in the built bundle.

1. **`log` is undefined.** `stop()` calls `log.info(...)` before the `try` and
   `log.warning(...)` in the handlers; `wait_ready()` calls `log.error(...)`. The module
   never imports `logging` or defines `log`. Any `stop()` reaches `log.info` first (a
   started controller always has `self.runtime`), raises `NameError`, and exits 1 without
   terminating the backend. This affects the GUI Quit button, Cmd-Q, and SIGTERM alike.
2. **`SUP` does not exist.** `build_supervisor()` imports
   `from jarvis.runtime.supervisor import RuntimeSupervisor, SUP`, but the module exports
   `SUPER`, not `SUP`. The import fails, the launcher prints
   `Warning: Could not initialize runtime supervisor: ...`, and the supervisor is never
   used.

Neither is fixed here: this work is scoped to the acceptance harness and this document.
The harness reports defect 1 as a **FAIL** on step 6, which is the honest result.

## Running the harness

```bash
python3 benchmarks/v22_standalone/standalone_accept.py
python3 benchmarks/v22_standalone/standalone_accept.py --keep     # leave the copy behind
```

Exit status is 0 only when the copy, launch, cwd, executor and shutdown steps pass. The
permission report never affects it. Every wait has a deadline, only pids the harness
started are ever signalled, it never binds 8765, and it removes the copy and the isolated
home unless `--keep` is given. Standard library only.