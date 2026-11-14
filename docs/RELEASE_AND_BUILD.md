# VANTAGE — Build, Run & Release Instructions

Reproducible build/run/release instructions for each artifact. Every command here is
grounded in the actual build scripts and a verified baseline; known limitations are stated
explicitly (this is not aspirational). The completion matrix and engineering log carry the
full evidence trail.

> Verified baseline (every command in this document was run on 2026-10-21, and
> its output is what the claims below are based on): branch `main`; Python suite
> **3022 passed / 18 skipped / 0 failed**; macOS bundle `com.jarvisforge.vantage`
> signature **VALID**; Android APK builds and promotes to `dist/` (JDK 17).
> See `PROJECT_COMPLETION_MATRIX.md`.

---

## 1. Python backend (the core engine)

**Requirements:** Python 3.11 (a `.venv` is provided), `requirements.txt`.

```bash
cd <repo>
# Dependencies. The venv is uv-managed and has NO pip in it, so
# `python -m pip install` cannot work here -- use uv (it installs into the
# tree's .venv and reports what it changed):
uv pip install --python .venv/bin/python -r requirements.txt

# run the backend + dashboard on the default port (8765)
.venv/bin/python run.py serve
```

**The venv.** It is uv-managed, not pip-managed: `.venv/bin/python -m pip` fails with
"No module named pip". To rebuild it from scratch, create it with `python3 -m venv
.venv` and then install with the `uv pip install --python .venv/bin/python` form
above — a `pip install` against it will not work.

**Port/host are configurable** (`run.py` maps the flags to env vars; a bad value exits
non-zero rather than silently falling back to 8765):

```bash
.venv/bin/python run.py serve --port 8899 --host 127.0.0.1
# or via environment:
JARVIS_PORT=8899 JARVIS_HOST=127.0.0.1 .venv/bin/python run.py serve
```

The backend refuses to start if the port is already bound by another process (it prints the
holder's PID and exits 1) so a stale process cannot masquerade as the one serving.

**One backend per data directory, not per port.** The single-instance claim is held
against `JARVIS_HOME`, so a second instance on a _different_ port is refused too:
starting one while another runs logs "second instance refused" and exits. To run a
second one deliberately, give it its own home:

```bash
JARVIS_HOME=/tmp/vantage-alt .venv/bin/python run.py serve --port 8899 --host 127.0.0.1
```

**Run the test suite:**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider        # full suite
.venv/bin/python -m pytest tests/<file>.py -q            # a single file while iterating
```

| Test suite | **3736 passed · 0 failed · 21 skipped (566.75 s, measured 2026-11-13)** |

The skips are legitimate environment-gated tests — macOS-only desktop control,
the Playwright browser, live permissions, a physical device — not hidden
failures. Each one names its prerequisite; `pytest -rs` lists them.

**Configuration / data:** runtime data lives in `~/.jarvis-forge` (outside the tree). The
master API token is written to `~/.jarvis-forge/api_token` on first start and is required
as the `X-JARVIS-Token` header on protected routes. Provider keys are stored in the OS
keychain, never in the repo.

**Browser automation (optional).** The browser tools need Playwright _and_ its
browser build. The library alone is not enough — `browser_open` fails with
"Executable doesn't exist" until the browser is downloaded:

```bash
uv pip install --python .venv/bin/python playwright
.venv/bin/python -m playwright install chromium
```

With that done, the browser tools work: `browser_open` reads a page's text,
`browser_save` writes it to a file, and the browser's suite stops skipping
(28 skipped → 27 passed, 1 skipped as of 2026-10-21).

---

## 2. macOS native app (SwiftUI, bundle `com.jarvisforge.vantage`)

**Requirements:** Xcode (or Xcode command-line tools with the macOS SDK), Swift 5.9+.

```bash
cd macos/Vantage
make app          # swift build -c release + assemble_app.sh release -> macos/Vantage/Vantage.app
```

`assemble_app.sh` **fails loud** if code signing or signature verification fails (a bad
bundle is never reported as built). Verify the signature yourself:

```bash
codesign --verify --strict macos/Vantage/Vantage.app && echo "signature OK"
```

Run the built app:

```bash
open macos/Vantage/Vantage.app
```

It reads the master token from `~/.jarvis-forge/api_token` and talks to the backend on
`127.0.0.1:8765` by default (start the backend first).

**This is what the DMG ships.** The default release artifact is this native app
(`com.jarvisforge.vantage`). The Tk launcher remains buildable as an explicit fallback
(`build_dmg.sh --launcher`) for the TCC/permission path. See §4.

**Installing over an existing copy — use the script, not a manual `rm`:**

```bash
./installers/macos/install_app.sh            # from macos/Vantage/Vantage.app
./installers/macos/install_app.sh path/to/Vantage.app   # or any built bundle
```

It prints the resolved source and destination, refuses a source that is not the
expected bundle, is unsigned, or has no executable, copies the installed app to a
timestamped `/Applications/Vantage.app.backup-*` first, stages and swaps, then
verifies the replacement's signature and bundle id — restoring the backup on any
failure. A hand-run `rm -rf /Applications/Vantage.app` beside a `cp` once ran from
the wrong working directory and deleted the app with nothing to replace it; this
script exists so that path is not available.

---

## 3. Android companion (`com.vantage.companion`)

**Requirements:** **JDK 17–21** (AGP 8.7 does **not** run on JDK 22+; the machine's default
JDK 26 fails — this is a hard requirement, not a preference), an Android SDK, and Gradle 8.x.

```bash
cd android
# point at a JDK 17 and the Android SDK:
export JAVA_HOME=/opt/homebrew/opt/openjdk@17        # or any JDK 17–21
export ANDROID_HOME="$HOME/Library/Android/sdk"       # or ANDROID_SDK_ROOT

# unit tests (46 tests; no device needed):
./gradlew testDebugUnitTest

# build the APK and promote it to dist/ (this is the script that also
# checks the JDK/SDK and verifies the package identity with aapt2):
../android/build_apk.sh

# or drive Gradle directly -- note this leaves the APK in the Gradle output
# directory, NOT in dist/:
./gradlew :app:assembleDebug          # -> android/app/build/outputs/apk/debug/app-debug.apk
./gradlew :app:assembleRelease        # release build
```

**`android/build_apk.sh` is the script that puts the APK in `dist/`.** A bare
`./gradlew :app:assembleDebug` does not: it writes
`android/app/build/outputs/apk/debug/app-debug.apk` and leaves it there. The
script copies it to `dist/Vantage-<version>-android-debug.apk` and writes
`dist/ANDROID_RELEASE_MANIFEST.json`.

**Verified (2026-10-21):** unit tests pass (46, 0 failures); `:app:assembleDebug`
builds successfully on JDK 17; and `android/build_apk.sh` promotes the APK to
`dist/Vantage-1.0-android-debug.apk` and writes the manifest. **Not verified
here:** install/launch on a real device or emulator (no emulator/device in this
environment) — the _contract_ (endpoints + `X-JARVIS-Token` auth) is verified by
source comparison against the backend, but live on-device behavior is not.

---

## 4. Release artifacts (per platform)

| Artifact                 | Command                               | Output                                                                | Status                                          |
| ------------------------ | ------------------------------------- | --------------------------------------------------------------------- | ----------------------------------------------- |
| macOS Tk-launcher bundle | `python3 build_app.py`                | `dist/Vantage.app` (`com.jarvisforge.app`)                            | build verified (iter 18)                        |
| macOS DMG (default)      | `installers/macos/build_dmg.sh`       | `dist/Vantage-<ver>.dmg` — **native app** (`com.jarvisforge.vantage`) | built + signature verified 2026-10-27           |
| Android release APK      | `installers/build_release.sh android` | `dist/Vantage-<ver>-android-release.apk` (`com.vantage.companion`)    | builds (JDK 17)                                 |
| Android debug APK        | `android/build_apk.sh`                | `dist/Vantage-1.0-android-debug.apk` (`com.vantage.companion`)        | build verified 2026-10-21 (promotes to `dist/`) |

**Decision (made 2026-10-27): the DMG ships the native app.** The default was the
Tk launcher, and that bundle contains no SwiftUI screens — so a person who built the app the
documented way saw an older interface than the one in `Sources/`, and the Settings voice
control plus the redesigned screens never reached the running app. The native app
(`com.jarvisforge.vantage`) is the product; the launcher is kept only as an explicit fallback
for the TCC identity path.

```bash
./installers/macos/build_dmg.sh             # default: the NATIVE app
./installers/macos/build_dmg.sh --launcher  # Tk launcher (fallback, unchanged)
./installers/macos/build_dmg.sh --native    # explicit; same as the default
```

Measured 2026-10-27: default → `com.jarvisforge.vantage`, binary contains the current
screens, DMG ad-hoc signed and verified (1.7 MB). The build prints the bundle id it
shipped, so a DMG cannot be mistaken for the other app. Pinned in
`tests/test_r106_dmg_ships_either_artifact.py`.

---

## 5. Known limitations (honest, not hidden)

- **iOS is paused** per the project CLAUDE.md — the `ios/VantageAI` tree exists but is not
  built or verified in this environment by design.
- **Attachment processing is real**, not a placeholder: an image is measured _and_ its
  text is extracted with the macOS Vision OCR backend (`jarvis/vision/ocr.py`), a PDF's
  text is extracted with pypdf (`jarvis/attachments/pdf.py`), and an audio file is
  transcribed through the configured STT backend. Each reports a truthful status —
  `processed`, `error` with the reason, or `not_processed` when no real backend is
  configured — and a page that tries to issue instructions is flagged rather than
  obeyed. Verified 2026-10-21 (engineering log iterations 57-58).
- **R3.3 GUI error-state fix is in the running app.** The app was relaunched on
  2026-10-21 after the fix, and the per-section error+retry states are in the process
  the user is looking at. **Correcting an earlier claim (engineering log iteration 40):**
  a backend restart does **not** pick up the supervisor DEGRADED-state fix — the live
  backend is the `run.py` subprocess, which does **not** use the `RuntimeSupervisor` at
  all. That fix is unit-tested but lives in a dead parallel path (the supervisor/menubar
  is not wired into the current build) — an owner decision, not a restart. See the
  completion matrix.
- **Live model inference** is not exercised here (needs owner OK + a provider credential);
  it is unit/integration-tested with deterministic mocks.
- **Android device/emulator runtime** is not verified here (no device/emulator); unit tests
  - build + contract are verified.

---

## 6. Troubleshooting

| Symptom                                               | Cause / fix                                                                                                                       |
| ----------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| `run.py serve` exits 1, "port already in use (pid N)" | A stale backend is bound. `lsof -ti :<port> \| xargs kill` (confirm it's yours first), then re-run.                               |
| `--port 8899` started on 8765                         | A non-numeric/unknown flag is refused loudly; check the exact flag (`--port` / `JARVIS_PORT`).                                    |
| Android build fails on JDK 22+                        | Use JDK 17–21 (`export JAVA_HOME=.../openjdk@17`). AGP 8.7 cannot run on JDK 22+.                                                 |
| macOS app can't reach backend                         | Start the backend first (`.venv/bin/python run.py serve`); the app reads `~/.jarvis-forge/api_token`.                             |
| Test suite has skips                                  | Expected — macOS-only / playwright / live-permission tests skip when that hardware or dependency is absent.                       |
| Browser tool says the browser does not exist          | The Playwright _library_ is installed but its browser is not: `.venv/bin/python -m playwright install chromium`.                  |
| `python -m pip install` fails in the venv             | The venv is uv-managed and has no pip. Use `uv pip install --python .venv/bin/python -r requirements.txt`.                        |
| A second backend refuses to start                     | The single-instance claim is held per data directory, not per port. Give the second one its own `JARVIS_HOME`, or stop the first. |
