# Phase 36 — two applications: one on the laptop, one on the phone

**Date:** 2026-10-02 · **Closes:** 35-D (no way to open the door from the app), and the
"the .app is a shortcut, not an application" limitation recorded while packaging Phase 35
**Refuses to pretend to close:** an Android APK, and Chrome-on-Android installability over a
self-signed certificate (measured below)

"Make 1st two applications 1 for mobile and 1 for laptop so i can connect and work."

## 1. Measured before anything was changed

| Half of the ask                       | Reality, measured                                                                                                                                                                                                                                                                                                       |
| ------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| a laptop application                  | `build_app.py:125` copied **no source**: the bundle held `launcher.py` plus `project_path.txt` containing `/Users/abhijeetmiskin/AppData/jarv/jarvis-forge`. The `.app` was a pointer at this checkout — the Phase 10 defect (an app that exits 1 because its recorded directory moved) still shipped, 26 phases later. |
| connecting from it                    | 35-D: nothing in `app/launcher.py` set LAN mode, and a Finder-launched app never sees `export JARVIS_LAN_MODE=1`. So the phone work of Phase 35 was unreachable from the app a user would actually double-click.                                                                                                        |
| a mobile application                  | No Android SDK, no `adb`, no device attached. What _is_ installable without a build chain is a web app the browser itself installs — which needs a manifest, icons and a service worker. None existed.                                                                                                                  |
| and does a browser accept it on a LAN | Unmeasured going in. Measured in §4: it depends on the certificate, and Chrome says no.                                                                                                                                                                                                                                 |

## 2. What changed

**The bundle carries its own code.** `build_app.py:stage_project()` copies `jarvis/`,
`dashboard/`, `run.py`, `requirements.txt`, `pyproject.toml` and `.env.example` into
`Contents/Resources/app/`, skipping `.venv`, `tests`, `dist`, `build`, `.git`, `node_modules`
and every `__pycache__`. `launcher.resolve_project_dir()` now prefers that staged copy over the
recorded absolute path, so the application travels. The copy step is a function rather than an
inline `shutil` call precisely so it can be tested without codesigning anything.

**The laptop has a switch of its own.** `jarvis/config.py` gained `lan_mode_state()` /
`set_lan_mode()`: one word in `<home>/lan_mode`, read as a boolean and never evaluated. The
control panel gained a checkbox, _"Allow devices on my wifi to connect"_, which writes it and
restarts the server. The launcher deliberately does not import `jarvis.config` — it has to work
when the thing it manages is broken — so both sides are pinned to the same file by
`test_the_apps_switch_is_the_same_file_the_server_reads`.

**The switch moves the bind, not the link.** `ServerController` now separates `host` (what the
panel and browser use — `127.0.0.1`) from `bind_host` (what the socket binds — `0.0.0.0` when the
switch is on). Before this, a laptop that bound loopback could not be reached by a phone however
the user had configured the app, and a panel that opened `http://0.0.0.0:8765` is a broken link.

**The phone has the three things that make it an app.** `dashboard/manifest.webmanifest`
(standalone, portrait, name, icons), two PNGs rendered from the app's own `make_icon.draw_artwork`
— not a placeholder — and `dashboard/sw.js`, served from the site root because a worker's scope is
its own directory and `/phone/sw.js` could never intercept the API calls the page makes. The worker
caches the shell and nothing else: a stored battery level or a stored confirmation would be
indistinguishable from a live one, which is the failure this whole program exists to prevent. The
page links them and registers the worker behind a feature check.

**No HTTP route can open the door.** A test asserts that `/settings`, `/config`,
`/admin/settings` and `/system/lan` all refuse a `lan_mode` write. The phone is served the same
dashboard as the laptop, so an endpoint that accepted that key would let a connected device grant
itself remote access to the machine it is talking to.

## 3. Live: the moved bundle, driven the way the app drives it

Copied to `/tmp/p36move`, `project_path.txt` overwritten with `/i/do/not/exist/checkout`, and
launched through its own `ServerController` with only `<home>/lan_mode` present — no env vars:

```
project resolved : /private/tmp/p36move/Jarvis Forge.app/Contents/Resources/app
link shown       : 127.0.0.1        bind chosen: 0.0.0.0
server ready     : True             token written: True
```

Its self-test on the same moved copy: `7/9 checks passed`, `project directory` ok, `server` ok
with **128 tools**, and the two failures the known macOS microphone/speech permissions. The
Phase 10 scenario — move the app, or delete the checkout — now resolves inside the bundle.

## 4. The browser said no, and it mattered that I asked

HTTPS was added (`https_enabled`, `run.py` passing uvicorn the cert from the repo's existing
`transport/tls.ensure_certificate`) on the assumption that a secure context would make the phone
app installable. Then it was measured in Chrome at `https://172.20.169.34:8799/phone`, clicking
through the `NET::ERR_CERT_AUTHORITY_INVALID` interstitial the way a user would:

```
isSecureContext            true
navigator.serviceWorker    present
manifest link              present
serviceWorker.register()   SecurityError: ... An SSL certificate error occurred
                           when fetching the script
beforeinstallprompt        never fired (2.5 s)
```

So: **accepting the certificate warning is not enough.** The page loads, logs in (`/auth/session` 200) and polls `200` forever after — but Chrome will not register a worker for a script whose
origin it does not trust, and without a worker there is no install prompt. What that leaves, stated
without padding:

- **iPhone** — installs today over plain http on the LAN. iOS offers _Add to Home Screen_ without
  demanding a secure context, and the `apple-mobile-web-app-capable` / `-title` / status-bar tags
  added here make it launch standalone. Verified: the tags are in the served page. **Not verified:
  the install itself, because there is no iPhone here.**
- **Android** — works as a page over http or https; installs as an app only against a certificate
  the handset trusts, which a self-signed LAN cert is not. Real options are a domain with a proper
  certificate, or a packaged APK, and this machine can build neither.
- **The encryption is still worth having** — with `JARVIS_HTTPS_ENABLED=true` the session
  credential stops crossing the wifi in cleartext, which was a named issue at the end of Phase 35.

The page's worker registration is guarded, and a test runs that shipped snippet under Node with
`navigator = {}` — the exact state measured here — to prove the page survives it rather than
throwing at startup.

## 5. A guard that refused the configuration it exists to protect

Running the built app found this, and no test had: `lan_startup_problems()` demanded a non-empty
`api_token`, but on a first start the token is generated _during_ startup. A fresh install that
turned wifi access on was therefore refused by a check for a file it was about to create — the
guard blocked the one configuration it is meant to make safe. It now refuses when authentication
is off, which is the actual danger. The Phase 35 test that pinned the old wording still passes;
the new test asserts a fresh home with no token file starts cleanly.

## 6. Regressions

`tests/test_phase36_two_applications.py` — 27 tests, written red first (14 failed, 4 passed
because they described behaviour that already held). Covered: the switch file's three states and
its precedence against an explicit env value; the switch not bypassing the startup guard; the
switch file being read as a word and not evaluated; the app/launcher agreement on the file;
bind-vs-link separation in both directions plus an explicit shell bind winning; staging copying
`run.py`/`jarvis`/`dashboard` while excluding `.venv`, `tests`, `dist` and `__pycache__`; a moved
bundle resolving inside itself; the manifest's fields; every promised icon served as a real PNG
(magic bytes checked, not just a 200); `start_url` answering; the worker's precache list containing
no API path; the iOS standalone tags; the page surviving a browser with no worker; and no HTTP
route accepting a `lan_mode` write.

```
tests/test_phase36_two_applications.py    27 passed
full suite                                1710 passed · 0 failed · 0 skipped · 42 s
§45 safety matrix                         6 of 6 (unchanged)
```

One regression was caught by an existing guard rather than by me: `test_bundled_launcher_has_not_
drifted_from_source` went red because `dist/` still shipped the pre-Phase-36 launcher — the drift
check earning its keep, and the reason the rebuild is part of this phase rather than an afterthought.

## 7. Re-measured

|                                            | before                            | after                                                                                                           |
| ------------------------------------------ | --------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| tests passing                              | 1683                              | **1710**                                                                                                        |
| API routes                                 | 105                               | **108** (manifest, worker, icons)                                                                               |
| tools / confirmation-gated                 | 128 / 27                          | 128 / 27 (unchanged)                                                                                            |
| test modules / reports                     | 59 / 37                           | **60 / 38**                                                                                                     |
| a built `.app` that runs after being moved | no — it exited on a stale pointer | **yes, verified from `/tmp`**                                                                                   |
| opening the door from the laptop app       | required a terminal               | **a checkbox**                                                                                                  |
| phone install                              | a browser tab                     | manifest + icons + worker; iOS standalone tags. Chrome install measured as blocked by the untrusted certificate |

## 8. Carried forward

- **36-A** Android installability needs a certificate the handset trusts. The self-signed path
  encrypts but does not install; nothing here can produce or test a trusted cert or an APK.
- **36-B** the iOS install is asserted from the tags the page serves, not from an iPhone.
- **36-C** the checkbox restarts the server; a user mid-command sees a few seconds of nothing, and
  the panel does not yet show _why_ a restart is happening.
- **36-D** `python_path.txt` still records an absolute interpreter, so a bundle moved to another
  machine needs a Python with the requirements installed — the source travels, the runtime does
  not. Embedding an interpreter is a different project (and the reason "self-contained" above is
  about code, not about the whole environment).
- **36-E** the service worker precaches by exact path list; a future second client page has to be
  added to it or it silently never caches.
- Still open from before, untouched: 35-A/B/C, 34-A/B/C, 32-A/B/C, 33-A/B/C, 30-B/C/D, 29-A/C/D,
  28-A (synonym half), 27-A/B/C, 26-B, 23-E, hardware BLOCKED, LLM planner, unattended workflows,
  and the `-rs` CI change that would finally name the runner's six skips.

## 9. Git

Parent `de4f4a7` (Phase 35 addendum). Committed: `jarvis/config.py`, `jarvis/security/auth.py`,
`jarvis/api/app.py`, `run.py`, `build_app.py`, `app/launcher.py`, `dashboard/phone.html`,
`dashboard/manifest.webmanifest`, `dashboard/sw.js`, `dashboard/icon-192.png`,
`dashboard/icon-512.png`, `.env.example`, `benchmarks/*`, `assets/*.svg`,
`tests/test_phase36_two_applications.py`, `README.md`, this report.

**CI verdict:** `37014255915` on `5050c46` finished **success** on both matrix legs —
`1704 passed, 6 skipped` on 3.11 and the same on 3.12, which closes against this machine's 1710
collected (1704 + 6), so the runner is running the whole suite. The six skips remain unnamed: the
workflow runs `python -m pytest -q`, so no `SKIPPED` reason reaches either log (36-A…E stand; the
`-rs` change is still an open item, not something to edit from a feature phase). Both legs took ~10m53s against 6m14s on the Phase 35 run. The obvious suspect — the new staging
test copying `jarvis/` and `dashboard/` into a temp directory — was measured and cleared: 0.09 s
for all three staging tests. The cause is **not identified here**, and no substitute was invented
for it.
