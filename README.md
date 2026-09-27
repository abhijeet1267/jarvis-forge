# JARVIS Forge (v1.5)

A permissioned, multi-agent personal AI operating system.

This repository implements the **Core** (section 60), **Text JARVIS** (41),
**Voice** (3), **Recovery** (47) and **Privacy** (49) of
`JARVIS_Forge_Full_Working_Plan.md`, plus build-order steps 1–9:
**Phone Agent + secure device transfer** (v0.6), **Email + Calendar** (v0.7),
**Scheduler + daily briefing** (v0.8), **Advanced/vector memory** (v0.9),
**Research** (v1.0), **Coding** (v1.1), **Design** (v1.2), **Vision** (v1.3)
and **Home / IoT** (v1.5) agents.

```
Intent -> Plan -> Permission -> Tool -> Execution -> Verification -> Memory -> Response
```

That loop works end-to-end today, with real macOS actions, a real safety layer,
real speech synthesis, and honest reporting when a capability is not available.

---

## Quick start

```bash
cd jarvis-forge

# 1. Use the managed interpreter (or any Python >= 3.10)
PY=/Users/abhijeetmiskin/.workbuddy-ai/binaries/python/envs/default/bin/python
$PY -m pip install -r requirements.txt

# 2. Run the API + dashboard
$PY run.py
#    -> http://127.0.0.1:8765/dashboard
#    -> http://127.0.0.1:8765/voice/status

# 3. Talk to it
$PY run.py cli          # text REPL
$PY run.py voice        # voice REPL -- replies are spoken aloud
$PY run.py demo         # acceptance sweep (text)
$PY run.py voice-demo   # acceptance sweep through the voice pipeline
```

No API key is required. Without a model endpoint the deterministic rule-based
planner runs the whole loop offline. To enable an LLM planner, copy
`.env.example` to `.env` and set `JARVIS_LLM_BASE_URL` / `JARVIS_LLM_API_KEY`
(any OpenAI-compatible endpoint).

---

## Desktop app

JARVIS Forge also ships as a real macOS application — no terminal needed.

```bash
./install_app.sh                 # build and install into /Applications
# or just build it:
python build_app.py              # -> dist/Jarvis Forge.app
open "dist/Jarvis Forge.app"
```

Double-clicking the app:

1. starts the JARVIS server,
2. opens a **control window** with live status (devices, tools, pending
   confirmations, model, recent actions),
3. opens the dashboard in your browser,
4. shuts the server down cleanly when you quit.

The control window has **Open Dashboard**, **Voice Status**, **Speak**,
**Restart** and **Quit**.

### Diagnosing an install

```bash
JARVIS_SELFTEST=1 "dist/Jarvis Forge.app/Contents/MacOS/JarvisForge"
```

```
  [ok  ] project directory        /path/to/jarvis-forge
  [ok  ] python interpreter       /path/to/python
  [ok  ] tkinter / Tk             Tk 9.0.3
  [ok  ] control panel widgets    4 widgets constructed
  [ok  ] server                   24 tools, agents ['laptop', 'memory'], model=rules
  [ok  ] text to speech           macos_say
  [ok  ] speech to text           macos_speech, transcript
  [ok  ] microphone               ready (3 input device(s))
```

The microphone row reports **consent**, not the configured device string. It
says `ready` only when a capture backend exists *and* macOS has granted the
microphone to the process that is running; otherwise it names the reason
(`blocked by macOS permission — …`, `no capture backend installed`, or
`permission state unreadable` where consent cannot be probed at all).

### What the bundle does and does not fix

The bundle declares `NSMicrophoneUsageDescription`,
`NSSpeechRecognitionUsageDescription` and `NSAppleEventsUsageDescription`, and
is ad-hoc signed so TCC has a stable identity. That is necessary but **not
sufficient** for Speech Recognition: Apple only grants it to an app signed with
a real Developer ID certificate. Measured on this machine, an ad-hoc-signed
bundle is still refused.

To get native on-device speech recognition you need either:

- a Developer ID signature (`codesign --sign "Developer ID Application: ..."`),
  then delete `~/.jarvis-forge/voice/native/.tcc_blocked` and retry, or
- a whisper.cpp binary on `PATH` (auto-detected), or
- `JARVIS_STT_BACKEND=openai_whisper` with a cloud key.

TTS, the control window, the dashboard and the whole command pipeline work
regardless.

If the control window cannot be created for any reason, the launcher
**falls back to headless mode** rather than failing — the server still starts
and the dashboard still opens.

---

## The six core components

| Component | Module | Responsibility |
|---|---|---|
| **Orchestrator** | `jarvis/core/orchestrator.py` | Runs the full pipeline; the only sequencer |
| **Tool Registry** | `jarvis/registry/tools.py` | Declarative capability catalogue + validation |
| **Permission Engine** | `jarvis/security/policy.py` | EXECUTE / CONFIRM / BLOCK for every action |
| **Laptop Agent** | `jarvis/agents/laptop.py` | App launch, file ops, system queries |
| **Memory** | `jarvis/memory/` | Flat store + project-state graph |
| **Audit Log** | `jarvis/security/audit.py` | Append-only SQLite + JSONL record of everything |

Supporting pieces: `jarvis/core/planner.py` (intent detection),
`jarvis/security/risk.py` (risk classification), `jarvis/security/paths.py`
(filesystem safety), `jarvis/gateway/router.py` (multi-model routing),
`jarvis/voice/` (the voice pipeline).

---

## Voice (Phase 2)

```
wake word -> listen -> transcribe -> JarvisCore.handle -> speak
```

Every backend is pluggable and probed for availability, so the same session
code runs against native macOS speech, whisper.cpp, a cloud provider, or a text
passthrough for testing.

| Layer | Backends | Status here |
|---|---|---|
| **Wake word** | transcript matching (fuzzy) | working |
| **TTS** | `macos_say`, `pyttsx3`, `null` | **`macos_say` working** — real audio, 184 voices |
| **STT** | `macos_speech`, `whisper_cpp`, `openai_whisper`, `transcript` | `transcript` fallback active (see below) |
| **Capture** | ffmpeg/AVFoundation, sounddevice | ffmpeg present; needs mic permission |

Implemented from section 3:

- **Wake word** — accepts "Jarvis", "hey Jarvis", "ok Jarvis", and *fuzzy* matches
  recognition noise like "jervis". Crucially, the command is sliced from the
  **original** transcript, so `/tmp/AMR_Report.pdf` is never mangled.
- **Interruption while speaking** — `POST /voice/interrupt` cancels the current
  utterance so the user can barge in.
- **Context-aware follow-ups** — after a question or a pending confirmation, a
  bare "yes" / "no" is understood without repeating the wake word.
- **Streaming responses** — `GET /command/stream` emits SSE frames
  (`status` → `action` → `token` → `done`) so TTS can begin before the reply
  is fully composed.

### A note on macOS Speech Recognition

`jarvis/voice/native/macos_speech.swift` compiles and runs, but Apple's Speech
framework only grants recognition to a **signed application bundle** with the
`NSSpeechRecognitionUsageDescription` entitlement. A bare command-line binary is
aborted by TCC (SIGABRT). Rather than pretend otherwise, the backend:

1. reports the exact reason,
2. writes a `.tcc_blocked` marker so auto-selection stops choosing it,
3. falls back to the next backend.

Delete `~/.jarvis-forge/voice/native/.tcc_blocked` after running JARVIS from a
bundled host. For local transcription today, install a whisper.cpp binary
(auto-detected) or set `JARVIS_STT_BACKEND=openai_whisper`.

---

## What works right now

Real, verified actions on macOS:

- `Jarvis, open VS Code` / `close terminal` — via `open` and AppleScript (no GUI clicking)
- `Jarvis, find my report` — Spotlight (`mdfind`) with a bounded-walk fallback
- `Jarvis, what is my battery` — real `pmset` reading
- `Jarvis, what time is it`, `system status`
- `Jarvis, compress /path/to/thing`
- `Jarvis, delete <exact path>` — **always** confirmed, then moved to Trash
- `Jarvis, remember that ...`, `what did I work on yesterday`
- `Jarvis, check my car fuel`, `give me today's location summary` — honest "not connected"
- `Jarvis, notify my watch "build finished"` — queued, and reported as queued until the companion takes it
- `Jarvis, ask my watch to confirm` — a pending approval on the wrist; only the wearer's reply decides it
- `Jarvis, stop everything` — priority emergency stop, never gated
- **Spoken replies** via macOS `say`

Every one of these produces an audit record and a memory entry.

## What is deliberately NOT implemented yet

The system is built to say so rather than fake success:

| Capability | Status |
|---|---|
| Email / calendar remote backends | local backends live; IMAP/SMTP and CalDAV activate when configured |
| Home automation | v1.5 agent wired; gateway offline by default (honest "No device was switched.") |
| Car telemetry | v1.7 agent wired; read-only, no adapter connected by default (honest "I cannot read live telemetry yet") |
| Watch Agent | v1.8 wired; unpaired and bridge-less by default (honest "nothing has reached your watch"); no tool may resolve a confirmation |
| Robotics | v1.6 agent wired; gateway offline by default (honest "no motion command was sent"); PHYSICAL risk ⇒ confirmation |
| Continuous acoustic wake word | interface reserved, no engine wired |
| Microphone capture | hardware present and enumerated, **consent measured as not granted** for an unbundled process — `describe_audio()` says `blocked` and capture refuses before spawning `ffmpeg`, instead of the old `which("ffmpeg")`-derived "available" followed by a hang |

---

## Safety model

The plan's central rule — *do not build one AI with unrestricted access* — is
enforced structurally, not by prompt wording.

**Risk tiers** (section 8): `0 read` → `1 low` → `2 external` → `3 destructive`
→ `4 physical`. Level ≥ 2 confirms by default; level 4 requires strong
authentication (planned).

**Three states** (section 48): `EXECUTE`, `CONFIRM`, `BLOCK`.

### API security (sections 25, 26)

A localhost HTTP API that can launch apps, move files and delete things is
reachable by **any web page the user has open**. That is a real attack surface,
so it is closed by default rather than by configuration:

| Control | What it stops |
|---|---|
| **Token auth** (`X-JARVIS-Token`, 0600 file) | Any other process on the machine |
| **Custom auth header** | Forces a CORS preflight a foreign origin cannot pass |
| **Strict CORS allowlist** | A web page *reading* command results. Never `*` |
| **Loopback `Host` check** | DNS rebinding (an attacker hostname resolving to 127.0.0.1) |
| **Rate limiting** | Runaway loops; sized with headroom for the dashboard |
| **Confirmation expiry** | An unanswered approval becoming a standing authorisation |
| **Hash-chained audit log** | Editing or deleting past records without detection |

```bash
# Call the API yourself:
curl -H "X-JARVIS-Token: $(cat ~/.jarvis-forge/api_token)" \
     -X POST http://127.0.0.1:8765/command \
     -H 'Content-Type: application/json' -d '{"text":"what is my battery"}'
```

`GET /health` reports the live security posture, and
`AuditLog.verify_chain()` re-walks the audit chain to detect tampering.

### Command execution

The old allowlist contained `python3`, `node`, `npm`, `pip` and `git`. Those are
Turing-complete or execute code, which made the allowlist decorative —
`python3 -c '...'` is arbitrary code execution. It is now split:

- `JARVIS_ALLOWED_COMMANDS` — read-only inspection tools (`ls`, `cat`, `date`…)
- `JARVIS_POWERFUL_COMMANDS` — **empty by default**; opt in explicitly

Even when opted in, `-c`/`-e`/`--eval`/`-m` style flags are refused, arguments
are passed literally (no shell), and shell metacharacters are rejected.

### Guard rails that are already live

1. **Phrasing guard** — "delete everything", "sudo rm -rf /", "unlock the
   door", "install unknown.exe", "pay 500 dollars" are refused *before* any
   planning happens. This applies on the voice path too.
2. **Path guard** — `/etc`, `/usr`, `/System`, the home directory itself, plus
   **credentials** (`~/.ssh`, `~/.aws`, `~/.netrc`, `~/.git-credentials`) and
   **persistence** paths (`~/.zshrc`, `~/.bashrc`, `~/Library/LaunchAgents`)
   are refused at the policy layer, so the user is never nudged into approving
   them. Writing a shell rc file is code execution at next login.
3. **No unrestricted shell** — commands run through `create_subprocess_exec`
   with an explicit argv; there is no path from the model to a shell.
4. **Trash, not delete** — `delete_file` moves to `~/.Trash` by default.
   Permanent deletion is a separate, always-confirmed path.
5. **No invented parameters** — a plan may only name tools that exist and pass
   only parameters the tool declares.
6. **Emergency stop** is tagged `safety` and bypasses confirmation entirely.
7. **High-entropy approvals** — confirmation IDs carry 128 bits of randomness,
   since they authorise a real action.

### Safety tests (section 45)

```bash
$PY -m pytest tests/test_pipeline.py -k safety -v
$PY -m pytest tests/test_security.py tests/test_recovery.py tests/test_privacy.py -v
```

Covers `delete everything`, `wipe the disk`, `run this command as root`,
`install unknown.exe`, `unlock the door`, `disable the firewall`,
`pay 500 dollars` — asserting each is rejected, clarified, or confirmed, and
that nothing actually executes. Plus auth, CORS, host validation, rate
limiting, confirmation expiry, protected paths, the command allowlist and
audit-chain tamper detection.

---

---

## Phone Agent + secure transfer (v0.6)

Trust is never implicit, and transfers are verified rather than assumed.

### Pairing

```
device  -> POST /devices/pair/request     (the one unauthenticated path)
        <- pending                        grants nothing
user    -> POST /devices/pair/{id}/approve
        <- paired + a 256-bit key         fingerprint shown for verification
user    -> POST /devices/{id}/revoke      key material destroyed
```

A pending request performs no action and is useless until approved, which is
why the knock itself may be unauthenticated. Pending requests are capped so the
path cannot be used to flood the database. Every request, approval, denial and
revocation is audited.

### Transfer

| Property | How |
|---|---|
| Encrypted transport | TLS via the platform OpenSSL, certificate pinned by fingerprint |
| Device authentication | HMAC-SHA256 challenge–response over the pairing key |
| Integrity | SHA-256 checked before the transfer is reported complete |
| No silent overwrite | A name collision gets a suffix — never clobbers |
| Atomic | Written to `.part`, renamed only after the checksum matches |
| Bounded | Size limit, per-message ceiling, handshake and message timeouts |
| Cancellable | `cancel_transfer`, with a guarded state machine |

```
PENDING → CONFIRMED → CONNECTING → TRANSFERRING → VERIFYING → COMPLETED
                                          ↓
                                 FAILED / CANCELLED
```

**Conversationally:**

```
you:  send /tmp/report.pdf to my phone
JARVIS: This needs your confirmation before I run it.
you:  approve
JARVIS: Sent report.pdf (19.6 KB) to phone_main and verified the checksum.
```

### Design notes

**The executor is the laptop, not the phone.** Transfer tools run on
`laptop_main` and take a `target_device` parameter. That keeps the permission
engine's device checks meaningful (they apply to the machine actually acting)
and turns "the phone is unreachable" into an honest tool-level message instead
of a policy block with a misleading reason.

**The phone side is a stand-in, not an Android app.** There is no Android client
yet. `jarvis/transport/endpoint.py` implements the device half of the protocol
as a real, runnable process so the flow is genuinely testable; an Android client
implements the same wire format. Nothing about the protocol is Mac-specific.

**A standing grant cannot bypass pairing.** Approving a repeat transfer is
consent to repeat *that* action — not consent to trust a new device. Transfers
to an unpaired device and any action that grants or revokes trust always
surface a confirmation.

**No tool executes anything on the phone**, and nothing lets a device run
anything on this Mac. The phone can send and receive files and nothing else.

---

---

## Scheduler + daily briefing (v0.8)

JARVIS can run approved work on a schedule — but a schedule is a *grant of
future authority*, so it is constrained in two independent ways.

**Conversationally:**

```
you:  remind me at 8 PM to submit my assignment
JARVIS: This needs your confirmation before I run it.
you:  approve
JARVIS: Scheduled submit my assignment — once at Sun 20 Sep 20:00.

you:  every morning give me my daily briefing
you:  approve
JARVIS: Scheduled briefing — every day at 08:00.

you:  give me my daily briefing
JARVIS: JARVIS DAILY BRIEFING — Sunday 20 September 2026
        Date and time / Schedule / Reminders / Waiting on you /
        Failures and recoveries / Email / Projects / System
```

### Never silently escalate

The plan states this in capitals, and it is enforced in two places:

1. **A risk ceiling per job.** The job records the risk level its action had at
   creation. If the action is riskier later, the run is refused — a schedule
   cannot become a way to gain authority after the fact.
2. **Re-evaluation on every run** through the same `PermissionEngine` an
   interactive command uses. A `CONFIRM` verdict **parks a pending
   confirmation**; the runner never approves on your behalf. A `BLOCK` fails
   the run.

Scheduling a destructive or physical action is refused outright:

```
POST /schedules {"action": "delete_file", ...}
-> 400  "delete_file is a destructive action, which is too risky to run
         automatically."
```

Every run is recorded with its outcome — `executed`, `awaiting_approval`,
`blocked` or `failed` — so "it ran" is never the whole story.

### Recurrence

```
every morning          every day at 06:30       at 8 PM (one-off)
every Monday at 9 AM   every 30 minutes         every 2 hours
```

### Briefing sections

`datetime`, `calendar`, `reminders`, `tasks`, `approvals`, `failures`, `email`,
`projects`, `system`. Choose a subset with `?sections=datetime,calendar` or
`JARVIS_BRIEFING_SECTIONS`. A section that cannot be produced **says so** —
an empty briefing must not look like good news.

---

## Email + Calendar (v0.7)

Both integrations have **two real backends**, and the active one is always
reported:

| Integration | Local backend (default) | Remote backend (when configured) |
|---|---|---|
| **Email** | Local maildir — real `.eml` files in `~/.jarvis-forge/mail/` | IMAP read + SMTP send (stdlib) |
| **Calendar** | SQLite — full CRUD, works offline | CalDAV over HTTPS with iCalendar |

Neither local backend is a stub: a `.eml` file opens in any mail client, and the
local calendar is a real store. `GET /integrations` says which is live.

**Conversationally:**

```
you:  check my unread emails
you:  draft an email to my teammate
JARVIS: Drafted an email to arun@example.com. Nothing has been sent.
you:  send it
JARVIS: This needs your confirmation before I run it.
you:  approve
JARVIS: Written to your local outbox as 20260920-...eml. No mail account is
        configured, so nothing left this machine.

you:  schedule a project meeting tomorrow at 5 PM
JARVIS: This needs your confirmation before I run it.
you:  approve
JARVIS: Created project meeting on Mon 21 Sep at 17:00.
you:  what do I have tomorrow
JARVIS: 1 event(s). First: project meeting at 17:00.
```

### Safety rules

- **Reading is free; writing is not.** `create_event`, `update_event` and
  `cancel_event` require confirmation. `send_email` is Level 3 destructive and
  unreversible.
- **Sending is never inferred.** Only an explicit send verb sends. Drafting
  writes a draft and transmits nothing.
- **An ambiguous recipient is refused.** Two contacts matching "Rao" produces a
  candidate list and a question, never a guess.
- **Credentials never reach a log.** Mail and calendar passwords are in the
  redaction known-secrets list.

Configure a real account by setting `JARVIS_EMAIL_ADDRESS`,
`JARVIS_EMAIL_IMAP_HOST`, `JARVIS_EMAIL_SMTP_HOST` (plus the matching
`JARVIS_EMAIL_PASSWORD`) or `JARVIS_CALENDAR_URL`, `JARVIS_CALENDAR_USER` and
`JARVIS_CALENDAR_PASSWORD`. Leave them blank to keep using the local backends.

---

## Home / IoT Agent (v1.5)

The home gateway is **offline by default**. That is deliberate: with no broker
wired, JARVIS keeps a last-known state, is honest that it cannot reach anything,
and never claims a switch happened when it did not.

```
you:  what is the home status
JARVIS: Your home gateway is offline, so I am showing last known state and
       not controlling anything. 3 of 17 devices were on. Motion: none.
       Front door: closed.

you:  turn on the study light
JARVIS: Your home gateway is offline, so I could not turn it on.
       No device was switched.

you:  how warm is the study
JARVIS: Study temperature sensor: 23.0°C (last known state).
```

### The five tools

| Tool | Level | What it does |
|---|---|---|
| `get_home_status` | 0 read | Gateway state + on/total counts, motion, door |
| `list_home_devices` | 0 read | Known devices, filtered by room / kind / on-state |
| `get_home_reading` | 0 read | A sensor reading for a room (temperature, motion, door, energy) |
| `control_home_device` | 1 low | On / off / dim / set; reversible, confirmed where the gateway verifies |
| `run_home_routine` | 1 low | study · sleep · movie · leaving · project · emergency |

### Honest reachability — three states, never conflated

* **Offline** (default) — nothing is sent. A control says "No device was
  switched." and a routine lists what it *would* affect, then "Nothing was changed."
* **Simulated** — `JARVIS_HOME_SIMULATE=1` makes the gateway answer so the whole
  flow can be tested; every reply is tagged "(simulated; no real device was reached)".
* **Online, unverified** — a real transport is wired but has not confirmed the
  state echo, so the reply says "(sent to the gateway, not yet confirmed)".

### Wiring a real gateway

Set `JARVIS_HOME_MQTT_BROKER` (optionally `JARVIS_HOME_MQTT_USER` /
`JARVIS_HOME_MQTT_PASS`). If the broker is unreachable the gateway degrades to
offline and says so. Home is driven through the main `/command` pipeline —
there are no separate home REST routes.

## Watch Agent (v1.8)

The last interface in plan §4.4: notifications, vibration, confirmation prompts,
device status, an emergency command and quick routines. A watch cannot be
dialled from this process, so the companion **calls in** — JARVIS keeps an
outbox, and delivery is only ever claimed as far as the device has actually
confirmed.

```
you:  how is my watch                       # nothing paired yet
JARVIS: Your watch is not paired yet (pairing state: unpaired), so there is
        nothing to report from it.

you:  notify my watch "build finished"      # paired, no bridge configured
JARVIS: Notification "build finished" is queued on the laptop, not delivered.
        Nothing has reached your watch yet.

companion: POST /watch/collect  ->  POST /watch/ack {displayed: true}
JARVIS: Notification "build finished" is shown on the watch.
```

### The six tools

| Tool | Level | What it does |
|---|---|---|
| `get_watch_status` | 0 read | Pairing state, capabilities, battery **as reported by the watch**, outbox counts |
| `get_watch_outbox` | 0 read | Every prompt and how far it got |
| `send_watch_notification` | 1 low | Queue an alert; "queued" until the companion takes it |
| `vibrate_watch` | 1 low | Ask for a buzz (short / long / alert / sos); never claims the watch buzzed |
| `ask_watch_confirmation` | 1 low | Put a **pending confirmation** on the wrist with CONFIRM / CANCEL |
| `run_watch_routine` | 1 low | Quick routine (study, sleep, leave_home, arrive_home, emergency) — asks, does not run |

### Delivery is staged, because the stages are different

`queued → delivered → displayed → replied`. A companion taking an item proves it
holds it, not that the screen showed it; `ack(displayed=false)` is recorded as a
note and **not** as an answer, because "never shown" and "said no" are different
facts. A confirmation can only be answered after it reached the watch —
otherwise JARVIS could approve its own prompt and describe it as a wrist tap.

### Watch confirmation (plan §25)

`JARVIS_CONFIRM_CHANNEL=watch` — a setting that existed long before anything
could honour it — now genuinely routes pending approvals to the companion, and
the reply says how far the prompt got ("queued … nothing has reached it yet", or
"shown on your watch — answer CONFIRM or CANCEL there").

The wearer's answer arrives on `POST /watch/reply`, which is the **only** path
that can decide a confirmation from the watch side:

* a prompt carrying a `confirmation_id` resolves that one confirmation through
  the same expiry-checked resolver the CLI and dashboard use;
* a prompt carrying a quick routine dispatches its command through the normal
  pipeline with `source="watch"` — so "start study mode" on the wrist runs the
  real home routine, and "stop all physical actions" engages the emergency stop;
* CANCEL runs nothing, and a prompt can only be answered once.

No **tool** can resolve a confirmation: `ask_watch_confirmation` displays and
stops there, and a test asserts every watch tool stays at READ or LOW. Simulate
mode delivers prompts but never presses buttons, so it cannot self-approve
either.

### Wiring a real watch

`JARVIS_WATCH_BRIDGE=https://host/path` POSTs each item to a push relay; only a
`{"ok": true}` answer counts as shown, an HTTP 200 that says otherwise is
reported as "accepted, unconfirmed", and an unreachable bridge leaves items
queued with the reason kept. `JARVIS_WATCH_SIMULATE=1` attaches a
deterministic companion for demos.

## Dashboard: what is actually plugged in

The dashboard has a **Peripherals & Gateways** card fed by `/health.peripherals`,
and its whole job is to refuse to flatten four different situations into one red
dot:

| It says | It means |
|---|---|
| `adapter not connected` | nothing is configured for the car; ask about it and the tools say so |
| `configured · not read yet` | an adapter is set up, but nothing has asked it yet — *not* a failure |
| `not answering` | something was tried and it did not reply; the reason is shown |
| `simulated` | a demonstration gateway, never presented as real hardware |
| `state unavailable` | this gateway could not be read; the card says so instead of guessing why |

The devices card uses the same vocabulary, because pairing words only apply to
devices that pair — telling someone to "pair" a Home Assistant hub or an OBD-II
adapter would be wrong. `/health` reads these from the gateways' own cheap,
non-probing views: polling the dashboard every few seconds must never turn into
traffic on the car's serial link.

`not answering` is an accusation, so it requires evidence — a recorded
`bridge_error` or `adapter_error`. Something was configured, tried, and failed;
otherwise the card says `not configured`, `not read yet`, or plain `unknown`.
That rule was written into the helper too loosely at first and the page claimed
the home gateway was "not answering" when nothing had ever been tried.

## Car Agent (v1.7)

Plan §18 allows two ways to reach a car — a manufacturer vehicle API (Mode A)
and an OBD-II adapter (Mode B) — and Phase 10 asks for a **read-only prototype**.
Both transports are wired for real; neither is connected by default, so the
gateway is offline and every reading says so.

```
you:  check my car
JARVIS: Your car adapter is not connected, so I cannot read live telemetry yet
        (no car adapter or vehicle API is configured).

you:  check my car diagnostics
JARVIS: I cannot read the car's diagnostic codes (no vehicle source is connected).

you:  what is my car fuel            # with an OBD-II adapter wired
JARVIS: Fuel: 66.7 %.
```

"no faults stored" and "I cannot tell you" are different answers, and the code
keeps them different: `None` means the source did not answer, `[]` means it
answered with nothing. An unplugged adapter never reports a clean bill of
health.

### The five tools

| Tool | Level | What it does |
|---|---|---|
| `get_car_status` | 0 read | The readings this car actually reports, each labelled live / last-known / simulated |
| `get_car_telemetry` | 0 read | One or more named metrics (fuel, rpm, coolant, voltage, …) |
| `list_car_metrics` | 0 read | What the connected source exposes, and why the rest is missing |
| `get_car_diagnostics` | 0 read | Stored DTCs + malfunction indicator lamp, or an explicit "unknown" |
| `get_car_service_status` | 0 read | Odometer, temperatures, battery voltage, fault count — telemetry, not an inspection |

There is **no actuation tool**: no unlock, horn, ignition or remote start. The
whole surface stays in the READ tier, needs no confirmation, and a test asserts
that no car tool can raise above it.

### Capability comes from the car, not the catalogue

A metric is only reportable when the connected source says it exists:

* **OBD-II (Mode B)** — the vehicle's own PID 00 bitmasks at bases `0x00`,
  `0x20` and `0x40` decide. Ask for engine oil temperature on a car that never
  advertised PID `01 5C` and the answer is *"this vehicle does not implement PID
  01 5C"*.
* **Manufacturer API (Mode A)** — the JSON decides. A field that is absent or
  null is not exposed, so an EV reporting state of charge never gets an invented
  coolant temperature. Estimated range and odometer are backend figures: over
  OBD-II they are reported as unavailable, not guessed.

Decoding is the real SAE J1979 arithmetic — `(256A+B)/4` for RPM, `A-40` for
temperatures, `100·A/255` for percentages, `(256A+B)/1000` V — plus the J2012
DTC bit layout (`0x01 0x04` → `P0104`), the 0x00 end marker and the `0xFF`
empty-slot rule. Every one is pinned by a hand-computed test vector.

### Honest reachability — four states

* **Offline** (default) — no live readings. Anything the car reported earlier in
  this session is still offered, labelled *last known*, and a status reply says
  plainly that it is not a live read.
* **Simulated** — `JARVIS_CAR_SIMULATE=1` connects a deterministic vehicle whose
  every line is tagged `(simulated)`. It exposes only a subset of metrics, so
  simulate mode cannot be used to fabricate a reading either.
* **OBD-II** — `JARVIS_CAR_OBD=host:port[@protocol]` (default port 35145,
  protocol 6 = CAN 500 k). A real ELM327 session: `ATZ / ATE0 / ATL0 / ATSP`,
  single-frame and ISO-15765 multi-frame replies, and a VIN only accepted when
  all 17 characters arrive.
* **Manufacturer API** — `JARVIS_CAR_API_URL` (+ `JARVIS_CAR_API_TOKEN`,
  `JARVIS_CAR_API_PATH`), fetched over HTTPS with stdlib `urllib`.

A configured source that cannot be reached degrades to offline and keeps the
reason, so the reply names the host that did not answer instead of implying
there is no car.

### Wiring a real car

Set `JARVIS_CAR_OBD=192.168.1.9:35145` for a Wi-Fi ELM327 bridge, or
`JARVIS_CAR_API_URL=https://…` for a vehicle backend — the API wins if both are
set. Like home and robotics, the car runs through the main `/command` pipeline;
there are no separate car REST routes.

## Vision Agent (v1.3)

The plan's pipeline for vision is:

```
SEE → UNDERSTAND → PROPOSE → PERMISSION → ACT
```

and the rule is explicit: **vision must never automatically perform an action
merely because it sees something.**

That is enforced by structure, not discipline: **the Vision Agent owns no action
tools at all.** No click, no type, no move, no delete. It observes and proposes,
and a test asserts the agent has none of them. Vision stops at PROPOSE.

```
you:  what's on my screen
JARVIS: Captured /Users/…/jarvis_screen_….png. I read 14 line(s) of text from it.

you:  read the text in ~/Desktop/invoice.png
JARVIS: I read 9 line(s) of text: …
```

### What is genuinely available here

| Capability | How |
|---|---|
| **OCR** | macOS Vision framework, via a cached Swift helper — real text, bounding boxes, confidence |
| **Measurement** | Pillow — size, format, dominant colours, brightness, contrast |
| **Screen capture** | `screencapture`, then OCR |
| **Change detection** | pixel diff with a located changed region |

No OCR library was installed and no vision model is configured, so OCR is done by
compiling a small helper against the framework macOS already ships. If that can't
be built, `read_image_text` says so rather than returning invented text.

**A stalled recogniser degrades instead of stalling everything.** macOS Vision
intermittently never returns under load (measured: 0.6 s for one image standalone,
over 120 s for the same file while other work ran). Each read is therefore bounded
(`JARVIS_OCR_TIMEOUT`, default 20 s, clamped to 1–120 s) and after a miss the
recogniser is left alone for 45 s rather than every later question waiting again.
A timed-out read answers "no text was read"; it never returns a guess. `/health`
and `vision_capabilities` report **present** and **answering** as two facts,
because a cached binary is not a working one.

The helper also **stalls** sometimes — measured at 0.6 s for one image and past
two minutes for the same image while other work was running. So a read is bounded
to 20 s (`JARVIS_OCR_TIMEOUT`, clamped to 1–120 s), a recogniser that misses it is
left alone for 45 s rather than making every later question wait again, and each
timed-out read answers "no text was read" with no invented content. `/health` and
`vision_capabilities` report *present* and *answering* as two separate facts,
because a cached binary is not a working one.

### What it does not do

Describing *content* ("there's a cat on the desk") needs a vision model. None is
configured, so those requests say so:

> No vision model is configured. I can measure images and read text, but I
> cannot describe what is depicted.

Locating text finds **words**, not widgets — JARVIS can tell you where "Submit"
is, not where "the blue button" is.

### API

```bash
TOKEN=$(cat ~/.jarvis-forge/api_token)

curl -H "X-JARVIS-Token: $TOKEN" -X POST http://127.0.0.1:8765/vision/inspect \
     -H 'Content-Type: application/json' -d '{"path":"/tmp/photo.jpg"}'
curl -H "X-JARVIS-Token: $TOKEN" -X POST http://127.0.0.1:8765/vision/read \
     -H 'Content-Type: application/json' -d '{"path":"/tmp/invoice.png"}'
curl -H "X-JARVIS-Token: $TOKEN" http://127.0.0.1:8765/vision/capabilities
```

---

## Design Agent (v1.2)

Generates UI concepts, architecture and system diagrams, documentation and
presentations — and keeps the results as linkable assets.

**The artifacts are real.** A "diagram" is valid standalone SVG; a "UI concept"
is self-contained HTML that opens offline; documentation is real Markdown. Tests
parse every generated file, because a diagram that is not SVG would be exactly
the failure this agent must not have.

```
you:  draw a diagram of the JARVIS pipeline
JARVIS: Created diagram: diagram of the JARVIS pipeline
        -> ~/.jarvis-forge/design/jarvis-pipeline-929e34fc.svg

you:  design a dashboard mockup
JARVIS: Created ui_concept: dashboard mockup
        -> ~/.jarvis-forge/design/dashboard-9b9a72f1.html
```

### What it produces

| Tool | Output | Notes |
|---|---|---|
| `create_architecture_diagram` | SVG | Tiered components, labelled arrows |
| `create_flowchart` | SVG | Steps, with decision diamonds |
| `create_ui_concept` | HTML | Self-contained wireframe, no external requests |
| `create_documentation` | Markdown | Structured outline |
| `create_presentation` | HTML | Slide deck with keyboard navigation |
| `analyze_design` | report | Measurable checks, not aesthetic judgement |

Diagram input is plain text, so it can come from speech:

```
components = "ui:Web UI:0, api:API server:1, db:Database:2"
links      = "ui>api, api>db"
```

Anything unparseable is **reported and skipped**, never invented into a box.

### Honest labelling

Three things JARVIS does not pretend about:

* **An outline is an outline.** Generated docs and decks contain placeholders
  and say so — a document that looks finished but is empty is worse than one
  that admits it is a skeleton.
* **A wireframe is a wireframe.** Every UI concept carries that label.
* **Origin is recorded.** `created_by` is `local` unless an external tool made
  it, so nothing is passed off as coming from a tool that was not used.

### Integrations

External design tools must be **configured and enabled** before use. Nothing is
on by default — local generation is the default backend, and
`/design/integrations` reports the state of each.

### API

```bash
TOKEN=$(cat ~/.jarvis-forge/api_token)

curl -H "X-JARVIS-Token: $TOKEN" -X POST http://127.0.0.1:8765/design/diagram \
     -H 'Content-Type: application/json' \
     -d '{"title":"Pipeline","components":"ui:UI:0, api:API:1","links":"ui>api"}'

curl -H "X-JARVIS-Token: $TOKEN" -X POST http://127.0.0.1:8765/design/ui \
     -H 'Content-Type: application/json' -d '{"title":"Dashboard","sections":"header,hero,cards"}'

curl -H "X-JARVIS-Token: $TOKEN" http://127.0.0.1:8765/design/assets
curl -H "X-JARVIS-Token: $TOKEN" http://127.0.0.1:8765/design/assets/ASSET-…/analyze
```

Assets live in JARVIS's own design directory — no arbitrary write paths — and can
be linked into the project graph as `design_asset` nodes.

---

## Coding Agent (v1.1)

Works inside controlled project workspaces, following the plan's workflow:

```
Request → Inspect → Plan → Permission → Modify → Test → Review → Report →
Optional commit
```

```
you:  open /tmp/myrepo as a workspace
JARVIS: Opened myrepo as a workspace (WS-…).

you:  search the code for def add
JARVIS: 1 match(es). First: pkg/core.py:1

you:  apply_code_change  # shown as a diff first, then confirmed
JARVIS: Changed pkg/core.py (backup at .jarvis_backups/core.py.20260920-….bak)
```

### The workspace boundary

Every path is checked before anything reads or writes. Refused outright:

| Attempt | Result |
|---|---|
| `/etc/hosts` | "That path is outside the workspace or protected." |
| `pkg/../../outside.py` | refused |
| a symlink inside the workspace pointing to `/etc` | refused |
| `~/.ssh/id_rsa` | refused (protected) |

A relative path like `pkg/core.py` resolves **against the workspace**, not the
process working directory — otherwise it could quietly land somewhere unrelated.

### What it will not do

Each of these is structural, not a promise:

* **Delete files.** There is no delete path in the changes module at all, so the
  repository cannot be removed by this workflow. A test asserts this.
* **Write outside the workspace.** Every path goes through the boundary.
* **Approximate an edit.** If the exact text to replace is not found, it refuses
  rather than substituting a similar-looking match.
* **Weaken the command allowlist.** Running tests means invoking an interpreter.
  This agent will *not* add one — that "would make the allowlist decorative"
  (see `config.py`). It reports the policy state instead:

  > No test runner is on the command allowlist. I will not add one myself —
  > that would make the allowlist decorative. Add it to `JARVIS_POWERFUL_COMMANDS`
  > if you want me to run tests here.

* **Push.** Committing requires `authorized=True` *and* `git` on the allowlist;
  push is not implemented at all.

### API

```bash
TOKEN=$(cat ~/.jarvis-forge/api_token)

curl -H "X-JARVIS-Token: $TOKEN" -X POST http://127.0.0.1:8765/workspaces \
     -H 'Content-Type: application/json' -d '{"path":"/tmp/myrepo"}'
curl -H "X-JARVIS-Token: $TOKEN" http://127.0.0.1:8765/workspaces/WS-…/inspect
curl -H "X-JARVIS-Token: $TOKEN" "http://127.0.0.1:8765/workspaces/WS-…/search?q=def+add"
curl -H "X-JARVIS-Token: $TOKEN" -X POST http://127.0.0.1:8765/workspaces/WS-…/propose \
     -H 'Content-Type: application/json' \
     -d '{"path":"pkg/core.py","old":"return a + b","new":"return a + b  # patched"}'
curl -H "X-JARVIS-Token: $TOKEN" http://127.0.0.1:8765/workspaces/WS-…/report
```

---

## Research Agent (v1.0)

Technical research with traceable sources, organised notes and honest briefs.

```
you:  research adaptive query optimization
JARVIS: I found 5 reference(s) on arxiv:
        Online Sketch-based Query Optimization (2021)
        Bitvector-aware Query Optimization … (2020)
        …

you:  create a research brief on adaptive query optimization
JARVIS: JARVIS RESEARCH BRIEF — adaptive query optimization
        Sources
          [izenov2021] Yesdaulet Izenov et al. (2021). Online Sketch-based
                       Query Optimization. arXiv:2102.02440v1.
        Findings
          Fact (from source) / Interpretation / Hypothesis / Unresolved
        Open questions
```

### Never fabricate a citation

This is structural, not a promise. A paper can only enter the store:

* from an **approved source backend** — the public arXiv API, which needs no key
  and returns real titles, authors and identifiers; or
* because **you added it explicitly** (stored as `unverified`, and labelled so).

There is no code path that turns a title into a citation. Ask for one that is
not stored and you get an error, not a plausible guess:

```
GET /papers/PAPER-fake-9999/cite
-> 404  "no stored paper 'PAPER-fake-9999'; I will not invent a citation for it"
```

Papers fetched from arXiv are marked `verified=True`. Papers you type in are
`verified=False` — the distinction is visible everywhere they appear.

### Source facts vs interpretation vs hypothesis vs unresolved

Every note carries a claim type, and the brief renders them separately:

| Type | Meaning |
|---|---|
| `source_fact` | Attributable to a stored paper — **requires** a `paper_id` |
| `interpretation` | JARVIS's reading. Opinion, not evidence |
| `hypothesis` | An idea to test. May be wrong |
| `unresolved` | An open or contradicted question |

A `source_fact` with no paper attached is **downgraded to an interpretation**
rather than presented as sourced:

```
POST /research/notes  {"claim": "…", "claim_type": "source_fact"}
-> recorded as interpretation
   "No source was attached, so this was recorded as an interpretation."
```

### Extraction, not invention

Methods, datasets and limitations are pulled from **stored abstracts**. If an
abstract states nothing, the result is empty and JARVIS says so — it does not
fill the gap. Likewise, "identify gaps" reports only recorded notes and
limitations the papers themselves declare:

> No gaps are recorded. I will not invent one.

### API

```bash
TOKEN=$(cat ~/.jarvis-forge/api_token)

curl -H "X-JARVIS-Token: $TOKEN" http://127.0.0.1:8765/research/sources
curl -H "X-JARVIS-Token: $TOKEN" -X POST http://127.0.0.1:8765/papers/search \
     -H 'Content-Type: application/json' -d '{"query":"learned indexes","source":"arxiv"}'
curl -H "X-JARVIS-Token: $TOKEN" http://127.0.0.1:8765/papers
curl -H "X-JARVIS-Token: $TOKEN" "http://127.0.0.1:8765/papers/PAPER-…/cite?style=bibtex"
curl -H "X-JARVIS-Token: $TOKEN" "http://127.0.0.1:8765/research/brief?topic=query+optimization"
```

Papers can be linked into the project graph as `paper` nodes.

---

## Advanced memory (v0.9)

Memory is now retrieved **hybridly** — keyword and vector together:

```
Query
  |
  +--> Keyword retrieval (BM25)   --+
  |                                 |
  +--> Vector retrieval (cosine)  --+
                                    |
                           Hybrid ranking (RRF)
                                    |
                           Relevant memories
```

Fusion uses **Reciprocal Rank Fusion** rather than a weighted sum, because BM25
scores and cosine similarities live on completely different scales — adding them
would let whichever happens to produce larger numbers dominate. RRF only looks at
*rank*, so both retrievers contribute comparably with no score calibration.

**Keyword retrieval is never removed.** If the vector side is unavailable, search
degrades to BM25 alone and says so in the result (`retrieval.mode`).

### The embeddings, stated honestly

No numeric library is installed (`numpy`, `scipy`, `sklearn`, `torch` are all
absent) and no embedding API is configured. So the default embedder is a
dependency-free **lexical** vector: the hashing trick over word tokens and
character trigrams, L2-normalised, 256 dimensions.

That gives fuzzy matching beyond exact keyword overlap — typo tolerance and
morphology — and it needs no download, no network and no key. It does **not**
understand paraphrase. `RemoteEmbedder` is the upgrade path: configure a model
endpoint and the rest of the system is unchanged.

```
query: "what material is the frame made of"
  -> mode=hybrid, found_by=['vector']   # zero shared keywords
     "the AMR chassis uses 3mm aluminium"
```

One trap worth noting: Python's built-in `hash()` is **randomised per process**,
so it must never be used here — every stored vector would be invalidated on
restart. All hashing goes through `hashlib`.

### Memory types

`preference`, `fact`, `task`, `project`, `decision`, `experiment`, `failure`,
`fix`, `document`, `conversation`, `device`, `relationship`.

Each memory carries a type, importance (0–1), confidence, optional expiry and
relationships. Pre-v0.9 memories get vectors backfilled automatically on startup.

### User controls

```bash
TOKEN=$(cat ~/.jarvis-forge/api_token)

curl -H "X-JARVIS-Token: $TOKEN" http://127.0.0.1:8765/memory/overview
curl -H "X-JARVIS-Token: $TOKEN" "http://127.0.0.1:8765/memory/search?q=gearbox"
curl -H "X-JARVIS-Token: $TOKEN" http://127.0.0.1:8765/memory/export
curl -H "X-JARVIS-Token: $TOKEN" -X POST http://127.0.0.1:8765/memory/MEM-abc123/forget
curl -H "X-JARVIS-Token: $TOKEN" -X POST "http://127.0.0.1:8765/memory/clear?scope=session"
```

Inspection, export, deletion, clearing a scope and disabling categories are all
available — including conversationally ("what do you remember", "clear my session
memory", "stop remembering preferences"). Deletion is destructive and confirmed.
**Exports withhold `private` and `secret` memories** unless you explicitly ask
for them, because an export is a file that tends to get copied around.

---

## Recovery (section 47)
Agents fail. The plan requires a defined response rather than a dead end:

```
Tool failure → Retry if safe → Alternative tool → Ask user → Record failure
```

Two rules make this safe rather than optimistic:

1. **Destructive actions are never retried automatically.** A retry assumes the
   first attempt changed nothing — false for `delete_file`, `move_file`, or any
   physical action. A second attempt could act on a world you did not approve.
2. **Deterministic errors are not retried.** "File not found" will not fix
   itself, so the retry budget is not burned on it.

Read-only and idempotent tools retry with exponential backoff; tools with a
declared fallback (e.g. Spotlight search → directory walk) try that next.
Every failure and its outcome is recorded so future planning can use it.

**Recoveries are always reported**, never hidden behind a clean success:

```
Your laptop battery is at 74% (charging).

(Recovered automatically — system_status: succeeded on retry 1)
```

---

## Privacy (section 49)

**Redaction is always on.** Credentials are stripped before they reach the
audit log, the log files, or a model prompt — which is what makes section 26's
"never place API keys directly into prompts" true rather than aspirational.
It targets exact known secrets plus credential *shapes*, and is verified not to
mangle ordinary text (false positives would corrupt the log).

**Privacy mode** (`JARVIS_PRIVACY_MODE=true`, or `POST /privacy/mode`) is
additive: it additionally masks personal identifiers and collapses
home-directory paths to `~`.

**Data control:**

```bash
# See what would be removed — deletes nothing
curl -H "X-JARVIS-Token: $(cat ~/.jarvis-forge/api_token)" \
     -X POST http://127.0.0.1:8765/privacy/preview \
     -H 'Content-Type: application/json' -d '{"scope":"memories"}'

# Export everything (redacted by default)
curl -H "X-JARVIS-Token: $(cat ~/.jarvis-forge/api_token)" \
     http://127.0.0.1:8765/privacy/export

# Delete a scope — requires confirm=true, it is irreversible
curl -H "X-JARVIS-Token: $(cat ~/.jarvis-forge/api_token)" \
     -X POST http://127.0.0.1:8765/privacy/delete \
     -H 'Content-Type: application/json' \
     -d '{"scope":"memories","confirm":true}'
```

Scopes: `memories`, `conversations`, `audit`, `projects`, `tasks`, `everything`.
Exports and the token file are written `0600`.

---

---

## Layout

```
jarvis-forge/
├── run.py                     # serve | cli | voice | demo | voice-demo
├── build_app.py               # assemble Jarvis Forge.app
├── install_app.sh             # build + install into /Applications
├── pyproject.toml
├── requirements.txt
├── .env.example
├── app/
│   ├── launcher.py            # app entrypoint: server + control window
│   └── make_icon.py           # renders the .icns
├── dashboard/index.html       # live control dashboard (incl. voice panel)
├── jarvis/
│   ├── config.py              # env-driven settings
│   ├── logging_setup.py       # console + JSONL logs
│   ├── core/
│   │   ├── models.py          # RiskLevel, PolicyDecision, Plan, ActionResult...
│   │   ├── planner.py         # deterministic intent -> plan
│   │   ├── orchestrator.py    # the pipeline
│   │   ├── recovery.py        # retry / fallback / failure log
│   │   └── tasks.py           # job manager
│   ├── registry/              # tools, devices, applications
│   ├── security/              # risk, policy, audit, paths, auth, privacy
│   ├── agents/                # 15 agents: laptop, laptop_center, memory,
│   │                          #   phone, email, calendar, scheduler, research,
│   │                          #   coding, design, vision, home, robotics, car, watch
│   ├── tools/                 # apps, files, system, memory, phone, email,
│   │                          #   calendar, scheduler, research, coding,
│   │                          #   design, vision, home, robotics, car, watch
│   ├── home/                  # HomeGateway (offline by default) + routines
│   ├── car/                   # CarGateway + metrics (OBD-II / vehicle API)
│   ├── watch/                 # WatchGateway (outbox, staged delivery, bridge)
│   ├── memory/                # store + project graph
│   ├── gateway/               # model router
│   ├── voice/                 # audio, tts, stt, wakeword, session
│   ├── devices/               # pairing, device identity, key store
│   ├── transport/             # TLS, protocol, transfer server + endpoint
│   │   └── native/            # macOS Speech Swift helper
│   ├── db/database.py         # SQLite schema (section 38)
│   └── api/app.py             # FastAPI + dashboard route
└── tests/                     # 763 tests
```

---

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/command` | Run a natural-language command |
| `GET` | `/command/stream` | Stream a command as SSE |
| `POST` | `/confirm/{id}?approve=true` | Approve/deny a pending action |
| `GET` | `/confirmations` | Pending approvals |
| `GET` | `/devices` `/tools` `/tasks` | Registries and jobs |
| `GET` | `/audit` | Recent audit records |
| `GET` `POST` | `/memory` | Read / write memory |
| `GET` | `/voice/status` | Voice subsystem report |
| `POST` | `/voice/command` | Run text through the voice pipeline |
| `POST` | `/voice/speak` | Synthesise speech |
| `POST` | `/voice/transcribe` | Transcribe an uploaded clip |
| `POST` | `/voice/interrupt` | Barge-in |
| `GET` | `/dashboard` | Control dashboard |
| `GET` | `/auth/token` | Token for a same-origin client (dashboard) |
| `GET` | `/metrics` `/failures` | Failure + recovery counters, audit chain state |
| `POST` | `/devices/pair/request` | Device asks to pair (**unauthenticated**, grants nothing) |
| `GET` `POST` | `/devices/pair/{id}/approve` `deny` `pending` | Pairing approval |
| `POST` | `/devices/{id}/revoke` | Revoke trust, destroy the key |
| `GET` `POST` | `/transfers`, `/transfers/{id}/cancel` | Transfer history and cancellation |
| `POST` | `/tasks/{id}/{pause,resume,retry,cancel}` | Job lifecycle |
| `GET` `POST` | `/privacy`, `/privacy/export`, `/privacy/preview`, `/privacy/delete` | Data control |
| `GET` | `/health` | Liveness, capability + security posture |

All endpoints except `/`, `/dashboard` and `/auth/token` require the
`X-JARVIS-Token` header (or `Authorization: Bearer`). Interactive docs at `/docs`.

---

## Tests

```bash
$PY -m pytest -q
# 954 collected -> 953 passed, 1 skipped. The skip is the live arXiv test, which
# refuses to invent a result when the network will not answer.
$PY -m pytest -q -m "not slow"     # skip the network-bound ones
```

Coverage: risk classifier, command guard, path safety, tool/device/app
registries, planner intents, memory store, project graph, permission engine
(including grants/revocations), the full pipeline, the HTTP API, the voice
pipeline (wake word, TTS, STT selection, session, follow-ups, barge-in), the
home agent (honest offline), the car agent — J1979 decoders against
hand-computed vectors, an ELM327 session over a real socket, manufacturer-API
capability gating, and the read-only guarantee — and the watch agent, whose
tests pin the delivery stages against being conflated and prove a wrist tap, and
nothing else, can resolve a confirmation.

---

## Next steps (section 57 build order)

1. ~~Voice~~ — done (wake word, STT/TTS, interruption, streaming)
2. ~~Phone Agent + secure file transfer~~ — done in v0.6
3. ~~Email + Calendar integrations~~ — done in v0.7
4. ~~Scheduler + daily briefing~~ — done in v0.8
5. ~~Vector search for memory~~ — done in v0.9
6. ~~Research / Design / Coding agents~~ — done in v1.0–v1.2 (Vision in v1.3)
7. ~~Home / IoT Agent~~ — done in v1.5
8. ~~Robotics Agent~~ — done in v1.6
9. ~~Car Agent~~ — done in v1.7 (read-only telemetry; actuation deliberately not built)
10. ~~Watch Agent~~ — done in v1.8 (outbox companion, staged delivery, watch-side confirmations)

The plan's build order is now complete through v1.8. What remains is wiring real
peripherals to surfaces that already refuse honestly without them: a paired
watch, an OBD-II adapter or vehicle API, a home gateway, a robot bridge, IMAP /
CalDAV accounts, and a Developer ID signature for on-device speech recognition.

Each of these is another **authenticated agent/tool**, not a rewrite — which is
the whole point of the architecture.
