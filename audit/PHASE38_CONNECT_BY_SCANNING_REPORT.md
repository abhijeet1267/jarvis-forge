# Phase 38 — connect by scanning, and why the address was dead

**Date:** 2026-10-02 → 10-03 (it ran past midnight) · **Closes:** "http://172.20.169.34:8765/phone not working"
**Adds:** a pairing address and scan code in the laptop's own window

## 1. Root cause, established before any fix was attempted

The report was that the phone could not reach the laptop. Four measurements, in order:

```
lsof -nP -iTCP:8765 -sTCP:LISTEN   ->  nothing listening
ps | grep Vantage|JarvisForge|run.py ->  no server process at all
ipconfig getifaddr en0             ->  172.20.169.34   (the URL was correct)
socketfilterfw --getglobalstate    ->  Firewall is disabled
```

Nothing was wrong with the code, the address, the host check or the network. **The laptop was
not running the server.** I had demonstrated that URL in Phases 35–37 against test servers I
started myself, and then killed every one of them during cleanup (`pkill -f "python run.py"`).
The user was handed an address that had been alive only while my process was.

That is a documentation and product failure, not a defect: a URL that works during a
demonstration and is dead by the time anyone tries it is worse than no URL, because the reader
has no way to tell which they have. Fixed by starting the app itself, and by §3.

## 2. What was changed

**The server is the single source of "what to type".** `/health` gained `phone_urls`, answered
from `Settings.reachable_urls()` — the same list that decides which `Host` headers get through.
The desktop panel asks the server instead of deriving an address from `socket.getaddrinfo`
itself, because two derivations drift, and one of them already did (Phase 35 advertised an
address its own origin gate then refused).

**The panel shows the address, and a scan code beside it.** `pairing_qr_png()` draws the URL as
a PNG when the optional `qrcode` library is installed, and returns `None` when it is not — in
which case the address is still shown as text, because typing a URL is a worse experience than
scanning one but a missing optional library is not a reason to break a window. `qrcode` was
deliberately **not** added to `requirements.txt`: this project advertises stdlib-first, and an
untested fallback is a crash waiting for the next clean install, so the fallback is tested
instead (§3).

**The pairing URL is the LAN one, never loopback.** `lan_phone_url()` skips `127.0.0.1`,
`localhost` and `::1` — an address a phone cannot use is not a pairing instruction, it is a
teaser.

## 3. Tests (`tests/test_phase38_scan_to_connect.py`, 6)

Red first: 5 failed, then 2, then 1, each for a different real reason recorded below.

- The scan code is a real PNG (magic bytes, and large enough to be scannable).
- **Tk can actually display it** — the claim worth testing is not "a PNG exists" but "this
  toolkit renders this image", so the test loads the bytes into a `PhotoImage` and checks size.
- A machine without the library still gets the address: `__import__` blocked for `qrcode`,
  `pairing_qr_png` returns `None` rather than raising.
- The panel draws an image when one is available, and shows `http://172.20.169.34:8765/phone`
  as text — asserting explicitly that the loopback URL is *not* what it offers.
- `/health.phone_urls` equals `Settings.reachable_urls()`, so the panel and the host gate
  cannot disagree.

## 4. Three findings from making this test pass

1. **`tk._default_root` is only set for the first interpreter in a process.** The panel tests
   passed alone and failed in a full run: the walk started from an earlier test's empty window.
   Fixed by capturing the root `build_ui` actually creates (a recorded `Tk` subclass) rather
   than trusting the module global. The failure looked exactly like "the feature is broken",
   and was the test not knowing which window it was reading.
2. **My fix for that made it worse, and the crash was real.** I added a fixture that destroyed
   the Tk interpreter between tests; the suite then died with `exit=133` (SIGTRAP) inside
   `libtcl9.0.dylib`. The macOS crash report named the faulting library, which is the only
   reason this was diagnosable at all — a Python-level traceback would have shown nothing,
   because the process did not raise, it aborted. The destroy was removed; the root capture
   above was the actual fix. **Deleting an interpreter you did not create is not isolation.**
3. **`PhotoImage` binds to the first interpreter unless told otherwise.** With the correct root
   captured, the image still did not appear: the image object belonged to another interpreter
   and the label rejected it, and `refresh()`'s own `except Exception` swallowed the
   `TclError` into a status line. `master=pair_image` fixes it. Worth noting the shape of the
   bug: a silent catch turned a constructor error into "nothing happened".

## 5. Live, on the user's own install

`~/.jarvis-forge/lan_mode` written (the switch the checkbox owns), then `open dist/Vantage.app`:

```
lsof -iTCP:8765 -sTCP:LISTEN  ->  python3.1  *:8765      (all interfaces)
GET http://172.20.169.34:8765/phone              -> 200
GET http://172.20.169.34:8765/manifest.webmanifest -> 200
GET /health (with the token) ->
    phone_urls: ['http://127.0.0.1:8765/phone', 'http://172.20.169.34:8765/phone']
```

The window therefore shows `phone: http://172.20.169.34:8765/phone` with a code beside it, and
the phone has only to scan or tap it. **What is not verified:** the QR being read by a physical
phone, and the phone's own behaviour — there is no handset here. The panel's image is proven by
the widget carrying a rendered image, not by a photograph of the screen.

## 6. A mistake worth recording

Rebuilding the bundle while the app was running from it (`build_app.py` does
`shutil.rmtree` on the bundle directory) **killed the user's running app** — its interpreter and
source files vanished out from under it. This happened once, mid-phase, and is why the app had
to be relaunched. A build step that deletes the artifact another process is executing is not
safe to run against a live install; the rebuild should follow a quit, or build elsewhere and
swap.

## 7. Re-measured

| | before | after |
|---|---|---|
| tests | 1718 | **1724 passed · 0 failed · 0 skipped** |
| test modules | 60 | **61** |
| reports | 39 | **40** |
| API routes | 108 | 108 (`phone_urls` is a field, not a route) |
| connecting from a phone | type an address, if a server happens to be running | the laptop shows the address and a scan code; the app was started and verified listening |

## 8. Carried forward

* **38-A** the scan code only appears if `qrcode` is installed (`pip install qrcode`); without
  it the panel shows the address alone. Deliberate, but it means the "just scan it" experience
  is opt-in on each machine.
* **38-B** the address changes when the router reassigns it. A scan code absorbs that today;
  nothing advertises the laptop via mDNS yet, which is what would make the connection survive
  an IP change without the laptop being opened again.
* **38-C** `refresh()` catches every exception into a status line — the reason finding 4.3 was
  invisible for as long as it was. Narrowing that catch is a real robustness item.
* **38-D** the app died when its bundle was rebuilt underneath it; the build should refuse to
  run against a live install rather than rely on the operator's care.
* Unchanged from before: 37-A…E, 36-A…E, 35-A…C, 34-A…C, and the `-rs` CI change that would
  name the runner's six skips.

## 9. Git

Parent `b2e82a5`. Committed: `app/launcher.py`, `jarvis/api/app.py`,
`tests/test_phase38_scan_to_connect.py`, `README.md`, `benchmarks/*`, `assets/*.svg`,
`dist/Vantage.app` rebuild (not committed — build artifact), this report.

**CI verdict:** `37048651947` on `6e44d22` finished **success** on both matrix legs —
`1715 passed, 9 skipped` each, which closes against this machine's 1724 collected (1715 + 9).

The skips rose from 6 to 9, and the three new ones are this phase's: the file contains exactly
three `pytest.skip` sites, all gated on the optional `qrcode` package, and the runner installs
`requirements.txt`, where `qrcode` deliberately is not. So the runner is exercising the
no-library fallback by skipping the code-drawing tests — consistent with the design, though
stated as consistency rather than proof: the workflow still runs `python -m pytest -q`, so no
skip reason reaches the log, which remains the standing `-rs` open item.

