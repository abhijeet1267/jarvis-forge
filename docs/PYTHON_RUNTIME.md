# Python Runtime

The Swift app at `macos/Vantage/Vantage.app` carries its own CPython 3.11
interpreter. It is relocatable: copied to another Mac, or with this checkout
deleted, it still starts its backend. This is the document for how that
interpreter is produced and how to change it.

It does **not** apply to `dist/Vantage.app` (the `build_app.py` bundle) or to
the Android client. Both are separate concerns.

## Why it exists

Before this, `Contents/Resources/python_path.txt` named
`/Users/<someone>/AppData/Vantage/.venv/bin/python`. The *packages* travelled
with the bundle but the interpreter did not, so the app only worked on the
machine that built it, from the checkout it was built in. `docs/STANDALONE.md`
recorded that limitation; this removes it.

## What is shipped

| Field | Value |
|---|---|
| Source | [python-build-standalone](https://github.com/astral-sh/python-build-standalone) (astral-sh) |
| Release tag | `20261110` |
| Python | `3.11.18` |
| Target | `aarch64-apple-darwin` (Apple Silicon) |
| Asset | `cpython-3.11.18+20261110-aarch64-apple-darwin-install_only.tar.gz` |
| URL | `https://github.com/astral-sh/python-build-standalone/releases/download/20261110/cpython-3.11.18%2B20261110-aarch64-apple-darwin-install_only.tar.gz` |
| SHA-256 | `73f1ed14486aa278d331e95f7d6d452f5cfb69d90b418e26e62fd4846d53557d` |
| Installed size | ~41 MB (pruned), ~71 MB unpruned (`--keep-full-stdlib`) |
| Placement | `Contents/Resources/python/` |
| Recorded in | `Contents/Resources/python_path.txt` -> `<bundle>/Contents/Resources/python/bin/python3.11` |
| Marker | `Contents/Resources/python/.vantage-python-runtime` |

The SHA-256 was downloaded from that URL and then cross-checked against the
`digest` field the GitHub release API reports for the same asset, so the pin is
not merely a hash of whatever arrived. Version and hash are pinned in
`scripts/bundle_python.sh`; the marker file records what was installed so a
second run can tell whether it is already current.

Python **3.11 is not negotiable**: the staged packages contain
`pydantic_core/_pydantic_core.cpython-311-darwin.so`, an ABI-tagged extension
module. A 3.12+ interpreter refuses to load it. Someone measured 3.15 failing
for exactly this reason.

## Options considered

1. **python-build-standalone** (chosen). Prebuilt, statically linked, upstream
   calls it relocatable, and it is the maintained artefact for this job. No
   per-binary surgery, no build step of our own, and an exact pinned hash.
2. **System framework copied + `install_name_tool` re-pointing.** Would make the
   bundle depend on whatever CPython ships with the host's developer tools, and
   would need `@rpath` rewriting on every `.so`, re-signing each one, and a
   re-check after every macOS update. Rejected as fragile.
3. **A venv with absolute symlinks rewritten.** This is the failure being fixed:
   a venv's `pyvenv.cfg` and script shebangs point at the interpreter that
   created it. Rewriting them is a patch over a design that assumes a fixed
   path, and it still leaves no interpreter in the bundle.

## What the script does

`scripts/bundle_python.sh`:

1. Refuses to run on anything but arm64 macOS; the staged extension modules are
   arm64, so an x86_64 interpreter beside them would fail at import.
2. Downloads the pinned tarball to `~/.cache/vantage/python-build-standalone/`
   (outside the repository; 27 MB is not source), with retries — the release CDN
   was observed resetting the connection mid-transfer — and verifies the
   SHA-256 before extracting. A cached tarball with the right hash means a
   rebuild needs no network.
3. Extracts into a private `<runtime>.staging.<pid>/` directory; nothing runs
   with that directory as the working directory.
4. Prunes payload the backend cannot use: `idlelib`, `turtledemo`, `lib2to3`,
   `ensurepip`, `tkinter` and the tcl/tk dylibs, `pip`/`setuptools`, headers,
   man pages, `config-*/`, `pkgconfig`, and `libpython3.11.dylib`. Saves ~30 MB.
   `--keep-full-stdlib` skips this for debugging.
5. Replaces the two build-machine absolute paths recorded in
   `_sysconfigdata__darwin_darwin.py` (`/install` -> `@PYTHON_RUNTIME@`,
   the upstream build root -> `@UPSTREAM_BUILD@`). CPython overrides the prefix
   at runtime from `sys.prefix`; these values are only read by build tooling.
   They are marked rather than rewritten to a plausible relative path so nobody
   mistakes them for something that resolves.
6. Precompiles the stdlib with `-I`, `UNCHECKED_HASH` and file paths relative to
   the runtime root. A signed bundle must not gain `__pycache__` directories on
   first run: writing into `Contents/Resources` invalidates the seal
   `codesign --verify --strict` just produced.
7. Moves the finished tree into place, writes the marker and
   `python_path.txt`, then verifies.

The verification step is the acceptance test, and it runs on every invocation
including the "already current" fast path:

```
<runtime>/bin/python3.11 -c "import fastapi, pydantic_core, ssl, sqlite3"
```

with only `Resources/site-packages` on `PYTHONPATH`, then asserts a non-empty
CA store and a working `sqlite3` round-trip, prints `sys.executable` and
`sys.prefix`, and greps the tree for this checkout's path and for the recorded
`/install` prefix.

## Why each pruned piece is safe

* `tkinter` / tcl / tk: nothing under `jarvis/`, `run.py` or `dashboard/` imports
  `tkinter`. Only `app/launcher.py` does, and that is the `build_app.py` bundle,
  which does not use this runtime.
* `pip` / `setuptools`: the bundle installs nothing at runtime. Keeping them adds
  ~10 MB and a `.pth` that mounts the setuptools distutils shim at every start.
* `libpython3.11.dylib`: not a linker dependency of anything in the tree. The
  interpreter is built `--enable-static-libpython-for-interpreter`, and the
  extension modules use `-undefined dynamic_lookup`. Verified with `otool -L`
  over every Mach-O file, and additionally guarded by the import check — if a
  future package really linked it, the script would fail rather than ship a
  runtime that cannot import.
* `lib/python3.11/site-packages` is kept, empty, because `sysconfig` names it as
  `purelib`. The app's packages come from `Resources/site-packages` through
  `PYTHONPATH`, which `BackendManager` already sets.

## Measured behaviour (2026-11-17, macOS 26.3, arm64)

```
$ <bundle>/Resources/python/bin/python3.11 -c "import fastapi, pydantic_core, ssl, sqlite3; print('ok')"
ok
sys.executable: <bundle>/Contents/Resources/python/bin/python3.11
sys.prefix:     <bundle>/Contents/Resources/python
python:         3.11.18
fastapi:        0.162.7
pydantic_core:  2.50.4
openssl:        OpenSSL 3.5.9 29 Sep 2026
sqlite:         3.53.1
ssl+sqlite:     functional
```

* `sys.executable` and `sys.prefix` are inside the bundle; the host venv is not
  on the path.
* `grep -rIl 'Users/abhijeetmiskin'` over `Resources/python` finds nothing, and
  the same for `/tmp/tmpa34w8s9s` and the `/install` forms the build recorded.
* Copying the whole `.app` to `/tmp` and running its interpreter and
  `run.py serve` there produced `/health -> {"status":"ok","tools":200,...}`.
* `codesign --force --deep -s -` then `codesign --verify --strict` succeed with
  the runtime in place, and importing `fastapi` afterwards does not write a
  `.pyc` into the tree or disturb the seal.
* `tests/test_api_contract.py tests/test_companion_gateway.py
  tests/test_backend_lifecycle.py` -> **89 passed, 1 skipped** under the bundled
  interpreter, matching the venv's 89 passed / 1 skipped. Playwright's
  `test_phase92_browser_automation.py` -> 28 passed. Both runs needed the venv
  for test-only dependencies (pytest, playwright), which the bundle deliberately
  does not stage.
* `--force` rebuild takes ~2.3 s from cache.

Not verified: launching the GUI app from Finder and letting it start this
interpreter end to end. The interpreter, the backend under it, and the bundle's
signature are verified; the Finder launch path is not.

## Rebuild and update

```bash
scripts/bundle_python.sh                    # into macos/Vantage/Vantage.app
scripts/bundle_python.sh --app /path/to/App.app
scripts/bundle_python.sh --force            # rebuild even when current
scripts/bundle_python.sh --keep-full-stdlib # no pruning (debugging)
```

The script is idempotent: with an intact runtime and the marker matching the
pinned version and hash, it reports `Runtime already current`, re-runs the
verification, rewrites `python_path.txt` and exits 0.

To move to a newer python-build-standalone release, update `PBS_TAG`,
`PBS_PY_VERSION`, `PBS_URL` and `PBS_SHA256` at the top of the script. Keep the
Python version at 3.11 unless the staged packages are rebuilt: the
`cpython-311` ABI tag in `pydantic_core` is what enforces it. Re-run with
`--force` and confirm the verification output above.

## Wiring into the build

`macos/Vantage/assemble_app.sh` already invokes this before signing:

```bash
"${PROJECT_DIR}/scripts/bundle_python.sh" --app "$(pwd)/${APP_BUNDLE}"
```

It must stay **before** `codesign`: the script writes into `Contents/Resources`,
and a signature computed first would not cover the interpreter. `assemble_app.sh`
currently treats a bundling failure as a warning, because without a network the
build can still produce an app that runs from this machine's `.venv`. If that
fallback stops being acceptable, make the call fatal.

## Known limits

* **arm64 only.** `uname -m` is checked and the script refuses otherwise. An
  Intel build would need the matching `x86_64-apple-darwin` asset *and* x86_64
  wheels for every staged package.
* **The download is a build-time dependency.** With no cache and no network,
  the script fails loudly rather than pretending; the `.app` then keeps whatever
  `python_path.txt` the staging step wrote, which is this checkout's venv.
* **No `ssl` CA bundle of its own.** `ssl` uses the platform store
  (`/etc/ssl/cert.pem`), which is what macOS ships. `certifi` is staged for
  libraries that want it. Nothing in the bundle pins a CA file, so a host with a
  broken keychain would surface as a TLS failure, not a missing file.
* **Test-only packages are not staged.** `pytest` and `playwright` are
  deliberately excluded; the bundled interpreter cannot run the test suite by
  itself.