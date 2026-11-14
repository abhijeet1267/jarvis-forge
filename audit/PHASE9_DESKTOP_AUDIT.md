# PHASE 10 — Desktop App Audit (READ-ONLY)

Repo: `/Users/abhijeetmiskin/AppData/jarv/jarvis-forge`
Audited: 2026-09-25, 20:10–20:20 local (`date` returned `2026-09-25 20:11:14` mid-session)
Mode: read-only. No production code changed. No rebuild executed.
Not a git repository — all "change" evidence is mtimes, sizes and SHA-256 hashes.

---

## Headline verdict (measured)

| Question                               | Answer                                                                                                       | Strongest evidence                                                                                                                                      |
| -------------------------------------- | ------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Is the desktop bundle current?         | **NO.** The bundle is a 588 KB pointer to a source tree that no longer exists at the recorded path.          | `Contents/Resources/project_path.txt` contains `/Users/abhijeetmiskin/AppData/aisys/jarvis-forge`; `test -e` on it fails                                |
| Is the launcher stale?                 | **The launcher _code_ is current; the launcher's _embedded config pointer_ is stale.**                       | `app/launcher.py` and `Contents/Resources/launcher.py` are both SHA-256 `accd4b35…695772` (identical). `project_path.txt` is not                        |
| Does the shipped app start?            | **NO.** Exit 1 before any server, GUI, or self-test runs.                                                    | Bundle shim executed end-to-end: `JARVIS Forge: Could not locate the jarvis-forge project directory.`, `EXIT=1`                                         |
| Is the known P0 carried by the bundle? | **YES — by reference, unavoidably.** The bundle ships 0 files of `jarvis/`; it executes the repo copy live.  | `jarvis/tools/base.py` `NameError` reproduced on both interpreters the launcher can select (3.11.16 and 3.13.12)                                        |
| Does it pass Gatekeeper?               | **NO.** `spctl -a -vv` → `rejected` (exit 3).                                                                | Measured on a pristine-structure temp copy                                                                                                              |
| Does it pass codesign?                 | **YES on pristine structure** (ad-hoc). **NO as it sits on disk right now** — see Audit-introduced artifacts | `codesign --verify` on temp copy: `valid on disk` / `satisfies its Designated Requirement`; on the real path: `a sealed resource is missing or invalid` |

---

## 1. What is actually in `dist/Jarvis Forge.app`

### 1.1 Layout (complete — `find`, no truncation)

```
dist/Jarvis Forge.app/                                    588K  (du -sk = 588)
└── Contents/
    ├── Info.plist                              1429 B   sha256 69437f8d…7c2e70
    ├── PkgInfo                                    8 B   sha256 82502191…e8eca0   "APPL????"
    ├── MacOS/
    │   └── JarvisForge                        581 B   sha256 a687e857…ec0cf8   -rwxr-xr-x
    ├── Resources/
    │   ├── JarvisForge.icns                494832 B   sha256 23a95366…d5cac8
    │   ├── launcher.py                      28655 B   sha256 accd4b35…695772
    │   ├── project_path.txt                      48 B   sha256 04afd937…1af1f5c
    │   └── python_path.txt                       75 B   sha256 b10f7e07…e52c4ba
    └── _CodeSignature/
        ├── CodeDirectory                      196 B   sha256 5a064d02…e44a53d5
        ├── CodeRequirements                    60 B
        ├── CodeResources                     3156 B
        └── CodeSignature                        0 B
```

Payload = **11 files, 529 040 bytes** (excluding `_CodeSignature` accounting, that is 7 payload files). The icon is 93.5 % of the bundle.

### 1.2 It is NOT a frozen app — there is no embedded Python

`find "dist/Jarvis Forge.app" -name "*.py"` → **1** (only `Resources/launcher.py`).
`find` for `*.so`, `*.dylib`, `*.pyd`, `jarvis/`, `dashboard/`, `run.py` inside the bundle → **zero hits**.
`find "dist/Jarvis Forge.app" -path "*jarvis*" -name "*.py"` → **0 files**.

No PyInstaller / Nuitka / python-build-standalone runtime is present. `build_app.py` never invokes one; it is a hand-rolled bundle assembler (see §5).

**Answer to "is the app source tree bundled or does it import from the dev checkout":** it imports from the dev checkout, entirely, by absolute path reference. Confirmed twice:

- `Resources/project_path.txt` → the checkout path, read by `resolve_project_dir()` (`app/launcher.py:52`).
- `Resources/python_path.txt` → the interpreter, read by `resolve_python()` (`app/launcher.py:72`).
- The server itself resolves the dashboard from the checkout at import time: `jarvis/api/app.py:2093`
  `DASHBOARD = Path(__file__).resolve().parents[2] / "dashboard" / "index.html"`.

### 1.3 The embedded "Python version" — 3.13.12, external

`python_path.txt` (xxd-verified, no trailing newline) contains exactly:
`/Users/abhijeetmiskin/.workbuddy-ai/binaries/python/envs/default/bin/python`

That path **exists**; it is a symlink to `python3` which resolves to
`/Users/abhijeetmiskin/.workbuddy-ai/binaries/python/versions/3.13.12/bin/python3.13` → **Python 3.13.12** (`3.13.12 (main, Feb  4 2026 00:05:32) [Clang 21.1.4]`).

This is the origin of the `cpython-313` caches in the tree, and it is **not** the project venv (3.11.16). Dependency parity between the two was probed and is identical, so 3.13.12 can run the server (verified: it imports `jarvis.api.app` cleanly, uvicorn 0.53.0):

| module                                                                              | venv 3.11.16 | bundle 3.13.12 |
| ----------------------------------------------------------------------------------- | ------------ | -------------- |
| fastapi / uvicorn / pydantic / pydantic_settings / httpx / multipart / dotenv / PIL | OK           | OK             |
| sqlalchemy / aiosqlite / sounddevice / numpy / psutil                               | MISSING      | MISSING        |
| `tkinter`                                                                           | OK (Tk 9.0)  | OK (Tk 9.0)    |

Falls back gracefully: `/voice/status` shows recorder `ffmpeg` available, `sounddevice` unavailable.

### 1.4 Info.plist keys that matter (`plutil -p`, 17 entries)

| Key                                         | Value                                                                        | Note                                                                                                                          |
| ------------------------------------------- | ---------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| `CFBundleIdentifier`                        | `com.jarvisforge.app`                                                        | matches `BUNDLE_ID` in `build_app.py`                                                                                         |
| `CFBundleExecutable`                        | `JarvisForge`                                                                | file exists, mode 0755, and is a **`/bin/sh` script**, not Mach-O (`file` → "POSIX shell script text executable, ASCII text") |
| `CFBundleShortVersionString`                | `0.2.0`                                                                      | `pyproject.toml` says `version = "0.1.0"` — mismatch (P4)                                                                     |
| `CFBundleVersion`                           | `2`                                                                          | hard-coded literal in `build_app.py`, not derived from source                                                                 |
| `CFBundleIconFile`                          | `JarvisForge`                                                                | `.icns` present, 494 832 B                                                                                                    |
| `CFBundlePackageType` / `CFBundleSignature` | `APPL` / `????`                                                              | consistent with `PkgInfo` = `APPL????`                                                                                        |
| `LSMinimumSystemVersion`                    | `11.0`                                                                       |                                                                                                                               |
| `NSMicrophoneUsageDescription`              | present                                                                      | declared, but never acted on — see §6.5                                                                                       |
| `NSSpeechRecognitionUsageDescription`       | present                                                                      |                                                                                                                               |
| `NSAppleEventsUsageDescription`             | present                                                                      |                                                                                                                               |
| **absent**                                  | `LSUIElement`, `LSBackgroundOnly`, `NSPrincipalClass`, `NSHighDPI…` variants | no `NSPrincipalClass`; app has no Cocoa main thread, GUI is Tk                                                                |
| **absent**                                  | any screen-recording declaration                                             | macOS has no plist key for this; but there is also no preflight code — see §6.6                                               |

The Info.plist is byte-for-byte what `build_info_plist()` produces — no manual drift.

---

## 2. Does the launcher point at the CURRENT source location? — NO

### 2.1 Exact path strings found, and whether each exists on disk

| #   | Source of string                                            | Literal value                                                                           | Exists?                                                                                                      |
| --- | ----------------------------------------------------------- | --------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| 1   | `dist/Jarvis Forge.app/Contents/Resources/project_path.txt` | `/Users/abhijeetmiskin/AppData/aisys/jarvis-forge`                                      | **MISSING** (`ls: … No such file or directory`; `/Users/abhijeetmiskin/AppData/aisys` itself does not exist) |
| 2   | `dist/…/Resources/python_path.txt`                          | `/Users/abhijeetmiskin/.workbuddy-ai/binaries/python/envs/default/bin/python`           | EXISTS (3.13.12)                                                                                             |
| 3   | `app/launcher.py:47`                                        | `os.environ.get("JARVIS_PROJECT_DIR")`                                                  | env var, unset                                                                                               |
| 4   | `app/launcher.py:79`                                        | `Path.home() / ".workbuddy-ai/binaries/python/envs/default/bin/python"`                 | EXISTS                                                                                                       |
| 5   | `app/launcher.py:78`                                        | `str(project_dir / ".venv" / "bin" / "python")`                                         | EXISTS (but only reachable once #1 resolves)                                                                 |
| 6   | `app/launcher.py:149`                                       | `Path.home() / ".jarvis-forge"`                                                         | not created by this audit                                                                                    |
| 7   | `build_app.py:23`                                           | `REQUIRED_PYTHON = Path.home()/".workbuddy-ai/binaries/python/envs/default/bin/python"` | EXISTS                                                                                                       |
| 8   | `MacOS/JarvisForge` shim fallback                           | `$(command -v python3)` → `/opt/homebrew/bin/python3` (3.14.7)                          | EXISTS but **has no `tkinter`** (`ModuleNotFoundError`) — would force the headless path                      |

**No hardcoded `aisys` string exists in `app/launcher.py` or `build_app.py`.** `grep -n "aisys"` over both returns nothing. The stale path is _data baked into the built artifact_, produced by `build_app.py:132`
`(resources / "project_path.txt").write_text(str(PROJECT_DIR), …)` where `PROJECT_DIR = Path(__file__).resolve().parent` (`build_app.py:20`).

Conclusion: the build ran at **2026-09-25 12:59** while the tree was still rooted at `/Users/…/AppData/aisys/jarvis-forge`. The relocation moved `dist/` along with everything else and the pointer was never refreshed.

### 2.2 The launcher code itself is current

```
accd4b355708dc3e34002d8a58f663308890256154decd5c499b0497d2695772  app/launcher.py
accd4b355708dc3e34002d8a58f663308890256154decd5c499b0497d2695772  dist/Jarvis Forge.app/Contents/Resources/launcher.py
```

Identical SHA-256, identical size 28 655, identical mtime 2026-09-25 12:49:05 (`shutil.copy2` preserved it). So `build_app.py` shipped the newest launcher source; only the pointer is wrong.

### 2.3 The fallback rescue does NOT reach the new location

`resolve_project_dir()` (`app/launcher.py:44-67`) step 3 probes three ancestors for `run.py` + `jarvis/`. Measured by importing the **bundled** copy under 3.13.12:

```
BUNDLE_ROOT = /Users/…/jarv/jarvis-forge/dist/Jarvis Forge.app/Contents
  cand …/Jarvis Forge.app/Contents   | run.py: False | jarvis dir: False
  cand …/Jarvis Forge.app            | run.py: False | jarvis dir: False
  cand …/jarv/jarvis-forge/dist      | run.py: False | jarvis dir: False
resolve_project_dir RAISED: RuntimeError -> Could not locate the jarvis-forge project
  directory. Set JARVIS_PROJECT_DIR or rebuild the app with build_app.py.
recorded project_path.txt exists on disk: False
```

The rescue only reaches three levels up (`Contents`, `.app`, `dist/`); the checkout is four levels up from `Contents`. **No fallback can save it.**

### 2.4 End-to-end confirmation against the real bundle executable

Ran `dist/…/Contents/MacOS/JarvisForge` with `env -i`, a throwaway `JARVIS_HOME=$(mktemp -d)`, `JARVIS_SELFTEST=1`, and a sanitised `PATH` containing no `osascript` so `_fatal()` could not open a modal dialog:

```
JARVIS Forge: Could not locate the jarvis-forge project directory. Set JARVIS_PROJECT_DIR or rebuild the app with build_app.py.
EXIT=1
```

`find $JARVIS_HOME` afterwards → empty. No port bound, no process left, no GUI shown.

---

## 3. Source / bundle drift

### 3.1 `jarvis/` — file-list diff and hashes

| Metric                                      | Value   |
| ------------------------------------------- | ------- |
| repo `jarvis/**/*.py` (excl. `__pycache__`) | **126** |
| bundle `jarvis/**/*.py`                     | **0**   |
| common paths both sides                     | **0**   |
| identical SHA-256 both sides                | **0**   |
| only in repo                                | **126** |
| only in bundle                              | **0**   |

**Drift is structurally impossible for `jarvis/`, because `jarvis/` is not bundled.** There is no second revision of `jarvis/tools/base.py` hiding in the `.app` to be "older" or "fixed". The bundle's only copy of application logic is `launcher.py`, and the server it spawns runs the repo's live 126 files out of `cwd=project_dir` (`app/launcher.py:269-277`, `[self.python, "run.py"]`).

The answer to the key drift question is therefore: **the bundle carries the repo's P0 verbatim, by reference.** Any future edit to `jarvis/` is picked up without a rebuild — which is also why the pointer (§2), not the payload, is the whole failure.

### 3.2 Age relationship: the bundle predates live source edits

Bundle build time = newest build-produced artifact = `project_path.txt` / `Info.plist` / `MacOS/JarvisForge` / `.icns` all **2026-09-25 12:59**.

`find … -newermt "2026-09-25 12:59:00"` (excluding `.venv`, `__pycache__`, `audit/`, `dist/`, `.pytest_cache`) → **6 source files newer than the bundle**:

```
jarvis/tools/base.py        2026-09-25 18:26:22   <-- the P0 file
jarvis/core/models.py       2026-09-25 18:24:19
jarvis/tools/files.py       2026-09-25 17:01:03
jarvis/core/planner.py      2026-09-25 16:59:01
jarvis/security/policy.py   2026-09-25 16:56:49
jarvis/security/paths.py    2026-09-25 16:56:08
```

(plus `tests/test_{paths,pipeline,planner,policy}.py`, `BASELINE_REPORT.md`, `PROJECT_DETAILS.md`, `STATUS.md`)

Because of §3.1 this does **not** mean the app runs old code — it will run these. It does mean the **code signature seal's age** and any assumption of "bundle == a snapshot of source at 12:59" is false.

### 3.3 mtime extremes, both sides

| Side                                            | Oldest                                        | Newest                                                                                                                   |
| ----------------------------------------------- | --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| repo `jarvis/*.py` (126 files)                  | `jarvis/__init__.py` — 2026-09-19 00:11:57    | `jarvis/tools/base.py` — 2026-09-25 18:26:22                                                                             |
| bundle payload (11 files, excl. audit artifact) | `Resources/launcher.py` — 2026-09-25 12:49:05 | `Resources/{project,python}_path.txt`, `Info.plist`, `MacOS/JarvisForge`, `_CodeSignature/*`, `.icns` — 2026-09-25 12:59 |
| **the one shared file** (`launcher.py`)         | both sides 12:49:05, identical hash           | —                                                                                                                        |

Span on the repo side: 6 days 18 h. Bundle side: 9 min 55 s.

### 3.4 Second-order drift surface the design does not defend against

Anything appearing under `Contents/Resources/` invalidates the seal. Proven in this session (§4.2, §Appendix-B): one `.pyc` was enough. `build_app.py` copies `launcher.py` in with `shutil.copy2` — which **preserves the source's mtime** (12:49) while stamping the pointer files at build time (12:59) — so an interpreter whose caches key off that mtime can be coaxed into writing beside it.

---

## 4. Signature state (actual command output)

### 4.1 `codesign -dv` / `-dvvv` — AD-HOC, no identity

```
Executable=…/dist/Jarvis Forge.app/Contents/MacOS/JarvisForge
Identifier=com.jarvisforge.app
Format=app bundle with generic
CodeDirectory v=20100 size=196 flags=0x2(adhoc) hashes=1+3 location=embedded
Hash type=sha256 size=32
CDHash=5a064d0267344a2265297e27db282dd6751a18c4
CMSDigest=5a064d0267344a2265297e27db282dd6751a18c44ff6c899f096c019e44a53d5
Signature=adhoc
Info.plist entries=17
TeamIdentifier=not set
Sealed Resources version=2 rules=13 files=4
Internal requirements count=1 size=60
```

Verdict: **ad-hoc signed**. `Signature=adhoc`, `TeamIdentifier=not set`, no Developer ID, no notarization ticket. `codesign -d --entitlements :-` returned **no entitlements payload** (only the header line) — the bundle carries an empty entitlements set, consistent with `build_app.py:163` which signs with `--force --sign - --timestamp=none` and never passes `--entitlements`.

`Format=app bundle with generic` (not `…with Mach-O`) reflects that `MacOS/JarvisForge` is a shell script.

### 4.2 `codesign --verify`

As it sits on disk **right now** (contains one `.pyc` this audit created; see Appendix B):

```
dist/Jarvis Forge.app: a sealed resource is missing or invalid
file added: /Users/…/jarv/jarvis-forge/dist/Jarvis Forge.app/Contents/Resources/__pycache__/launcher.cpython-313.pyc
exit=1
```

On a pristine-structure copy (`cp -R` to `mktemp -d`, that one `.pyc` deleted **in the copy only**), which is the bundle's true built state:

```
/var/folders/…/tmp.vfPBIbIL8e/Jarvis Forge.app: valid on disk
/var/folders/…/tmp.vfPBIbIL8e/Jarvis Forge.app: satisfies its Designated Requirement
exit=0
```

So: the signature **is** cryptographically sound as-built; it is **fragile**, and one generated bytecode file breaks it.

### 4.3 `spctl -a -vv` — GATEKEEPER REJECTS

```
$ spctl -a -vv "dist/Jarvis Forge.app"
dist/Jarvis Forge.app: a sealed resource is missing or invalid        exit=1   # current on-disk state

$ spctl -a -vv "$TMP/Jarvis Forge.app"        # pristine copy
/var/folders/…/tmp.vfPBIbIL8e/Jarvis Forge.app: rejected               exit=3
```

**Rejected in both cases.** Ad-hoc + unnotarized ⇒ Gatekeeper will not assess it acceptingly. It only launches locally because the quarantine bit is absent (§4.4). `install_app.sh` copies to `/Applications` and runs `xattr -cr`, which keeps it launchable on _this_ machine but does nothing about the identity.

### 4.4 Quarantine xattrs — none

```
$ xattr "dist/Jarvis Forge.app"
com.apple.provenance
$ xattr -r -l "dist/Jarvis Forge.app"
   … every entry:  com.apple.provenance:
```

**`com.apple.quarantine` is absent on every file in the bundle** (18 inodes scanned). Only `com.apple.provenance` (macOS lineage marker, no value) is present. Consistent with `build_app.py:171` `run(["xattr","-cr",str(app)])` and `install_app.sh:44`. Consequence: Gatekeeper is bypassed by provenance, not by assessment.

`/Applications/Jarvis Forge.app` **does not exist** — `ls /Applications | grep -i jarvis` → no entry. The app was built but never installed.

---

## 5. Does the bundle build at all? — **UNVERIFIED** (statically assessed; not executed, per brief)

**I did not run `build_app.py`.** The word "builds" below is UNVERIFIED. Verification command:

```
python build_app.py --dest "$(mktemp -d)"        # non-destructive: writes only into the temp dir
```

(Note `build()` does `shutil.rmtree(app)` on the target first — with the default `--dest` that deletes `dist/Jarvis Forge.app`, the audited artifact. Always pass `--dest`.)

### 5.1 Declared inputs — all present

| Referenced at      | Path                                            | Status                                                                                           |
| ------------------ | ----------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| `build_app.py:126` | `PROJECT_DIR/app/make_icon.py`                  | **EXISTS** (6 708 B, mtime 2026-09-19 11:27)                                                     |
| `build_app.py:131` | `PROJECT_DIR/app/launcher.py`                   | **EXISTS** (28 655 B, mtime 2026-09-25 12:49)                                                    |
| `build_app.py:20`  | `PROJECT_DIR = Path(__file__).resolve().parent` | **SELF-LOCATING** → resolves to the current `…/jarv/jarvis-forge`. No dead literal in the script |

### 5.2 Dead-path references — **none found**

`build_app.py` contains no `aisys` string and no other absolute literal. Its only absolute path, `REQUIRED_PYTHON` (`build_app.py:23`), **exists** (`…/versions/3.13.12/bin/python3.13`). `find_python()` therefore takes branch 1 and a rebuild would re-stamp `project_path.txt` to the _current_ tree, i.e. **a rebuild from today's tree repairs §2's P0.** That is the difference between a broken _script_ and a stale _artifact_ — here it is the artifact.

### 5.3 Assumed toolchain — all present

| Tool                                                                            | Needed by                                                                                                                               | Status                                                                              |
| ------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------- |
| `python` ≥3.10 (`from __future__ import annotations`, `plistlib`, `subprocess`) | whole script                                                                                                                            | PRESENT — 3.13.12 / 3.11.16 / 3.9.6 (3.9.6 is _below_ `requires-python = ">=3.10"`) |
| `PIL` (Pillow)                                                                  | `app/make_icon.py:17`                                                                                                                   | PRESENT on 3.13.12 and 3.11.16 — **Pillow 12.3.0**                                  |
| `iconutil`, `sips`                                                              | icon set → `.icns`                                                                                                                      | PRESENT (`/usr/bin/iconutil`, `/usr/bin/sips`)                                      |
| `swiftc`                                                                        | _not_ used by `build_app.py`; used at runtime by `jarvis/voice/stt.py` (`available()` gates on `shutil.which("swiftc")`) and `audio.py` | PRESENT (`/usr/bin/swiftc`)                                                         |
| `codesign`                                                                      | `build_app.py:163`                                                                                                                      | PRESENT                                                                             |
| `xattr`                                                                         | `build_app.py:171`                                                                                                                      | PRESENT                                                                             |
| `osascript`                                                                     | shim error dialog + `launcher.py:_fatal`/`_notify`                                                                                      | PRESENT                                                                             |
| `clang`, `plutil`, `say`                                                        | aux                                                                                                                                     | PRESENT                                                                             |

### 5.4 Static build risks (would not stop the build, but change the result)

1. **Interpreter-selection divergence.** A rebuild re-records 3.13.12 (`find_python` branch 1), while `tests/` and `.venv` are 3.11.16. The shipped app is never exercised by the same interpreter the test suite uses. `resolve_python()` (`app/launcher.py:75-81`) also puts `recorded_path` **ahead of** `project_dir/.venv/bin/python`, so the project venv can never win unless `JARVIS_PYTHON` is set.
2. **The shim's `python3` fallback is `tkinter`-less.** `/opt/homebrew/bin/python3` (3.14.7) → `import tkinter` `ModuleNotFoundError`. Not fatal (`build_ui` raises `GuiUnavailable` → `run_headless`), but the control window silently disappears if `python_path.txt` ever goes stale.
3. **Nothing in the build seals the _pointer's validity_.** `build()` never asserts that `PROJECT_DIR` is a stable location, so every relocation re-creates P0-P13. See the fix in the findings list.
4. **`--timestamp=none`.** Ad-hoc CDHash changes on every rebuild ⇒ any TCC grant keyed to the previous build's code identity is invalidated by the next rebuild. `build_app.py`'s docstring claim "ad-hoc signed so TCC has a stable identity to attach permissions to" is contradicted: ad-hoc gives a stable identity _per build_, not across builds. Measured CDHash of the present build: `5a064d02…18c4`.

---

## 6. Self-test / dashboard / server / shutdown / microphone / speech / screen

**Self-test entrypoint exists:** `app/launcher.py:602 def selftest(controller)`, reached from `main()` at `:738` on `JARVIS_SELFTEST == "1"`. Documented as `JARVIS_SELFTEST=1 "dist/Jarvis Forge.app/Contents/MacOS/JarvisForge"` (`launcher.py:605`, `README.md:75`, `STATUS.md:121`, `PROJECT_DETAILS.md:338`).

**It is unreachable from the shipped bundle.** `main()` calls `resolve_project_dir()` at `:726`, _before_ the `JARVIS_SELFTEST` branch at `:738`. Measured end-to-end in §2.4: exit 1, no report printed. Every documented invocation of the self-test on the built `.app` fails identically today.

Because the bundle cannot start, **all runtime evidence below was taken from the dev server**, launched read-only exactly as the brief allows: `JARVIS_HOME=$(mktemp -d) PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python run.py`, port pre-checked free (`lsof -nP -iTCP:8765` → exit 1, no listener; `connect_ex` → 61), then SIGTERM'd and confirmed dead.

### 6.1 Server startup — PASSES (dev path)

```
INFO: Uvicorn running on http://127.0.0.1:8765 (Press CTRL+C to quit)
INFO: Application startup complete.
20:18:16 INFO jarvis.security.auth: api token generated
20:18:16 INFO jarvis.db: database ready
20:18:16 INFO jarvis.tools: tool registry built
20:18:16 INFO jarvis.core.orchestrator: jarvis core initialised
20:18:16 INFO jarvis.scheduler.runner: scheduler started
20:18:19 INFO jarvis.voice.audio: mic consent helper built
```

Ready in <9 s; `lsof` confirmed `python3.1 25193 … TCP 127.0.0.1:8765 (LISTEN)`. Bound to loopback only.

### 6.2 Dashboard — PASSES

```
GET /dashboard → HTTP=200  bytes=22809  type=text/html; charset=utf-8  time=0.004785
<title>JARVIS Forge — Control Dashboard</title>
GET /          → HTTP=200
```

Served from `parents[2]/dashboard/index.html` in the checkout → further confirms §1.2.

### 6.3 Health + auth — PASSES

```
GET /health  (X-JARVIS-Token)  → HTTP=200  time=0.109
  {"status":"ok","tools":126,"agents":[15 listed],"llm_enabled":false,
   "pending_confirmations":0,"security":{"auth_enabled":true,"host_header_enforced":true,…}}
GET /devices                   → HTTP=200  laptop_main, caps incl. "screen"
GET /health  (no token)        → HTTP=401  + log "jarvis.api: rejected unauthenticated request"
```

`tools: 126` matches `find jarvis -name "*.py" | wc -l` = 126. **Note:** the registry builds successfully _because_ the P0 in `tools/base.py` is a _call-time_ `NameError`, not an import-time one — verified in §6.4.

### 6.4 Clean shutdown — PASSES

```
$ kill -TERM 25193 ; sleep 4
INFO:     Shutting down
INFO:     Waiting for application shutdown.
INFO:     Application shutdown complete.
INFO:     Finished server process [25193]
process gone | lsof -nP -iTCP:8765 → (no output, port released)
```

`$JARVIS_HOME` contained only `jarvis.db{,-wal,-shm}`, `jarvis.log`, `api_token`, `mail/inbox`, `devices/tls`, `voice/mic_state`. Nothing written into the repo. Launcher's own shutdown logic (`ServerController.stop`, `app/launcher.py:304-311`: `terminate()` → 8 s grace → `kill()`) is **UNVERIFIED** against a real app launch because the bundle cannot start.

### 6.5 Microphone — measured DENIED, and the prompt is never issued

```json
"audio": { "capture_available": false, "capture_state": "blocked",
  "microphone": { "state":"blocked", "permission":"denied", "granted":false, "known":true,
    "detail":"macOS has denied this app the microphone. Enable it in System Settings -> Privacy & Security -> Microphone, then restart it.",
    "host":"unbundled process (python)", "measured_seconds_ago":31.91,
    "devices":[{"Microsoft Teams Audio"},{"MacBook Air Microphone"},{"MJAudioRecorder"}] },
  "recorders":[{"sounddevice",false},{"ffmpeg",true}] }
```

Good engineering, honestly reported: 4 distinct states (`ready`/`blocked`/`no_backend`/`unknown`) and `mic_note()` (`app/launcher.py:201`) refuses to conflate "a binary exists" with "we are allowed to listen". `probe_mic` runs on a background thread and `settled_voice_status()` (`:221`) waits up to 25 s rather than reporting a transient `measuring` as a failure.

**P1 — the microphone prompt can never appear.** `jarvis/voice/native/mic_state.swift:44` only _queries_: `AVCaptureDevice.authorizationStatus(for: .audio)`. Grep for a _request_ across all `.py`/`.swift`/`.sh` in the tree:

```
grep -rn "requestAccess|AVCaptureDevice.requestAccess|SFSpeechRecognizer.requestAuthorization|…"
  jarvis/voice/native/mic_state.swift:13   <-- inside a COMMENT explaining why it does NOT call it
  jarvis/voice/native/macos_speech.swift:50 <-- real call, speech only
```

The comment at `mic_state.swift:13-17` documents this as a deliberate design (`requestAccess` from a bare CLI binary "either never answers or aborts the process"). The consequence is not documented anywhere: **`NSMicrophoneUsageDescription` is declared in Info.plist but no code path ever triggers it**, so a first-time bundled user sees `blocked / notDetermined` forever and must hand-grant in System Settings. `install_app.sh:47` states the opposite — _"First launch will ask for Microphone and Speech Recognition permission."_ — which is **false for Microphone** as measured.

Secondary, reporting-level: `consent_host()` (`jarvis/voice/audio.py:68-79`) decides "am I in a bundle?" by substring-testing `sys.executable`'s resolved path for `.app/Contents/`. The launcher's resolved interpreter is `/Users/abhijeetmiskin/.workbuddy-ai/binaries/python/versions/3.13.12/bin/python3.13` — it can **never** contain `.app/Contents/`, so even a correctly-launched bundled app will self-report `"unbundled process (python3.13)"` and attribute the denial to the wrong host. (Currently measured value `"unbundled process (python)"` is accurate for the dev run; the bundled-case value is UNVERIFIED because the bundle does not start.)

### 6.6 Speech — STT reports "available" that will abort; TTS is fine

```json
"tts": { "enabled":true, "available":{"macos_say":true,"pyttsx3":false} }
"stt": { "configured_backend":"auto","language":"en-US",
         "available":{"macos_speech":true,"whisper_cpp":false,"openai_whisper":false,"transcript":true},
         "notes":[] }
"wake": { "wake_words":["jarvis","hey jarvis","ok jarvis"], "require_wake_word":true, "mode":"transcript_matching" }
```

`macos_say` present ⇒ the launcher's "Speak" button works. `pyttsx3`/`openai_whisper`/`whisper_cpp`/`sounddevice` absent — matches §1.3's dependency probe.

**P2 — `macos_speech: true` is a false-positive outside a bundle.** `MacOSSpeechSTT.available()` (`jarvis/voice/stt.py:126-131`) returns true on `(swiftc present) and (source present) and (blocked marker absent)`. It tests _buildability_, not grantability. The class docstring (`:100-106`) is explicit that from a non-bundled process Apple **SIGABRTs** it, and `transcribe()` handles that as `returncode in (134, -6)` → writes `$JARVIS_HOME/voice/native/.tcc_blocked` and notes `"blocked by TCC (not a signed app bundle)"`. So `/voice/status` advertises the _first-choice_ STT backend while the code knows it will abort the helper; `auto` selection (`:361-364`, `for name in ("macos_speech", …)`) picks it first every time. The marker only gets written _after_ the first abort, so a fresh install always over-reports.
Actual end-to-end STT (synthesize → transcribe, as `selftest()` does at `launcher.py:693-700`) is **UNVERIFIED** — it requires the app to start, and it would prompt/abort.

### 6.7 Screen Recording — no preflight, no probe, and the failure detector can't see a stripped capture

There is **no** screen-permission machinery anywhere in the tree:

- `CGPreflightScreenCaptureAccess` / `CGRequestScreenCaptureAccess`: **0 hits** in `.py`/`.swift`.
- No `/screen/status` endpoint. The only route touching the screen is `@app.post("/vision/screen")` (`jarvis/api/app.py:1884`) → `read_screen()` — a **side-effecting** capture (writes a PNG to `Path.home()` or the caller's path), not a status probe.
- Two independent capture implementations, both reactive-only: `jarvis/tools/system.py:119` and `jarvis/vision/screen.py:37`, both `screencapture -x <target>`.
- Contrast with microphone, which got a purpose-built Swift preflight (`mic_state.swift`), a 4-state model, a background probe, an honest `capture_state`, and a `consent_host()` attribution. Screen Recording got none of that. It is the one TCC service in this product with zero measurement.

Detection logic in both files is `if code != 0 or not target.exists()`. On current macOS a screen-recording-denied `screencapture -x` still exits 0 and still writes a file (containing only the desktop wallpaper), so this predicate is **not** a permission test — the error string "On macOS this usually means Screen Recording permission has not been granted" (`system.py:126-131`) can never fire in the case it names, and the success branch reports `ok` for a blank image. I did **not** execute `screencapture` (it would capture the user's screen), so the exit-code/blank-file behaviour is **UNVERIFIED-at-runtime**; the absence of any preflight or size/entropy check in the two code paths is measured from source.

Also note `take_screenshot`'s success branch is `return ok(… verified=target.exists())` (`system.py:133-141`) → straight into the P0.

---

## 7. Stale `.pyc` — as it affects the desktop path specifically

### 7.1 Whole-tree census (excluding `.venv`)

```
total .pyc                              = 340
  cpython-311                           = 154      cpython-311-pytest-8 = 27
  cpython-313                           = 129      cpython-313-pytest-9 = 24
  cpython-314                           = 6
embedding the OLD /Users/…/AppData/aisys = 300  (88.2 %)
cache header MATCHES its source          = 329  -> Python REUSES these caches as-is
cache header DIFFERS (recompiled)        = 11
orphaned (no matching .py)               = 0
```

Interpreters confirmed: `.venv` 3.11.16, bundle-recorded 3.13.12, `/opt/homebrew/bin/python3` 3.14.7, `/usr/bin/python3` 3.9.6. All three of 311/313/314 have caches in the tree.

### 7.2 Why the caches are _not_ inert

`jarvis/` breakdown of old-path embedding: **123/126** cpython-311 caches and **119/125** cpython-313 caches embed `AppData/aisys`. And because the relocation preserved mtimes, `header_matches_src = 329`: the cached code objects are still considered valid, so **the interpreter will load bytecode whose `co_filename` points at a path that does not exist.** Consequences, in order of how they bite the desktop path:

1. **Misleading tracebacks.** An exception raised from cached code prints `/Users/abhijeetmiskin/AppData/aisys/jarvis-forge/…` — a nonexistent file — sending whoever debugs the app to look in the wrong tree. Directly relevant: the P0 in §7.3 is a `NameError` whose traceback frames come from these caches.
2. **`app/__pycache__/launcher.cpython-311.pyc` is the desktop path's own stale cache** — 47 714 B, mtime **2026-09-25 12:58:08**, one minute _before_ the bundle build at 12:59, and its `co_filename` string is `@/Users/abhijeetmiskin/AppData/aisys/jarvis-forge/app/launcher.py`. So the launcher was imported-from-cache in the old tree during the same minute-scale window that produced the bundle. Two launcher caches now disagree:
   - `launcher.cpython-311.pyc` → old absolute `aisys` path, header **stale** (cached size 25 585 vs current 28 655) → recompiles.
   - `launcher.cpython-313.pyc` → relative `app/launcher.py`, mtime 2026-09-19 11:47, size 25 585 → also stale vs the current 28 655 B source.
     Neither would execute old code; both would print old paths.
3. **The bundle is the one place a stray cache is fatal to signature.** `Contents/Resources/__pycache__/launcher.cpython-313.pyc` produced `codesign --verify` → `a sealed resource is missing or invalid` (§4.2) and `spctl` → `a sealed resource is missing or invalid`. The shipped `Resources/` is sealed at `rules=13 files=4`; any `.pyc` added there breaks it, and 3 of 4 sealed files under `Resources/` are tiny pointer/config files whose mtimes the build manipulates.
4. **`cpython-314` caches prove a third interpreter has been in the tree** (6 files). Nothing in `build_app.py` or `launcher.py` pins one interpreter, so the set will keep growing: `resolve_python()`'s candidate list is 5 entries deep (§5.4.1).

### 7.3 The caches do NOT mask the P0 — reproduced on both candidate interpreters

```
./.venv/bin/python (3.11.16)         ->  Verification in base namespace: False ; ok() RAISED: NameError -> name 'Verification' is not defined
bundle python (3.13.12)              ->  Verification in base namespace: False ; ok() RAISED: NameError -> name 'Verification' is not defined
```

Confirmed in source: `jarvis/tools/base.py:16` `from ..core.models import ActionResult` — `Verification` absent from the 8 `from …` imports (`:8-22`), yet referenced at `:87` (annotation, neutralised by `from __future__ import annotations` at `:8`), `:103`, `:110`, `:117`. The enum **is** defined at `jarvis/core/models.py:210 class Verification(str, Enum):` — it is simply never imported into `base.py`. Blast radius measured: **171 `return ok(`/`= ok(`/`return unverified(` call sites across 19 files** under `jarvis/`.

This is the P0 as briefed, re-confirmed only to answer the drift question: **there is no bundled `jarvis/` copy that could have been a different revision** (§3.1). The bundle carries it by reference and there is no cache masking it — every desktop tool success path hits it. UNVERIFIED end-to-end through the running server (I did not invoke a side-effecting tool), but the code path from `ok()` is reached by 19 modules.

---

## Findings

### P0 — blockers

**P0-D1 · The shipped app cannot start. `project_path.txt` points at the pre-relocation tree.** (new; §2)
`Contents/Resources/project_path.txt` = `/Users/abhijeetmiskin/AppData/aisys/jarvis-forge`, which does not exist (`/Users/abhijeetmiskin/AppData/aisys` itself is absent). `resolve_project_dir()` step 2 rejects it via `Path(text).is_dir()`; step 3's ancestor walk only reaches `dist/` (3 levels up from `Contents/`), never the checkout at 4 levels. Measured end-to-end against `Contents/MacOS/JarvisForge`: exit 1, `Could not locate the jarvis-forge project directory`. Nothing in `app/launcher.py` or `build_app.py` hardcodes the old path — it is baked into the artifact. Every documented app command (`README.md:75`, `STATUS.md:121`, `PROJECT_DETAILS.md:338`) fails.
_Fix:_ `python build_app.py --dest "$(mktemp -d)"` re-stamps the pointer correctly (it is `Path(__file__).resolve().parent`); but also resolve `run.py`-relative to `BUNDLE_ROOT`'s real ancestors or vendor the payload, or the next relocation re-breaks it identically.

**P0-D2 · The known `Verification` NameError is carried by the bundle and reaches 171 call sites.** (inherited; §7.3)
`jarvis/tools/base.py:16` imports only `ActionResult`; `Verification` is used at `:103/:110/:117`. Reproduced on 3.11.16 **and** 3.13.12 — the two interpreters the launcher can select. Because `jarvis/` is not bundled, the app runs exactly this file. The bundle cannot dodge it by having an older copy: there is no copy. Blast radius: 19 files, 171 `ok()`/`unverified()` sites; `take_screenshot`'s success path (`system.py:133`) is one of them.

**P0-D3 · `selftest()` is unreachable, so the app has no working health check.** (consequence of D1; §6)
`main()` calls `resolve_project_dir()` at `launcher.py:726`, before the `JARVIS_SELFTEST` branch at `:738`. The 9-check self-test — the only thing standing between a broken install and a silent failure, and the source of the "7/9" claim in `STATUS.md:25` / `BASELINE_REPORT.md:34` — cannot be invoked on the built bundle at all. **Those doc claims are now unreproducible**: they can only have been measured while the tree was at `aisys`.

### P1 — high

**P1-D1 · The microphone TCC prompt is never presented.** (§6.5)
`mic_state.swift:44` only calls `authorizationStatus`; `AVCaptureDevice.requestAccess` appears **nowhere** in the tree (only in a comment at `:13` explaining its omission). `NSMicrophoneUsageDescription` is therefore declared-but-unexercised, and measured state is `permission:"denied"`, `capture_state:"blocked"`, `capture_available:false`. `install_app.sh:47` promises _"First launch will ask for Microphone … permission"_ — false as measured. A first-time user is told to grant access to an app that never asked.

**P1-D2 · Gatekeeper rejects the app; the local build only runs because quarantine was stripped.** (§4.3, §4.4)
`spctl -a -vv` → `rejected` (exit 3) on the pristine structure; `Signature=adhoc`, `TeamIdentifier=not set`, no entitlements, no notarization, `com.apple.quarantine` absent on all 18 inodes. Distribution requires a Developer ID + `--entitlements`; `STATUS.md:577` acknowledges needing one, so shipping ad-hoc is a known gap — but nothing enforces the "local build only" boundary, and `install_app.sh` `cp -R`s into `/Applications` for real use.

**P1-D3 · One generated `.pyc` inside `Resources/` voids the code signature.** (§4.2, §7.2.3)
`codesign --verify` on the on-disk bundle: `a sealed resource is missing or invalid / file added: …/Resources/__pycache__/launcher.cpython-313.pyc`. Sealed at `rules=13 files=4`. `build_app.py` neither excludes `__pycache__` from the copy step nor runs `codesign --verify` before declaring success (its own verify at `:167` only prints a warning line, and the build still exits 0).

**P1-D4 · `/voice/status` advertises an STT backend that the same module says will SIGABRT.** (§6.6)
`MacOSSpeechSTT.available()` (`stt.py:126-131`) tests `swiftc` + source + absent blocked-marker — buildability, not grantability. Auto-selection (`:361`) prefers `macos_speech` first. Measured `"macos_speech": true` with `"notes": []`. `available()` returns true until _after_ the first abort creates `$JARVIS_HOME/voice/native/.tcc_blocked`, so every fresh install over-reports its speech capability.

### P2 — medium

**P2-D1 · Screen Recording has no preflight, no probe, and an ineffective failure test.** (§6.7)
0 hits for `CGPreflightScreenCaptureAccess`/`CGRequestScreenCaptureAccess` in `.py`/`.swift`; no `/screen/status`; only side-effecting `POST /vision/screen` (`api/app.py:1884`). Both capture paths test `if code != 0 or not target.exists()` — a denied `screencapture -x` still exits 0 and still writes a wallpaper-only file, so the "usually means Screen Recording permission" branch (`system.py:126-131`, `screen.py:48-54`) cannot fire in the case it names. Mic got a purpose-built Swift preflight; screen got none. Runtime denial behaviour UNVERIFIED (not executed, to avoid capturing the user's screen).

**P2-D2 · `consent_host()` can never recognise its own bundle.** (§6.5)
`audio.py:75-79` substring-tests `Path(sys.executable).resolve()` for `.app/Contents/`. The launcher's resolved interpreter is `~/.workbuddy-ai/binaries/python/versions/3.13.12/bin/python3.13` (`python_path.txt`, symlink resolved) — structurally incapable of containing that marker, so a correctly bundled run still reports `unbundled process (python3.13)` and blames the wrong host for the denial. Bundled-case value UNVERIFIED (bundle won't start).

**P2-D3 · The app never runs under the project venv's interpreter.** (§5.4.1)
`find_python()` prefers `~/.workbuddy-ai/…` → re-records 3.13.12; `resolve_python()` orders `recorded_path` **ahead of** `project_dir/.venv/bin/python`, so 3.11.16 is unreachable without `JARVIS_PYTHON`. Consequences measured: caches split 154×311 / 129×313 / 6×314 across three interpreters; the suite that guards the P0 tests 3.11 while users get 3.13. No dep-set difference today (probed identical), so this is a divergence risk, not a present break.

**P2-D4 · Ad-hoc signing is not the "stable TCC identity" the build script claims.** (§5.4.4)
`build_app.py` docstring: _"ad-hoc signed so TCC has a stable identity to attach permissions to."_ Ad-hoc CDHash changes per build (this one: `5a064d0267344a2265297e27db282dd6751a18c4`), and `--timestamp=none` plus no `--entitlements` means every rebuild can orphan previously granted Microphone/Speech permissions — the exact failure the `launcher.py:682-688` self-test hint ("delete `…/.tcc_blocked` to retry from a bundled app") exists to work around.

**P2-D5 · Stale caches print a path that does not exist.** (§7.2)
300/340 (88.2 %) of `.pyc` embed `AppData/aisys`, and 329/340 still pass their mtime+size header check, so they are **reused, not recompiled**. Tracebacks from the app (whose server runs from this tree) will cite `/Users/abhijeetmiskin/AppData/aisys/jarvis-forge/…`. Includes `app/__pycache__/launcher.cpython-311.pyc` (12:58:08, one minute before the 12:59 build).

### P3 — low

**P3-D1 · The shim's `python3` fallback has no `tkinter`.** (§5.4.2) `command -v python3` → `/opt/homebrew/bin/python3` 3.14.7, `import tkinter` → `ModuleNotFoundError` (venv 3.11 and 3.13.12 both have Tk 9.0; `/usr/bin/python3` has 8.5). Degradation is graceful (`GuiUnavailable` → `run_headless`, `launcher.py:755-757`), so the control window silently vanishes if `python_path.txt` ever rots.

**P3-D2 · `shutil.copy2` makes `Resources/launcher.py` look older than its own build.** (§3.4) `launcher.py` mtime 12:49:05 vs every build-written file at 12:59. A 9-minute spread inside one sealed bundle; mtime-based freshness reasoning about the artifact is unreliable.

**P3-D3 · Version metadata disagree.** (§1.4) `Info.plist CFBundleShortVersionString=0.2.0` / `CFBundleVersion=2` (literal at `build_app.py:22`) vs `pyproject.toml version="0.1.0"`. `CFBundleVersion` is a hard-coded string, so it will not advance across rebuilds.

**P3-D4 · `/Applications/Jarvis Forge.app` does not exist.** (§4.4) `build_app.py` completed, `install_app.sh` was never run (or was reverted). Confirmed by `ls /Applications | grep -i jarvis` → no entry. Nothing is broken for users _because_ nothing is installed, so the D1-D3 chain has no live blast radius yet.

**P3-D5 · `build_app.py`'s own `codesign --verify` cannot fail the build.** (§7.2.3) At `:166-168` the verify result is only printed (`"codesign verify: …"`); the build proceeds and `main()` returns 0 regardless. D1's signature break is therefore invisible to the builder's exit status.

### P4 — informational

**P4-D1 · No Mach-O anywhere.** `CFBundleExecutable` is a 581-byte `/bin/sh` script (`file` → "POSIX shell script text executable"), hence `Format=app bundle with generic`. Functional as a shim; means no hardened runtime, no `NSPrincipalClass`, and TCC attribution rests entirely on the LaunchServices responsible-process mechanism rather than on the binary itself.

**P4-D2 · 3 of 4 sealed `Resources/` files are pointers, not payload.** `project_path.txt` (48 B) + `python_path.txt` (75 B) + `launcher.py` vs a 494 832 B icon = 93.5 % icon. The bundle's real content is two path strings — which is precisely why relocation broke it and why "rebuild" is the whole fix.

**P4-D3 · `swiftc`/`PIL`/`iconutil` all present** (§5.3): no toolchain reason to expect a build failure. Still UNVERIFIED — not executed, per brief.

---

## Appendix A — exact commands used

All read-only unless marked. Nothing was piped to a shell for execution of remote content. `python -c` bodies are quoted in full where they matter.

### A.1 Bundle enumeration

```
ls -la . ; ls -la app/ ; ls -la dist/
find "dist/Jarvis Forge.app" -maxdepth 4 -print
find "dist/Jarvis Forge.app" -exec ls -la {} \;
du -sh "dist/Jarvis Forge.app" ; du -sk "dist/Jarvis Forge.app"
find "dist/Jarvis Forge.app" -type f | sort
find "dist/Jarvis Forge.app" -type f -not -path "*__pycache__*" -exec stat -f "%z" {} \; \
  | awk '{s+=$1} END {print s" bytes in "NR" files"}'
(cd "dist/Jarvis Forge.app" && find . -type f -not -path "*__pycache__*" | sort | xargs shasum -a 256)
plutil -p "dist/Jarvis Forge.app/Contents/Info.plist"
xxd "dist/Jarvis Forge.app/Contents/Resources/project_path.txt"
xxd "dist/Jarvis Forge.app/Contents/Resources/python_path.txt"
xxd "dist/Jarvis Forge.app/Contents/PkgInfo"
cat "dist/Jarvis Forge.app/Contents/MacOS/JarvisForge"
file "dist/Jarvis Forge.app/Contents/MacOS/JarvisForge"
cat build_app.py ; cat install_app.sh
plutil -p "dist/Jarvis Forge.app/Contents/_CodeSignature/CodeResources" | grep -o "Resources/[A-Za-z_.@]*"
find "dist/Jarvis Forge.app" \( -name "*.py" -o -name "run.py" -o -name "jarvis" \
  -o -name "dashboard" -o -name "*.so" -o -name "*.dylib" \) -print
find "dist/Jarvis Forge.app" -name "*.py" | wc -l
```

### A.2 Launcher path resolution (§2)

```
for p in "/Users/abhijeetmiskin/AppData/aisys/jarvis-forge" \
         "/Users/abhijeetmiskin/AppData/jarv/jarvis-forge" \
         "/Users/abhijeetmiskin/.workbuddy-ai/binaries/python/envs/default/bin/python" \
         "/opt/homebrew/bin/python3" "/usr/bin/python3" "./.venv/bin/python" \
         "/Applications/Jarvis Forge.app"; do [ -e "$p" ] && echo "EXISTS $p" || echo "MISSING $p"; done
ls -la /Users/abhijeetmiskin/AppData/ ; ls -la /Users/abhijeetmiskin/AppData/aisys
which -a python3 ; /opt/homebrew/bin/python3 -V ; /usr/bin/python3 -V ; ./.venv/bin/python -V
grep -n "project_path\|aisys\|sys.path\|Path(__file__)\|PROJECT_DIR\|parents\[" app/launcher.py
shasum -a 256 app/launcher.py "dist/Jarvis Forge.app/Contents/Resources/launcher.py"
```

In-process probe of the **bundled** launcher, under the interpreter the bundle records (wrote a `.pyc`; see Appendix B):

```
cd "…/Contents/Resources" && /Users/…/.workbuddy-ai/…/bin/python -c "
import importlib.util
from pathlib import Path
spec = importlib.util.spec_from_file_location('bundled_launcher', 'launcher.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
print('BUNDLE_ROOT =', m.BUNDLE_ROOT)
for c in (m.BUNDLE_ROOT, m.BUNDLE_ROOT.parent, m.BUNDLE_ROOT.parent.parent):
    print(c, '| run.py:', (c/'run.py').exists(), '| jarvis dir:', (c/'jarvis').is_dir())
try: print('resolve_project_dir ->', m.resolve_project_dir())
except Exception as e: print('resolve_project_dir RAISED:', type(e).__name__, '->', e)
print('recorded project_path.txt exists:', Path(open('project_path.txt').read().strip()).is_dir())"
```

End-to-end launch of the real bundle executable, in a sanitised env, throwaway home, no `osascript` reachable:

```
SANDBOX=$(mktemp -d)/bin && mkdir -p "$SANDBOX"
ln -sf /bin/sh /bin/cat /usr/bin/dirname <…> "$SANDBOX/" ; ln -sf <bundle python> "$SANDBOX/python"
JH=$(mktemp -d)
env -i PATH="$SANDBOX" HOME="$JH" TMPDIR=/tmp JARVIS_HOME="$JH" JARVIS_SELFTEST=1 \
  PYTHONDONTWRITEBYTECODE=1 \
  sh -c "/Users/…/jarv/jarvis-forge/dist/Jarvis Forge.app/Contents/MacOS/JarvisForge" 2>&1; echo EXIT=$?
find "$JH" | head ; rm -rf "$JH"
```

### A.3 Drift (§3)

```
(cd jarvis && find . -name "*.py" -not -path "*__pycache__*" | sort | xargs shasum -a 256) > $TMP/repo_jarvis.txt
(cd "dist/Jarvis Forge.app/Contents/Resources" && find . -path "*jarvis*" -name "*.py" | xargs -I{} shasum -a 256 {}) > $TMP/bundle_jarvis.txt
comm -12 <(awk '{print $2}' $TMP/repo_jarvis.txt|sort) <(awk '{print $2}' $TMP/bundle_jarvis.txt|sort) | wc -l
comm -12 <(sort $TMP/repo_jarvis.txt) <(sort $TMP/bundle_jarvis.txt) | wc -l
find jarvis -name "*.py" -not -path "*__pycache__*" | wc -l
find . -path ./.venv -prune -o -name "__pycache__" -prune -o -type f -newermt "2026-09-25 12:59:00" -print
find jarvis run.py dashboard app \( -name "*.py" -o -name "*.html" -o -name "*.js" -o -name "*.css" \) \
  -exec stat -f "%Sm %N" -t "%Y-%m-%d %H:%M:%S" {} \; | sort -r | head -20
find "dist/Jarvis Forge.app" -type f -exec stat -f "%Sm %N" -t "%Y-%m-%d %H:%M:%S" {} \; | sort | sed -n '1p;$p'
```

### A.4 Signature (§4)

```
codesign -dv "dist/Jarvis Forge.app" ; echo exit=$?
codesign -dvvv "dist/Jarvis Forge.app"
codesign --verify --verbose=4 "dist/Jarvis Forge.app" ; echo exit=$?
codesign -d --entitlements :- "dist/Jarvis Forge.app"
spctl -a -vv "dist/Jarvis Forge.app" ; echo exit=$?
TMP=$(mktemp -d) && cp -R "dist/Jarvis Forge.app" "$TMP/" \
  && rm -rf "$TMP/Jarvis Forge.app/Contents/Resources/__pycache__" \
  && codesign --verify --verbose=4 "$TMP/Jarvis Forge.app" ; spctl -a -vv "$TMP/Jarvis Forge.app"
xattr -l "dist/Jarvis Forge.app" ; xattr "dist/Jarvis Forge.app" ; xattr -r -l "dist/Jarvis Forge.app"
ls -la /Applications/ | grep -i jarvis
```

### A.5 Static build assessment (§5)

```
for t in swiftc clang codesign spctl xattr iconutil sips plutil osascript say; do command -v $t; done
for f in app/make_icon.py app/launcher.py; do [ -e "$f" ] && echo EXISTS || echo MISSING; fi
grep -n "^import\|^from" app/make_icon.py
cat requirements.txt ; cat pyproject.toml
```

### A.6 Interpreter / dependency probes (§1.3, §6)

```
$PY -V ; realpath $PY ; $PY -c "import sys; print(sys.version); print(sys.executable)"
for m in fastapi uvicorn pydantic pydantic_settings httpx multipart sqlalchemy aiosqlite \
         sounddevice numpy PIL psutil dotenv openai pyttsx3 speech_recognition pyaudio AppKit Quartz; do
  $PY -c "import $m"; done        # for both ./.venv/bin/python and the bundle-recorded 3.13.12
for PY in ./.venv/bin/python <3.13.12> /opt/homebrew/bin/python3 /usr/bin/python3; do
  $PY -c "import tkinter; print(tkinter.TkVersion)"; done
JH=$(mktemp -d); PYTHONDONTWRITEBYTECODE=1 JARVIS_HOME=$JH $PY -c "import sys; sys.path.insert(0,'.'); import jarvis.api.app as a; print('OK')"
```

### A.7 P0 re-confirmation (§7.3)

```
for PY in ./.venv/bin/python <bundle 3.13.12>; do
  JH=$(mktemp -d); JARVIS_HOME=$JH $PY -c "
from jarvis.tools import base
print('Verification in base namespace:', hasattr(base,'Verification'))
try: print('ok() ->', base.ok('demo.tool','hello'))
except Exception as e: print('ok() RAISED:', type(e).__name__, '->', e)"; done
sed -n '1,30p' jarvis/tools/base.py ; grep -n "Verification" jarvis/tools/base.py
grep -n "^from\|^import" jarvis/tools/base.py ; grep -n "class Verification" jarvis/core/models.py
grep -rn "return ok(\|= ok(\|await ok(\|return unverified(\|= unverified(" jarvis/ --include="*.py" | wc -l
grep -rln "return ok(\|= ok(\|return unverified(" jarvis/ --include="*.py" | wc -l
```

### A.8 TCC / permission / self-test source (§6)

```
grep -rn "CGPreflightScreenCaptureAccess|CGRequestScreenCaptureAccess|AVCaptureDevice|requestAccessForMediaType|\
kTCCService|_blocked_marker|SCLiveSpeechRecognition|screen_capture|capture_state" jarvis/ --include="*.py" --include="*.swift"
grep -rn "requestAccess|AVCaptureDevice.requestAccess|SFSpeechRecognizer.requestAuthorization|\
CGRequestScreenCaptureAccess|CGPreflightScreenCaptureAccess|TCCPrompt" \
  --include="*.py" --include="*.swift" --include="*.sh" . | grep -v "\.venv"
grep -rn "screencapture|CGDisplay|ScreenCapture" jarvis/ --include="*.py" --include="*.swift"
cat jarvis/voice/native/mic_state.swift ; sed -n '40,75p' jarvis/voice/native/macos_speech.swift
sed -n '100,215p' jarvis/voice/stt.py ; sed -n '360,400p' jarvis/voice/stt.py
sed -n '296,352p' jarvis/voice/audio.py ; sed -n '455,520p' jarvis/voice/audio.py
grep -n "def consent_host" -A 22 jarvis/voice/audio.py
sed -n '105,145p' jarvis/tools/system.py ; sed -n '1,70p' jarvis/vision/screen.py
grep -n "@app.get|@app.post|add_api_route|APIRouter" jarvis/api/app.py | wc -l ; grep -rn "screen" jarvis/api/*.py
grep -rn "JARVIS_SELFTEST|selftest|self-test" --include="*.py" --include="*.sh" --include="*.md" . | grep -v "\.venv|__pycache__"
grep -n "voice_path|JARVIS_HOME|data_dir" jarvis/config.py
cat run.py
```

### A.9 Live dev-server run (§6.1–§6.4) — throwaway home, port pre-checked, process killed

```
lsof -nP -iTCP:8765 ; echo exit=$?                       # -> exit 1, no listener
/usr/bin/python3 -c "import socket; s=socket.socket(); s.settimeout(0.5); \
  print(s.connect_ex(('127.0.0.1',8765)))"               # -> 61 (ECONNREFUSED)
JH=$(mktemp -d)
PYTHONDONTWRITEBYTECODE=1 JARVIS_HOME=$JH nohup ./.venv/bin/python run.py > /tmp/jarvis_dev_server.log 2>&1 &
sleep 9 ; tail -30 /tmp/jarvis_dev_server.log ; lsof -nP -iTCP:8765
TOK=$(cat "$JH/api_token")
curl -sS -m 8  -H "X-JARVIS-Token: $TOK" -w "HTTP=%{http_code} time=%{time_total}\n" http://127.0.0.1:8765/health
curl -sS -m 20 -H "X-JARVIS-Token: $TOK" -w "HTTP=%{http_code}\n" http://127.0.0.1:8765/voice/status
curl -sS -m 8  -o /tmp/dash.html -w "HTTP=%{http_code} bytes=%{size_download} type=%{content_type}\n" \
  -H "X-JARVIS-Token: $TOK" http://127.0.0.1:8765/dashboard
curl -sS -m 8  -o /dev/null -w "HTTP=%{http_code}\n" http://127.0.0.1:8765/
curl -sS -m 8  -H "X-JARVIS-Token: $TOK" http://127.0.0.1:8765/devices
curl -sS -m 8  -o /dev/null -w "HTTP=%{http_code}\n" http://127.0.0.1:8765/health     # no token -> 401
kill -TERM 25193 ; sleep 4 ; ps -p 25193 ; lsof -nP -iTCP:8765 ; tail -12 /tmp/jarvis_dev_server.log
find "$JH" -maxdepth 2 ; rm -rf "$JH"
```

`POST /voice/speak`, `POST /vision/screen` and every tool route were **deliberately not called** — they speak aloud, capture the screen, or mutate state.

### A.10 `.pyc` census (§7)

```
find . -path ./.venv -prune -o -name "*.pyc" -print | wc -l
find . -path ./.venv -prune -o -name "*.pyc" -print | sed -E 's/.*cpython-([0-9]+)\.pyc/\1/' | sort | uniq -c
ls -la app/__pycache__/
for f in app/__pycache__/*.pyc; do strings -a "$f" | grep -i "launcher.py"; done
grep -ao "/Users/abhijeetmiskin/AppData/[a-z]*/jarvis-forge" app/__pycache__/*.pyc
# per-tag old-path counts (jarvis/)
for f in $(find jarvis -name '*cpython-313*.pyc'); do grep -qa AppData/aisys "$f" && echo x; done | wc -l
# PEP 335 header validation: struct.unpack('<II', data[8:16]) vs (int(src.stat().st_mtime)&0xffffffff, st_size)
python3 - <<'EOF'  …(full script quoted in the session log; reports total/by-tag/old-path/match/stale/orphan)…  EOF
find . -path ./.venv -prune -o \( -name "*.pyc" -newermt "2026-09-25 20:00:00" \) -print
```

---

## Appendix B — audit-introduced artifacts (disclosed; NOT cleaned up, removal was blocked)

This audit intended to be strictly read-only. Two side effects occurred and remain on disk. Both are regenerable bytecode; **no source file, Info.plist, pointer file or signature blob was created, modified, or deleted.**

| Path                                                                                                                  | Cause                                                                                                                                                                 | Effect                                                                                                                                                                                                                                                                         |
| --------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `dist/Jarvis Forge.app/Contents/Resources/__pycache__/launcher.cpython-313.pyc` (42 121 B, mtime 2026-09-25 20:10:00) | `importlib.util.spec_from_file_location(…, 'launcher.py')` from inside `Contents/Resources` (§2.3), which wrote a cache for the imported module                       | **Breaks the bundle's code seal as measured**: `codesign --verify` → `a sealed resource is missing or invalid`, `spctl -a -vv` → `a sealed resource is missing or invalid`. This is _not_ the bundle's built state — §4.2 gives the pristine-structure result from a temp copy |
| `jarvis/{tools,core,security}/__pycache__/{base,files,models,policy,paths}.cpython-313.pyc` (5 files, written ≈20:16) | module-import probes under the 3.13.12 interpreter (§7.3) without `PYTHONDONTWRITEBYTECODE=1`. The dev-server run (§A.9) _did_ set it and wrote nothing into the repo | Inert. Normal cache locations. The 3 sources were edited at 16:56–18:26 today, so these caches are freshly valid for current code and displace older revisions                                                                                                                 |

`rm -rf` of the in-bundle cache was attempted to restore the pre-audit state and was **blocked by the permission system** (correctly, as a write to the tree under a read-only mandate). It was not retried or worked around. The §4.2/§4.3 numbers reported as the bundle's state are the temp-copy measurements, which are the bundle's true as-built state.

Operator cleanup (one line; restores the audited artifact and drops 5 regenerable caches):

```
rm -rf "dist/Jarvis Forge.app/Contents/Resources/__pycache__" \
       jarvis/tools/__pycache__/base.cpython-313.pyc \
       jarvis/tools/__pycache__/files.cpython-313.pyc \
       jarvis/core/__pycache__/models.cpython-313.pyc \
       jarvis/security/__pycache__/policy.cpython-313.pyc \
       jarvis/security/__pycache__/paths.cpython-313.pyc \
 && codesign --verify --verbose=4 "dist/Jarvis Forge.app"   # expect: valid on disk / satisfies its DR
```

This clears the 20:00+ caches only. The 300 pre-existing old-path caches of §7 are unrelated to this audit and are a separate cleanup decision.

---

## Appendix C — what remains UNVERIFIED

| Item                                                                            | Why                                                                                                   | Command that would settle it                                                                                      |
| ------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| `build_app.py` builds                                                           | explicitly not executed per brief                                                                     | `python build_app.py --dest "$(mktemp -d)"` (never default `--dest`: `build()` `rmtree`s `dist/Jarvis Forge.app`) |
| Self-test result (the "7/9" claim)                                              | unreachable on the built bundle (P0-D3)                                                               | rebuild, then `JARVIS_HOME=$(mktemp -d) JARVIS_SELFTEST=1 "…/Contents/MacOS/JarvisForge"`                         |
| Bundled GUI, Restart/Quit buttons, clean shutdown via `ServerController.stop()` | needs a launchable bundle; the server itself was verified (SIGTERM → "Application shutdown complete") | `JARVIS_HOME=$(mktemp -d) open -n "dist/Jarvis Forge.app"`                                                        |
| Microphone / Speech actually prompting post-fix                                 | nothing calls `requestAccess` (P1-D1) and the STT helper SIGABRTs unbundled                           | grant-free path: read `/voice/status` `capture_state` from a bundled run; STT: self-test's `transcribe()` leg     |
| `screencapture -x` behaviour when Screen Recording is denied                    | would capture the user's screen                                                                       | `screencapture -x /tmp/t.png; echo $?; ls -l /tmp/t.png` on a denied host, plus a pixel-variance check            |
| Whether TCC attributes the server subprocess to `Jarvis Forge.app`              | bundle won't start; `consent_host()` heuristic is wrong either way (P2-D2)                            | bundled run + `tccutil reset Microphone com.jarvisforge.app`, then observe `capture_state`                        |
| End-to-end P0 `NameError` through the HTTP server                               | tool routes mutate state / trigger actions                                                            | `POST /command {"text":"take a screenshot"}` with a throwaway `JARVIS_HOME` and read the audit row                |
| Whether a rebuild restores the pointer                                          | static reading only                                                                                   | rebuild to temp, then `xxd "$TMPDIR/…/Resources/project_path.txt"`                                                |
