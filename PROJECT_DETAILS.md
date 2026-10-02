# Vantage — Full Project Details

**Generated:** 25 September 2026
**Machine:** MacBook Air (Apple Silicon, macOS 26, arm64) · project at `/Users/abhijeetmiskin/AppData/aisys/jarvis-forge/`

Every number and quoted response in this document was produced by running the
system on this machine today — not copied from the plan or from an earlier
report. `STATUS.md` remains the project's living status file; this document is
the complete inventory behind it. Commands to reproduce anything here are in
§10.

---

## 1. What it is

A permissioned, multi-agent personal AI operating system. One request travels a
fixed pipeline, and no tool runs outside the permission engine:

```
utterance → Intent → Plan → Permission → Tool → Execution → Verification → Memory → Response
```

Risk is graded per tool, and the grade decides what happens next:

| Tier | Meaning | Count | Behaviour |
|---|---|---|---|
| `READ` | observes, changes nothing | 68 | runs silently |
| `LOW` | local, reversible | 33 | runs, logged |
| `EXTERNAL` | crosses a boundary (file copy, send, remote call) | 11 | confirmation |
| `DESTRUCTIVE` | removes or commits | 11 | confirmation |
| `PHYSICAL` | moves real hardware | 3 | confirmation, **two** where the plan requires it |

**126 tools · 15 agents · 100 API paths · 36 intent handlers · 24 tools require confirmation.**

The governing rule of the codebase: *an unimplemented capability must say so.*
Nothing may report success it did not measure. That rule is what makes §7 and §8
below unusually negative — and they are the honest part.

---

## 2. Scores

| Metric | Result |
|---|---|
| Tests collected | **976** |
| Passing | **975** |
| Failing | **0** |
| Skipped | 1 (the live arXiv test skips itself when the network refuses) |
| Wall clock | ~25 s for the whole suite |
| Source | 126 modules · ~29,700 lines · 26 test files |
| Desktop app | 544K, `codesign --verify` → *valid on disk*, *satisfies its Designated Requirement* |

### Per-area test counts

| Area | Tests | Area | Tests |
|---|---|---|---|
| `test_car.py` | 84 | `test_coding.py` | 46 |
| `test_api.py` | 67 | `test_voice.py` | 58 |
| `test_watch.py` | 65 | `test_phone.py` | 38 |
| `test_memory_advanced.py` | 60 | `test_robotics.py` | 30 |
| `test_security.py` | 52 | `test_home.py` | 29 |
| `test_email_calendar.py` | 52 | `test_pipeline.py` | 28 |
| `test_research.py` | 50 | `test_planner.py` | 27 |
| `test_design.py` | 50 | `test_laptop_center.py` | 25 |
| `test_scheduler.py` | 48 | `test_recovery.py` | 22 |
| `test_vision.py` | 48 | `test_privacy.py` | 22 |
| | | `test_risk.py` 18 · `test_transfer.py` 17 · `test_paths.py` 15 · `test_policy.py` 13 · `test_registry.py` 6 · `test_memory.py` 6 | |

---

## 3. Live acceptance (captured today, `run.py demo`)

Real output from a fresh isolated instance. Note that several rows are
*refusals* — those are the feature working.

| Input | Intent | Decision | Actual response |
|---|---|---|---|
| `open VS Code` | `open_app` | EXECUTE | "Opening Visual Studio Code on your macos machine." |
| `find my latest project report` | `search_files` | EXECUTE | "I found 198 file(s) matching 'project'. Most recent: projects.data (3 min ago)." |
| `what is my laptop battery` | `laptop_battery` | EXECUTE | "Your laptop battery is at 100% (fully charged)." |
| `what time is it` | `time` | EXECUTE | "It is 23:36 on Thursday, 24 September 2026." |
| `take a screenshot` | `screenshot` | EXECUTE | "That did not work: Screenshot failed: could not create image from display. On macOS this usually means Screen Recording permission has not been granted…" |
| `remember that this is my AMR project` | `remember` | EXECUTE | "Got it. I will remember: this is my AMR project" |
| `check my car fuel` | `car_telemetry` | EXECUTE | "I could not read those car values (no car adapter or vehicle API is configured)." |
| `give me today's location summary` | `location_summary` | EXECUTE | "Location history for today is not available: location tracking is disabled." |
| `transfer this file to my phone` | `transfer` | EXECUTE | "I will not guess which file to send. Tell me the exact file…" |
| `plan my day` | `daily_schedule` | EXECUTE | "Your today is clear." |
| `delete everything` | `blocked` | **BLOCK** | "I will not do that automatically: blanket delete request." |
| `stop all physical actions` | `emergency_stop` | EXECUTE | "Emergency stop engaged. Physical actions cancelled; safety controllers remain active." |

The same sweep runs through the voice path (`run.py voice-demo`) with wake-word
stripping and TTS: `wake words: ['jarvis', 'hey jarvis', 'ok jarvis']`,
`tts backend: macos_say`, `stt backend: macos_speech`, and identical decisions.

**Model posture on this machine: `llm_enabled: false` — every decision above was
made by the deterministic rules engine, with no cloud model configured.**

---

## 4. Agents and their tools

Risk letters: **R** read · **L** low · **E** external · **D** destructive ·
**P** physical. `*` = requires confirmation before it can run.

### laptop (17) — the machine itself
`battery_status`R · `compress_path`L · `copy_file`E* · `delete_file`D* · `emergency_stop`P · `get_location_summary`R · `get_time`R · `list_apps`R · `list_directory`R · `move_file`E* · `open_app`L · `read_file`R · `run_allowlisted_command`E* · `search_files`R · `system_status`R · `take_screenshot`L · `close_app`L

### memory (13) — persistence and recall
`add_project_node`L · `clear_memory_scope`D* · `create_project`L · `explain_failures`R · `export_memories`R · `forget_memory`D* · `inspect_memory`R · `memory_overview`R · `project_summary`R · `remember_fact`L · `search_memory`R · `set_memory_category`L · `sweep_expired_memories`L

Retrieval is **hybrid**: keyword + vector embeddings fused with RRF, plus recency ranking. No third-party vector store; embeddings are dependency-free.

### research (13) — approved sources only
`add_paper`L · `add_research_note`L · `cite_paper`R · `compare_papers`R · `extract_paper_datasets`R · `extract_paper_limitations`R · `extract_paper_methods`R · `find_research_gaps`R · `forget_paper`D* · `link_paper_to_project`L · `list_papers`R · `research_brief`R · `search_papers`L

Live against arXiv. Citations are never fabricated; a claim carries typed provenance back to the paper it came from.

### design (11) — real artifacts on disk
`analyze_design`R · `create_architecture_diagram`L · `create_documentation`L · `create_flowchart`L · `create_presentation`L · `create_ui_concept`L · `delete_design_asset`D* · `design_integrations`R · `link_asset_to_project`L · `list_design_assets`R · `read_design_asset`R

Emits genuine SVG / self-contained HTML / Markdown, not descriptions of them.

### coding (10) — propose, then apply
`apply_code_change`E* · `close_workspace`L · `coding_report`R · `commit_workspace`D* · `inspect_workspace`R · `list_workspaces`R · `open_workspace`L · `propose_code_change`L · `run_workspace_tests`E* · `search_code`R

Hard-bounded to a workspace, no delete path exists, and commit only happens through the permission engine.

### calendar (8) · scheduler (8)
`cancel_event`D* · `create_event`E* · `daily_schedule`R · `find_free_time`R · `list_events`R · `next_event`R · `search_events`R · `update_event`E*
`cancel_schedule`D* · `daily_briefing`R · `get_schedule`R · `list_schedules`R · `pause_schedule`L · `resume_schedule`L · `run_schedule_now`E* · `schedule_task`E*

Local SQLite calendar is live; recurring schedules run with a permission ceiling enforced, so a job cannot escalate beyond what its creator could do.

### email (5) · phone (10)
`draft_email`L · `read_email`R · `search_email`R · `send_email`D* · `summarize_email`R
`approve_pairing`D* · `cancel_transfer`L · `get_device_capabilities`R · `get_device_status`R · `get_transfer_status`R · `list_devices`R · `ping_device`R · `register_phone`E* · `remove_phone`D* · `send_to_device`E*

Local maildir is live and reads real `.eml` files; sending always confirms. Device pairing is a request/approve/deny flow with a TLS identity per device.

### vision (6) — looks, never touches
`capture_screen`R · `compare_images`R · `find_image_text`R · `inspect_image`R · `read_image_text`R · `vision_capabilities`R

Real OCR via the macOS Vision framework (a Swift helper compiled on demand), plus pixel measurement and image diff. Reports *present* and *answering* as two separate facts.

### home (5) · robotics (5) · car (5) · watch (6) — the hardware-facing four
`control_home_device`L · `get_home_reading`R · `get_home_status`R · `list_home_devices`R · `run_home_routine`L
`command_robot_motion`P* · `get_robot_status`R · `get_robot_telemetry`R · `list_robots`R · `run_experiment`P*
`get_car_diagnostics`R · `get_car_service_status`R · `get_car_status`R · `get_car_telemetry`R · `list_car_metrics`R
`ask_watch_confirmation`L · `get_watch_outbox`R · `get_watch_status`R · `run_watch_routine`L · `send_watch_notification`L · `vibrate_watch`L

### laptop_center (4)
`build_agent_prompt`L · `laptop_processes`R · `laptop_project_status`R · `laptop_snapshot`R

---

## 5. What is genuinely working

| Capability | Evidence from today |
|---|---|
| Intent → plan → permission → tool → verify → remember | full demo sweep above, 14/14 correct dispositions |
| Refusal before planning | `delete everything` → BLOCK, no tool was ever considered |
| Text to speech | `macos_say` backend, speaks on request from the dashboard |
| Memory write + hybrid recall | `remember …` then `what did I work on yesterday` returned the stored turn |
| File search, directory listing, read | 198 matches returned with mtime |
| App control | `open VS Code` launched it |
| Battery / time / system status | live values, not placeholders |
| Local mail + calendar | `/integrations` → `active: local_maildir`, `active: local_calendar` |
| Research over arXiv | real fetches; citation paths refuse when a paper isn't in the store |
| Design artifacts | real SVG/HTML/Markdown files written to the asset store |
| Coding workspace flow | open → inspect → propose → apply (with backup) → test → report |
| Vision OCR | `/vision/capabilities` today: `present: true`, `responsive: true` |
| Scheduler | recurring jobs with permission ceiling |
| Privacy redaction | `redaction_enabled: true`, export + delete scopes available |
| Audit | append-only, hash-chained, `/audit` returns sealed records |
| Auth | token required on every state-reading route, CORS locked to the loopback origin, Host header enforced |
| Recovery | failures counted and exposed: `/metrics` → `by_tool: take_screenshot: 2` |
| Desktop app | launches, self-tests, serves the dashboard |

---

## 6. What works but is *deliberately offline*

These four agents are fully built and correctly wired. They refuse because the
thing they would talk to is not connected — which is the designed behaviour, not
a gap in the code.

Measured posture from `/health` → `peripherals`:

| Agent | Live state | What it says |
|---|---|---|
| car | `configured: false`, `via: not configured`, `probed: false` | "I cannot read live telemetry yet" — never an invented number |
| home | `mode: offline`, `online: false`, 17-device catalogue held locally | "No device was switched." |
| robotics | `mode: offline`, `robots: 2`, `faults: []` | "no motion command was sent"; PHYSICAL ⇒ confirmation |
| watch | `auth_state: unpaired`, `mode: queue`, all delivery counts 0 | "nothing has reached your watch" |

The car path is real protocol code, not a stub: SAE J1979 PID decoders, PID-00
support bitmasks read at bases 0x00/0x20/0x40, ISO-15765 frame reassembly, and
the ELM327 AT command set over TCP. **Capability comes from the car's own
answers, never from the catalogue** — an unread PID is reported as unknown
(`None`), distinctly from "the car answered and supports nothing" (`[]`).
There is no actuation path in the car agent at all; it is read-only by
construction.

The watch enforces staged delivery `queued → delivered → displayed → replied`.
A confirmation **cannot be answered until it has reached the wrist** — replying
to something still queued returns "That prompt has not reached your watch, so
there is nothing to answer yet." No tool can resolve a confirmation; only the
companion's reply can.

---

## 7. Hardware reality on this machine (measured, 24 Sep)

This was surveyed directly rather than assumed:

| Probe | Result |
|---|---|
| USB tree (`system_profiler SPUSBDataType`) | **empty** — nothing attached |
| Serial devices (`/dev/cu.*`) | only macOS defaults (`Bluetooth-Incoming-Port`, `debug-console`, `wlan-debug`) — no OBD-II/ELM327 |
| Bluetooth | adapter on; iPhone, Redmi phone, PS headset **in range, none connected**; **no watch visible** |
| Microphones (AVFoundation) | **3 real inputs**: `MacBook Air Microphone`, `Microsoft Teams Audio`, `MJAudioRecorder` — all idle |
| Camera | `FaceTime HD Camera` present; video consent `notDetermined` |
| LAN | `172.20.169.171` on a shared corporate subnet — **not swept**; probing neighbours' machines would be attacking other people's devices |
| Wi-Fi | `networksetup` reports not associated on en0 (address is from the managed interface) |
| Speech/OCR toolchain | `swiftc` 6.3.3 present; `ffmpeg` present; **no** whisper / whisper.cpp / vosk / sox / sounddevice / numpy |

**Consequence:** there is no reachable peripheral to attach. The hardware-facing
agents stay in honest-offline mode until *you* bring an adapter, a broker or a
credential — and the earlier "wire a real device you own" pass turned up
something more valuable instead: a health claim that was lying (§8).

---

## 8. Known limitations — all measured, none assumed

| Limitation | Measured evidence | State of the fix |
|---|---|---|
| **Microphone capture blocked by consent** | 3 mics idle, `ffmpeg` installed, yet `ffmpeg -f avfoundation` **hangs forever**; AVFoundation reports `denied` for an unbundled process | **Fixed the lie.** `mic_state.swift` now probes real consent; `capture_available` requires backend **and** grant; capture refuses *before* spawning ffmpeg (**0.96 s** warm vs a 17 s stall that then guessed the cause). Only a signed `.app` can be granted, and the bundle already declares `NSMicrophoneUsageDescription` |
| ↳ and its own first-run trap | the freshly compiled helper answered `unknown` on its very first exec — macOS spends ~8 s assessing a binary it has never run, past the 5 s steady-state budget | Fixed: the first exec after a build gets a 45 s budget, later reads keep 5 s. Verified live on a clean home — first call now answers `blocked / denied / 3 devices` in 3.4 s |
| **On-device speech recognition unavailable** | Apple grants Speech Recognition only to a real **Developer ID**; ad-hoc signing is refused (measured) | Falls back to `transcript`. Fix = Developer ID signature, or install whisper.cpp, or set `JARVIS_STT_BACKEND=openai_whisper` |
| **Screenshots fail here** | `/metrics` records 2 failures: "could not create image from display" | Screen Recording permission not granted to the host app — a checkbox, not code |
| **Vision OCR is intermittent, not broken** | same image measured at **0.6 s** standalone and **>120 s** under load | Bounded to 20 s (`JARVIS_OCR_TIMEOUT`, clamped 1–120), stalled recogniser **left alone for 45 s** rather than every later question paying again; a timeout returns "no text was read", never guessed text |
| **Continuous acoustic wake word** | interface reserved, no engine | `wake.mode = transcript_matching` — wake words are detected in the *transcript*, not in audio |
| **Pre-chain audit rows** | early records carry no hash | sealed at startup; sealing cannot retroactively protect unhashed data |
| ~~Built app was stale~~ **fixed 25 Sep** | `dist/…/Resources/launcher.py` held the pre-fix microphone line | Rebuilt; bundle launcher now `diff -q` identical to `app/launcher.py`, signature valid, 544K |
| ~~Dead entries in the honesty table~~ **fixed 25 Sep** | `NOT_IMPLEMENTED` still listed `email`, `calendar`, `transfer`, but only `phone` was read anywhere | Renamed to `UNAVAILABLE_REASONS` with only the live case, reworded from "the Phone Agent is not connected in this build yet" (false — it is built) to "no phone is paired with this laptop yet" |
| **A regression the probe itself introduced** | Making consent measurable made `/voice/status` compile Swift *inside the request*; the desktop app's own health check uses a 3 s client timeout and timed out on every cold home | Measurement moved to a background thread, warmed during server startup, and cached: `/voice/status` now answers in **~2 ms** warm, 42 ms worst-case cold, and the app self-test went from `voice subsystem — timed out` to passing. Capture still waits for a first reading, so it cannot slip past the probe into an ffmpeg hang |
| **Three §4 acceptance rows were quoting lies** | `send an email…`, `transfer this file…` and `plan my day` were documented as replying "not configured" — true on 19 Sep, false once the local maildir and calendar went live | Re-captured and replaced with what the system actually says today. The lesson is the project's own rule in reverse: a refusal that outlives its cause is as untrue as a fake success |
| **"Measuring" was reported as "unreadable"** | the async probe answered `unknown` both while the first measurement was in flight and on a machine that can never measure (no `swiftc`) — one word for two opposite facts | Added a fourth state, `measuring`, with its own wording in `/voice/status`, the dashboard and the launcher, and made the launcher keep polling through it. Verified live: first request returns `measuring` in 0.05 s, settles to the real answer in 3.0 s |

---

## 9. What has to be added

### A. Nothing in the plan's build order is missing
v0.1 → v1.8 is complete: phone + transfer, email/calendar, scheduler/briefing,
advanced memory, research, coding, design, vision, home, robotics, car, watch.
Each arrived as another authenticated agent/tool on the existing pipeline, not a
rewrite.

### B. Attachments that need something only you can supply

| To enable | Set / do | Then JARVIS will |
|---|---|---|
| Car over OBD-II | `JARVIS_CAR_OBD=host:port[@protocol]` (ELM327 on the LAN) | read real PIDs, report only what the car supports |
| Car over vehicle API | `JARVIS_CAR_API_URL` + `JARVIS_CAR_API_TOKEN` | read range/charge/odometer via Mode A HTTPS |
| Home automation | `JARVIS_HOME_MQTT_BROKER` (+ user/pass) | toggle real devices; still never claim a switch it can't confirm |
| Robotics | `JARVIS_ROBOT_BRIDGE` | send motion commands, PHYSICAL-tier confirmation first |
| Watch | a companion that calls `POST /watch/collect` → `/ack` → `/reply`, optionally `JARVIS_WATCH_BRIDGE` | staged delivery and on-wrist approvals |
| Real email | IMAP/SMTP host + address + password | send/receive; credentials stay redacted in logs |
| Real calendar | CalDAV credentials | sync events |
| Cloud model | `JARVIS_LLM_*` (currently `llm_enabled: false`) | natural-language planning instead of rules |
| Native speech | Developer ID signature, or whisper.cpp | on-device transcription |
| Cloud vision | vision model endpoint | content understanding (`understands_content` is false locally) |
| Voice + camera consent | approve the prompt from the signed app | listening and screen capture |

### C. Small, real cleanups found today
Done in this pass: ~~rebuild the app~~, ~~prune `NOT_IMPLEMENTED`~~, ~~the
synchronous-probe timeout in `/voice/status`~~, ~~the three stale §4 rows~~,
~~add the drift guard~~, ~~split "measuring" from "unreadable"~~, and ~~cover
the probe's failure paths~~ (six parameterised cases: non-macOS, no `swiftc`,
compile failure, helper non-zero exit, unreadable output, helper timeout — each
must yield `unknown` plus a specific reason and never a guessed permission).
Two of those were caught by my own tests while writing them: a patch left over
from an earlier assertion in the same test, and the drift guard failing because
I had edited `app/launcher.py` without rebuilding.

Still open:

1. **The desktop self-test still exits 1 on this machine**, for two genuine
   consent reasons (microphone, Developer-ID speech). Correct behaviour, but it
   means "self-test failed" carries no signal about *which* class of problem
   you have until you read the two lines.
2. **`jarvis/voice/native/mic_state.swift` needs `swiftc`.** On a machine
   without the toolchain the microphone reports `unknown` forever — now with a
   named reason and test coverage, but still no alternative probe.
3. Nothing else: the suite is green with zero failures, so there is no broken
   code waiting.

### D. Ideas not in the plan (unranked, not started)
Companion apps (watch/iOS), multi-user + per-device policy scoping, a real
acoustic wake engine, plugin/extension surface for third-party tools, offline
packaging for non-macOS hosts.

---

## 10. Verify every claim yourself

```bash
cd jarvis-forge
PY=./.venv/bin/python

# Scores — expect "975 passed, 1 skipped"
$PY -m pytest -q
$PY -m pytest -q --collect-only | grep "::" | sed 's/::.*//' | sort | uniq -c | sort -rn

# Counts: tools (126), risk tiers, agents (15), paths (100)
$PY -c "from jarvis.tools import build_default_registry as b; r=b(); \
print(len(r.all()),'tools'); import collections; \
print(collections.Counter(s.risk.name for s in r.all()))"
$PY -c "from jarvis.api.app import app; \
print(len({x.path for x in app.routes if getattr(x,'methods',None) and x.path.startswith('/')}),'paths')"

# Live behaviour, never touching your real ~/.jarvis-forge
JARVIS_HOME=/tmp/jarvis-check $PY run.py demo
JARVIS_HOME=/tmp/jarvis-check $PY run.py voice-demo
JARVIS_HOME=/tmp/jarvis-check JARVIS_PORT=8919 $PY run.py &
T=$(cat /tmp/jarvis-check/api_token)
for p in /health /metrics /integrations /vision/capabilities /voice/status; do
  curl -s -H "X-JARVIS-Token: $T" http://127.0.0.1:8919$p | $PY -m json.tool; done

# Microphone truth (first call compiles the helper, ~3 s; then ~0.15 s)
$PY -c "from jarvis.voice.audio import microphone_state as m; s=m(); \
print(s['state'], s['permission'], s['host'], [d['name'] for d in s['devices']])"

# App signature
codesign --verify --verbose "dist/Vantage.app"
JARVIS_SELFTEST=1 "dist/Vantage.app/Contents/MacOS/JarvisForge"
```

The dashboard at `http://127.0.0.1:<port>/dashboard` shows the same facts live:
peripherals and gateways, devices, tools, tasks, memory, audit, pending
confirmations, voice, privacy and health.
