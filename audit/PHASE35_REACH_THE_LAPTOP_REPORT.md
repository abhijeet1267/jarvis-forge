# Phase 35 — reach the laptop

**Date:** 2026-10-02 · **Closes:** the user's request that a phone "connect them and check",
plus the ask "I can give command that check qoder is working"
**Refuses to pretend to close:** the Android APK and the Windows app (see §7)

The request was for apps on Android, Mac and Windows that connect to the laptop, act on the
laptop from a sentence, and answer whether Qoder is working. Before writing anything, each half
was measured against what this machine can actually build and verify.

## 1. Measured before anything was changed

| Part of the ask | Reality, measured |
|---|---|
| "connect them" — a phone reaching the API | `security/auth.py:132` accepted a `Host` only from `LOOPBACK_HOSTS`; `config.py:36` binds `127.0.0.1`. A phone on the same wifi was refused twice over. |
| a client to connect *with* | `devices/pairing.py` has request/pending/approve/secret; `api/app.py` exposes `/devices/pair/*`, `/confirmations`, `/command`, `/devices/{id}/endpoint`. **No page anywhere spoke them.** The protocol had a server side and no client side. |
| "check qoder is working" | `/Applications/Qoder.app` and `~/.qoder/bin/qoder-computer-use/` exist; nothing in 127 tools could ask about them. |
| an Android install | No Android SDK, no `adb`, no device attached: `ls ~/Library/Android 2>/dev/null` → nothing. An APK cannot be built, signed, installed or verified here. |
| a Windows build | Platform is `darwin/arm64`. A `.exe`/`.msi` produced here could not be run once, so it could not be truthfully called working. |
| voice on "any sound, command which i set" | `voice/wakeword.py` matches configurable phrases **against transcripts**; `voice/audio.py` probes the microphone. There is no always-on audio detector, and Microphone + Speech Recognition need a TCC grant this session cannot request. |

So the deliverable that exists today is the laptop's own door and a page on the other side of it.
The rest is written down as what is missing rather than shipped as a file that has never run.

## 2. What changed

**The door (`lan_mode`, opt-in, `config.py:54`).** `host_allowed` keeps its purpose — it exists
because a browser can be tricked into resolving an attacker's domain to this machine — and stops
being loopback-only: with LAN mode on it accepts exactly the addresses this machine owns
(`interface_addresses()`), and never a hostname. A DNS-rebinding request still dies at 400.

**A guard that runs.** `lan_startup_problems()` existed and was tested and was called by nobody:
the config could be as unsafe as it liked and the server would boot. It is now consulted in
`lifespan`, first thing, and the process refuses (`RuntimeError`, non-zero exit). `run.py` is the
entry point the desktop app uses, so the guard was proven there and not only under `TestClient`.

**The address to type.** `host = 0.0.0.0` is a bind instruction; nobody can put it in a browser.
`Settings.client_hosts()` is now the single source for both the URLs printed at startup and the
origins the API accepts. It first printed `http://fe80::834:22c2:9066:2805:8791/phone` — a
link-local IPv6 with no zone id, in a form that is not even a valid URL host — which is why the
list excludes link-local and brackets the rest.

**The phone's page (`/phone`, `dashboard/phone.html`).** Pair, one command box, and the pending
confirmations with Approve/Deny, speaking the endpoints that already existed rather than a new
parallel ones. It spends the single-use bootstrap the way the dashboard does; the master token is
never in the page.

**"Is Qoder working" (`tools/agents.py`).** READ tier, no confirmation, refuses a name carrying a
path or a newline, and answers the three things actually asked — installed, which version, running
now — from `mdls` and `ps`. When `mdls` gives nothing it returns `unverified`, not a guess.
Registered first in the spec list and reachable from a sentence via the planner's new
`_agent_status` rule.

## 3. Live, over HTTP, on a launched server

`JARVIS_LAN_MODE=1 JARVIS_HOST=0.0.0.0 JARVIS_PORT=8791 python run.py`, real temp `JARVIS_HOME`:

```
JARVIS Forge core -> http://127.0.0.1:8791/dashboard
Phone client           -> http://127.0.0.1:8791/phone
Phone client           -> http://172.20.169.34:8791/phone

/phone   via 127.0.0.1                  200
/phone   via the laptop's LAN address   200
/phone   with Host: evil.example.com    400      <- the rebinding defence still holds
/command with no credential             401
POST /command "is qoder working"        "Qoder is installed (version 0.3.4) and is running."
                                       {"found":true,"version":"0.3.4","running":true,
                                        "path":"/Applications/Qoder.app"}
/devices/pair/request → /devices/pair/pending    PAIR-fe78e886… listed as pending
```

The refusal path, also live: the same command with `JARVIS_HOST=127.0.0.1` exits non-zero and
prints the sentence telling the user which variable to change.

## 4. The browser drive, and the three defects only it could find

Phase 25 §7 closed with **"LIVE BROWSER DRIVE: NOT PERFORMED."** This phase performed one:
Chrome (via DevTools) opened `http://172.20.169.34:8791/phone` — the laptop's real LAN address —
and used it. Findings, in the order they appeared:

1. **`POST /auth/session` → 403 "Origin not allowed."** The page's first call carried the origin
   the server had just printed, and the API refused it, because `allowed_origins()` only ever
   contained `http://{host}:{port}` with `host` = `0.0.0.0`. Advertising an address and refusing
   requests from it is worse than not opening the door. Fixed by deriving both from
   `client_hosts()`. Every later request in that first session was a 401 — 90 of them, one per
   poll — while the page looked idle.
2. **The page read `.token`; the endpoint returns `session_token`.** Login returned 200 and the
   credential was silently dropped, so the phone stayed anonymous forever. The working dashboard
   reads `data.session_token`; the new page had invented a field.
3. **`pending.confirmations` on a list is `undefined`.** `/confirmations` and `/devices` are
   annotated `List[...]` and answer with bare arrays, so "nothing pending" rendered beside a
   confirmation that was really waiting: approve-from-the-phone did not exist. Then, after that
   was fixed, tapping **Deny** failed — the 4 s poll rebuilt the list unconditionally and detached
   the button under the finger. The list now repaints only when its signature changes.

None of the three is visible to a Python test that only checks the HTML contains the right paths.
Two of them are now covered by tests that run the page's *own shipped JavaScript* through Node
against a real API response (`test_phase35…:5`), which is how this project already tests
`dashboard/index.html`; the tap-timing fix is verified by hand and recorded as a gap, not as a
test that does not exist.

After the fixes, in the same browser session: the card rendered and stayed; **Deny** resolved it;
the server logged `"result":"denied","risk":"denied"` for `control_home_device`; `/confirmations`
returned `[]`; nothing was actuated.

## 5. Regressions

Red-first, then green. `tests/test_phase35_reach_the_laptop.py` — 30 tests: the default stays off;
loopback still works; a LAN address is refused with the mode off and accepted only when the
machine owns it; a foreign hostname is never accepted, in either mode; the guard's own cases plus
the two real-startup paths (refuses unsafely, boots honestly); the printed address reaches a
*piped* log — proven non-vacuous, it fails in 40 s with the `flush=True` removed, because stdout
is block-buffered when the desktop app pipes it; the page speaks only existing endpoints; the
credential and list-shape contracts; pairing round-trip; the probe's tier, its three answers, its
honest "absent" answer without inventing a version, and its refusal of a hostile name.

```
tests/test_phase35_reach_the_laptop.py    30 passed
full suite                                1683 passed · 0 failed · 0 skipped · 45 s
§45 safety matrix                         6 of 6 (unchanged)
arXiv                                     skipped in one run, passed live in the next
```

The skip is reported rather than smoothed over: `ArxivSource().search()` was called directly and
returned three real papers, so the skip is arXiv throttling, not this code.

## 6. Re-measured

| | before | after |
|---|---|---|
| tests passing | 1653 | **1683** |
| tools | 127 | **128** (read tier 68 → 69) |
| API routes | 104 | **105** |
| confirmation-gated | 27 | 27 (the new tool is READ and stays unasked) |
| test modules / reports | 58 / 36 | **59 / 37** |
| a phone can drive the laptop | no | **yes, over the LAN address, in a browser** |

## 7. Carried forward

* **35-A** the page's `send()`, `resolve()` and poll timing are hand-verified only; a DOM harness
  would keep the three defects above dead.
* **35-B** LAN mode trusts *every* interface the machine reports — a VPN or hotspot adapter is as
  accepted as the wifi. There is no per-interface allowlist.
* **35-C** a session minted from the page's bootstrap can drive the laptop as `laptop_main`
  without the device ever being paired, so pairing is advisory on that path. Verified live: the
  command above ran from an unpaired browser. Either the session carries a device identity the
  policy engine consults, or the page says pairing is not what authorises a command.
* **35-D** the packaged `.app` has no way to turn LAN on — `app/launcher.py` never sets it — so a
  double-clicked install stays laptop-only until the user exports a variable by hand.
* **Not delivered, and not pretending otherwise.** Android: no SDK/`adb`/device on this machine, so
  no APK was built or run; a phone on the same wifi can use `/phone` today, which is a web page,
  not an installable app. Windows: nothing built, because nothing could be executed here.
  Voice: wakeword matching runs over transcripts; there is no always-on audio detector, and the
  Microphone/Speech Recognition grants are the user's to give.
* **"Give Qoder the next instruction" remains unstarted deliberately.** `qoder` is not on `PATH`;
  `~/.qoder/bin/qoder-computer-use` is a directory holding an app, not a CLI entry point. Writing a
  tool that shells out to a path this machine does not have would produce a tool that reports
  success on a command that never ran — the exact failure this program has been spending 34 phases
  removing. The read-only half (`check_agent_status`) is what can be honest today.
* Previously carried and untouched by this phase: 26-B, 27-A/B/C, 28-A (synonym half), 29-A/C/D,
  30-B/C/D, 32-A/B/C, 33-A/B/C, 34-A/B/C, 23-E, hardware BLOCKED, LLM planner, unattended
  workflows.

## 8. Git

Branch `main`, parent `6db1baa` (Phase 34). Committed: `jarvis/config.py`, `jarvis/security/auth.py`,
`jarvis/api/app.py`, `jarvis/core/planner.py`, `jarvis/tools/__init__.py`, `jarvis/tools/agents.py`,
`dashboard/phone.html`, `run.py`, `benchmarks/acceptance_run.py`, `benchmarks/*.json`, `assets/*.svg`,
`tests/test_phase35_reach_the_laptop.py`, `README.md`, this report.

### The first runner's verdict: three failures, and two of them were the tests' fault

`37000938214` finished **failure** on `suite (3.12)` — `3 failed, 1673 passed, 6 skipped` — while
this machine reported green. The three are recorded here because each says something different:

1. `test_the_agent_probe_reports_the_three_answers_that_are_asked` asked the probe about **Qoder**
   and asserted `found: true`. The probe answered `{'found': False, ...}`. That is the probe being
   correct — the runner has no `/Applications/Qoder.app` — and the test being machine-dependent:
   it faked `run_command` but the installed-check reads the real filesystem, so it passed only on
   a laptop that happens to have the app. Fixed by giving the tests a bundle root of their own
   (`bundle_root`), which also makes the "absent app" test actually guarantee absence.
2. Fixing (1) exposed a real defect in the tool: the running-process needle was built with
   `installed.rstrip(".app")`, which strips *characters*, not the suffix. `Arena.app` became the
   needle `aren`, `Opera.app` became `oper` — a running app would be reported as not running for
   any name ending in `a`, `p` or `.`. Now `removesuffix(".app")`, with a test naming the case.
3. Both subprocess tests timed out: `_SERVER_TIMEOUT = 40 s` was sized by this machine, where the
   suite takes 38 s; the runner took 257 s for the same work, and a still-importing child looks
   identical to a child that refused. The deadline is 180 s and the failure text now says which of
   the two happened, with the output captured so far — a slow machine must not be allowed to
   report "it started anyway".

This is the same class of finding as the browser drive above, one level over: a green local run
was not evidence about the runner, exactly as green Python tests were not evidence about the page.

Pushed as a follow-up commit. **The second runner's verdict:** `37002522780` — `suite (3.11)`
finished `failure` on `1 failed, 1676 passed, 6 skipped`; the three above are gone, and what was
left was my own test over-reaching. It asserted that `0.0.0.0` must not appear anywhere in the
startup output, and `uvicorn` prints `running on http://0.0.0.0:PORT (Press CTRL+C to quit)` — a
true statement about the socket, not an address offered to a human. The assertion now checks only
the lines this app writes for a person to act on (`Phone client`, `JARVIS Forge core`), which is
what the finding was ever about. Locally that file is 30/30 and the suite is 1683 passing.

Two rounds of runner failures here are worth stating plainly rather than burying: this phase's
tests were written against one machine twice over — once in what they asserted about installed
apps, once in how long they allowed a boot to take, and once in what they assumed a startup log
contains. The CI verdict for this commit is appended below.


