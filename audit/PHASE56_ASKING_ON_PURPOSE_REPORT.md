# Phase 56 — asking for a permission, on purpose

| | |
|---|---|
| Phase | 56 |
| Date | 2026-10-04 |
| Starting commit | `176bcfb` (Phase 55) |
| Ending commit | recorded in §8 |
| Tests before | 2190 passed · 0 failed · 0 skipped |
| Tests after | **2218 passed · 0 failed · 0 skipped** (63.5 s) |
| Tools before / after | 142 / **142** — no capability was added |
| Planner routes before / after | 52 / **52** |
| HTTP surface | 117 / 111 paths → **118 / 112** |
| Confirmation-gated | **28 of 142** |
| Push status | **nothing pushed; local commit only** |

Gap E of the operating prompt. Phase 50 built the status card and deliberately left it
read-only, because a dashboard polls it and a poll that raises a system dialog is a consent
attack. This phase adds the other half: a request a person starts.

---

## 1. The rule the phase is built on

**The answer comes from re-reading the state, never from what the request reported.**

That is not a style preference. macOS's own request APIs disagree about what their completion
means:

* `AVCaptureDevice.requestAccess` and `SFSpeechRecognizer.requestAuthorization` hand back a
  bool for "the user decided yes" — the authoritative answer is still the status query.
* `AXIsProcessTrustedWithOptions` with the prompt option **grants nothing**. It opens System
  Settings and returns the trust flag as it was *before* the user did anything. Reporting its
  return value as the outcome would print "granted" about a dialog nobody has finished.
* `CGRequestScreenCaptureAccess` can return true while the running process still needs a
  relaunch before the grant applies to it.

So `request()` ends in `_read_state(cap)` — a fresh status read — and the test that pins this
patches a request claiming `granted` against a re-read saying `DENIED`, and requires the
answer to be `DENIED`.

## 2. What was built

| file | what it does |
|---|---|
| `jarvis/permissions/native/permission_state.swift` | a `request <key>` mode, with the run-loop pump Phase 47 learned the status path never had |
| `jarvis/permissions/service.py` | `requestable` / `prompt_only` / `no_request_reason` on every capability; `_run_request`, `_read_state`, `request()` |
| `jarvis/api/app.py` | `POST /permissions/{capability}/request`, loopback-only, audited |
| `dashboard/index.html` | "Grant access" where macOS can be asked, "Open System Settings" where the prompt cannot grant, and a reason where it cannot be asked at all |
| `tests/test_phase56_asking_on_purpose.py` | 28 tests |

Requestable: **microphone, speech recognition, screen recording, accessibility** (the last is
`prompt_only`). Not requestable, each with a reason that reaches the user: **automation** ("the
only probe is sending an event, which is the action itself"), **notifications** (the framework
needs the app's own bundle and run loop), **local network** (macOS does not gate a server for
it).

## 3. Loopback-only, and why the host gate does not cover it

A phone on the wifi passes the existing host gate — its `Host` is an address this machine
owns. So `_loopback_only_or_refuse` is a separate rule: a system dialog appears in front of
the laptop, over the whole screen, and only someone sitting there can answer it honestly. A
remote device with a valid session token must not be able to raise prompts on the owner's
display. Tested from a LAN-shaped peer address, not argued.

## 4. What the real binary said, which no test did

The suite patches the helper seam, so it cannot see whether the helper works. Running the
compiled binary directly:

```
status mode  -> speech=not_determined  microphone=denied        (works)
request mode -> exit -6  (SIGABRT), no stdout                   (does not work)
```

The Speech framework aborts a process that is not an app bundle — the same constraint Phase 50
recorded for `UNUserNotificationCenter`, now met again on the request side. **A helper binary
cannot raise these prompts; only the app process can.**

The implementation was changed to carry that rather than hide it: `_run_request` now returns a
reason, and the answer a person gets is

```
ok: False | state: UNKNOWN | granted: False
"I could not ask macOS about speech recognition, so nothing about it has changed
 (the helper could not ask (exit code -6)). Set it yourself in
 System Settings → Privacy & Security → Speech Recognition, then check again."
```

`UNKNOWN`, not `DENIED`: a failure to ask and a refusal are different facts, and reporting the
first as the second would send someone to System Settings to undo a decision that was never
made.

**So this phase ships the door and not the room behind it.** The route, the guard, the
reasons, the re-read rule and the panel controls are real and tested. The ask itself cannot
succeed until it runs inside `Vantage.app` — which is Phase 57 (the app process, persistent)
and Phase 58 (a native shell) territory, not something to fake with a status message.

## 5. Live

Isolated instance, `JARVIS_HOME=$(mktemp -d)`, ephemeral port. Nothing here raised a dialog:

```
POST /permissions/microphone/request   (no token)   -> 401
POST /permissions/telepathy/request                 -> 404
POST /permissions/automation/request                -> 409
   "There is no query for Apple Events. The only probe is sending an event,
    which is the action itself, not a settings page."
POST /permissions/speech_recognition/request        -> 200, ok=False, state=UNKNOWN
   (the helper aborts before any prompt; the reason is reported, see §4)
```

## 6. BLOCKED_BY_USER_DECISION

**The microphone, screen-recording and accessibility request paths were not driven live.**
Each raises a macOS dialog that only the person at this machine can answer, and running them
unattended would be exactly the un-consented prompt this phase's own design forbids. The
session's permission classifier blocked the same attempt when it was made, and it was not
worked around.

To finish the verification, one deliberate action is needed from the user: press **Grant
access** on the laptop panel (or approve `POST /permissions/microphone/request` on the
delivered app) while sitting at the machine, and check that the card moves from
`NOT_DETERMINED` to `GRANTED` — or stays `DENIED` if "Don't allow" is chosen, which must also
survive a re-read unchanged. Until that is done, the honest statement is: the request path is
implemented and tested against stubs and against one real abort, and **has not been observed
raising a dialog on this machine.**

## 7. Defects found in this phase's own work

1. **The helper's request mode aborts** — found only by executing the binary, not by the
   suite. Reported in §4 and turned into a carried reason instead of a bare failure.
2. **My own test stub had the argument index wrong** (`cmd[2] == "request"` where the call is
   `[binary, "request", key]`), so the first version of the abort test **ran the real helper**
   instead of the stub. It passed for the wrong reason and would have kept passing; fixed, and
   the mistake is recorded in a comment where someone will repeat it.
3. **A guard of mine asserted a substring that its own new code contained**: "AXIsProcessTrusted
   must not appear in the request path" failed because `AXIsProcessTrustedWithOptions` contains
   it. The clause tested nothing and was removed; the surviving assertions are the ones that
   matter.
4. **The Phase 50 helper guard had to change contract.** It banned `requestAccess` anywhere in
   the file. Phase 56 legitimately adds a request mode to the same binary, so the file-wide ban
   would have blocked real work. It is now scoped to what it was ever protecting — *the status
   read must not prompt* — enforced by explicit REQUEST PATH / STATUS READ PATH markers in the
   Swift source. That is a narrowing of a safety assertion, so it is stated here rather than
   left in the diff: the prompting calls are still required to exist, and now required to be
   **only** on the request side.
5. **TestClient's default peer is "testclient"**, which is neither loopback nor real — so a
   loopback-only rule reads every test caller as remote. The fixture now sets the socket peer
   explicitly, and one test asserts the LAN case separately.

## 8. Numbers, re-measured

| | before | after | how |
|---|---|---|---|
| Test suite | 2190 passed | **2218 passed · 0 failed · 0 skipped** (63.5 s) | `JARVIS_HOME=$(mktemp -d) .venv/bin/python -m pytest -q` |
| Phase 56 tests | 0 | **28** | `tests/test_phase56_asking_on_purpose.py` |
| Test modules | 82 | **83** | `ls tests/test_*.py \| wc -l` |
| Phase reports | 57 | **58** (this file) | `ls audit/*.md \| wc -l` |
| Lines | 68,461 — 41,430 · 27,031 | **69,120 — 41,726 source · 27,394 tests** | `wc -l` |
| Tools / planner routes | 142 / 52 | **142 / 52** | unchanged — this phase adds no capability |
| HTTP surface | 117 / 111 paths | **118 / 112 paths** | `len(app.routes)` |
| Confirmation-gated | 28 | **28** | no gate moved |

## 9. Open items

* **56-A** — the headline: **the ask cannot succeed from a helper process on this machine.**
  The Speech framework aborts a non-bundle process; the microphone and screen-recording paths
  were not exercised at all (§6). The request must move into the app process.
* **56-B** No first-run *wizard* was built — this phase is the request path and the panel
  control, not the guided sequence §3 of the build prompt describes. The wizard depends on 56-A.
* **56-C** `prompt_only` capabilities still report `DENIED` from the status query when macOS
  has simply never been asked. The card's word for that is `REQUIRES_MANUAL_SYSTEM_SETTINGS`,
  which is right, but the underlying helper cannot tell "never asked" from "asked and refused"
  for Accessibility.
* **56-D** No revocation *from the panel* of a macOS permission — that is System Settings'
  job, and the card says so; there is no `tccutil` call and there must not be a private-DB one.
* **56-E** The route is audited (`permission.request`, with `denied` rows for refusals), but the
  audit row records the *attempt*, not the user's answer to the dialog — which the server
  cannot see.

## 10. Next

Phase 57 — host persistence. It is now also the prerequisite for 56-A: the permission ask has
to live in a long-running app process, and that process does not exist yet.
