# Phase 50 — A permission manager that says who answered

Opened 2026-10-04 on the second attached document, `VANTAGE — MASTER APPLICATION BUILD
PROMPT`. Its §36 implementation order puts this at step 3 ("Implement macOS permission
manager"), and §3 sets the rule it has to satisfy:

> Never display "Permission Granted" until the operating system actually reports that it is
> granted.

That rule turned out to be incomplete in a way the document does not anticipate, and the
missing half is the substance of this phase.

---

## 1. The audit came before the build

The prompt asks for a great deal that already exists. Measured first, so the phase is built
against the repository rather than against the document's impression of it:

| Prompt asks for (§2, §10, §11, §16, §30) | Reality |
|---|---|
| One pipeline, `PermissionEngine` sole authority, tri-state verification, hash-chained audit, honest offline states | all present; 134 tools, 28 gated |
| QR pairing where "scanning alone is NOT authorization" | already true: `/devices/pair/request` → pending → Mac-side approve/deny |
| 128-bit confirmation IDs, 300 s TTL, explicit resolution | already true (`models.py:30`, `config.confirm_timeout=300`) |
| Device revocation | exists for *devices*; **no product surface can revoke a tool** (found in Phase 49a) |
| Emergency stop | the `emergency_stop` tool exists; **no route**, so a phone cannot reach it |
| §28's eleven tables | `tasks`, `confirmations` exist; the pairing/key/pairing-attempt concepts exist as `device_pairings` and `devices`; genuinely absent: `remote_sessions`, `remote_events`, `push_tokens`, `ai_provider_configs`, `permission_status` |
| Menu bar, setup wizard, permission manager, native mobile app, relay, push, biometrics, provider adapters, replay protection | none exist |

## 2. The measurement that decided the design

The same status helper, the same binary, the same machine, two callers:

| capability | asked by **Vantage.app** | asked by **a terminal** |
|---|---|---|
| Speech recognition | **GRANTED** | NOT_DETERMINED |
| Screen recording | **GRANTED** | denied |
| Microphone | NOT_DETERMINED | **DENIED** |
| Accessibility | denied (needs Settings) | **GRANTED** |

Four capabilities, four different answers. macOS attributes a permission to the **responsible
process**, and there is no public API to ask which process that is.

So §3's rule has a second half: it is not enough that the OS reported it — the report has to
be *about the application the user is looking at*. A dashboard that prints "Accessibility:
GRANTED" from a helper a shell started is describing Terminal.app to someone reading a Vantage
window, and it does so with the confidence the rule was written to prevent.

The Speech row is the cross-check, because it is not a guess: Phase 47 recorded this user
granting Speech Recognition to Vantage, and only the app's own reading reports it.

## 3. What was built

```
jarvis/permissions/native/permission_state.swift   the four non-prompting macOS queries
jarvis/permissions/service.py                      PermissionStatusService
GET  /permissions                                  authenticated, read-only
dashboard                                          a Permissions card
```

The helper uses only the queries Apple provides that **answer without prompting**:
`CGPreflightScreenCaptureAccess()`, `AXIsProcessTrusted()`,
`AVCaptureDevice.authorizationStatus(for: .audio)`,
`SFSpeechRecognizer.authorizationStatus()`. A test asserts `requestAccess` never appears in
the file — a status endpoint that pops a dialog on every poll is not a status endpoint.

Seven capabilities, six states (`NOT_REQUIRED / NOT_DETERMINED / DENIED / GRANTED /
REQUIRES_MANUAL_SYSTEM_SETTINGS / UNKNOWN`), and each row carries the §4 field list:
explanation, request method, settings pane, verification method, recovery — plus
`answered_by`, `host`, `about_bundled_host` and a `status_note` naming the subject.

Two mappings are deliberate and tested:

* **Screen recording and accessibility report `REQUIRES_MANUAL_SYSTEM_SETTINGS`, not
  `DENIED`.** macOS never prompts for these; "denied" would send the user looking for a
  dialog that will never arrive, when the only door is System Settings.
* **Automation and notifications report `UNKNOWN`.** Apple Events has no preflight query --
  the only probe is sending an event, which *is* the permission. Reporting GRANTED there
  would be an invented fact, which is the thing this whole phase is against.
* **Local network reports `NOT_REQUIRED`.** That gate is an iOS thing; the Mac side has
  nothing to grant. Saying "granted" here would be wrong in the flattering direction.

## 4. What the browser caught that the suite could not

Three defects, all found by looking at the running app rather than at test output:

1. **`about_bundled_host` was False on the delivered app**, even though the path plainly
   contains `Vantage.app`. The check was `".app" in parts` -- looking for a path component
   *named* `.app` instead of one *ending* in it. No unit test could catch it, because the
   tests inject the host identity rather than reading their own filesystem path; the fix is
   a pure `_bundle_for()` with the real delivered path as a parametrised case.
2. **The card printed "read by none".** It named the *helper's* bundle id, and the helper is
   a loose binary in the data directory whose bundle id is legitimately nil. The subject has
   to come from the server's own path.
3. **The card then printed the same sentence twice** -- the client's subject line and the
   server's note. Now there is one line, styled by whether it describes Vantage, and a test
   counts it.

Each of these is the project's recurring shape: a claim verified in a test and falsified by
the real surface.

## 5. Live, on the delivered app

Rebuilt, re-signed (`valid on disk`, `satisfies its Designated Requirement`), relaunched with
`open`:

```
GET /permissions
  about_bundled_host: True
  note: read by Vantage running from /Users/abhijeetmiskin/Downloads/Vantage/Vantage.app
    microphone           NOT_DETERMINED
    speech_recognition   GRANTED          ← the grant the user gave in Phase 47
    screen_recording     GRANTED
    accessibility        REQUIRES_MANUAL_SYSTEM_SETTINGS
    automation           UNKNOWN
    notifications        UNKNOWN
    local_network        NOT_REQUIRED
  summary: {GRANTED: 2, DENIED: 0, REQUIRES_MANUAL: 1, NOT_DETERMINED: 1,
            NOT_REQUIRED: 1, UNKNOWN: 2}
```

Dashboard at 1440 px: the card renders each state in its own colour, one subject line, a
"Check again" control, 0 console errors. It also correctly showed **"Audit chain: BROKEN"**
next to it -- the documented state of this machine's `~/.jarvis-forge` (5 breaks from
pre-verification records and tamper tests, addenda C-2/C-2a), not something this session
caused. A permission card that only ever agrees with you is not a permission card.

## 6. Numbers, re-measured

| | before | after | how |
|---|---|---|---|
| Test suite | 1939 passed · 1 skipped | **1960 passed · 0 failed · 1 skipped** (1961 collected, 81.4 s) | `JARVIS_HOME=$(mktemp -d) .venv/bin/python -m pytest -q` |
| Phase 50 tests | 0 | **21** | `tests/test_phase50_who_answered.py` |
| Test modules | 76 | **77** | `ls tests/test_*.py | wc -l` |
| Phase reports | 51 | **52** (this file) | `ls audit/*.md | wc -l` |
| Lines | 63,805 | **64,614** — 39,572 source · 25,042 tests | `wc -l` |
| Routes | 112 objects / 106 paths | **113 / 107** | `len(app.routes)` |
| Capabilities reported | 0 (no such surface) | **7**, with 2 granted / 1 needs-Settings / 1 never-asked / 2 unknown / 1 not-gated | live, above |

Re-measured at the close, and one number moved: **1961 passed · 0 failed · 0 skipped in 59.1 s**.
The skip in the row above was `tests/test_research.py` reaching for arXiv and timing out; on this
run the network answered and the same test passed. 1961 collected either way -- the pair
(1960, 1 skipped) and (1961, 0) are one suite with a network-gated test, not two states of the
code. `README.md` now publishes it that way instead of asserting a single figure, and
`benchmarks/suite_history.json` had stopped at phase 45a while the README cited it for the
phase-50 total -- the five missing closes were transcribed from each report's own final run and
the growth chart regenerated from them.

## 7. Open items

* **50-A — `inside_bundle` is a proxy, not the responsible-process answer.** macOS exposes
  no public API for "which app is this request attributed to", so the service infers it from
  where the server's own code lives. A Vantage started from a terminal *while running from the
  bundle directory* would be labelled as describing Vantage and would be describing the
  terminal. The measured four-way difference in §2 is the evidence this matters.
* **50-B — the two `GRANTED`s are not proof of working features.** Screen recording reads
  granted and the screenshot tool has never been exercised from this app build; speech reads
  granted and Phase 47 proved it transcribes. The card reports what the OS says, which is a
  different claim from "this feature works" -- and §9 of the prompt asks for exactly that
  distinction to be kept.
* **50-C — no capability can be *granted* from here.** Deliberate: the endpoint is read-only
  and the helper never prompts. The first-run wizard (§3) will need a deliberate, user-initiated
  request path, and it must not be a side effect of a status poll.
* **50-D — `answered_by.pid` is the helper's pid, not the server's.** The helper is a child
  process, so its pid is transient and its `executable` is a path in the data directory. The
  useful subject is `host`, and `answered_by` is kept because it is what the helper can
  honestly report about itself.
* **50-E — notifications are `UNKNOWN` because nobody has asked.** The UserNotifications
  query needs the app's own run loop and a bundle the framework recognises; a helper answers
  nothing useful. This is a real gap in the card, not a finished row.
* **50-F — the helper compiles on first call** (a few seconds, arm64 since Phase 47 pinned
  the architecture). Same cold-start shape as the speech helper; shipping it prebuilt would
  remove it.

## 8. Where this leaves the build prompt

§36's order is: audit → preserve → **permission manager** → package host → persistent service
→ Basic Mode → verification → mobile shell → pairing → LAN → sync → approvals → push → relay →
providers → planner → realtime → computer use.

Step 3 is now done, with the caveat that it is a *status* manager: it can tell you the state
and where the state was read from, and it cannot yet request, which is what the §3 first-run
wizard will need. The next honest step is §36 step 6 -- the Basic Mode command set the spec
measured 14 utterances short in Phase 49a -- because that is the layer every later item
(mobile control, approvals from a phone, the acceptance scenario in §35) actually exercises.
