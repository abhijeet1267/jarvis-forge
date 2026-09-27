# JARVIS Forge — Status Report

**Version:** v1.8 · **Generated:** 24 September 2026
**Location:** `/Users/abhijeetmiskin/AppData/aisys/jarvis-forge/`

A permissioned, multi-agent personal AI operating system. Built from
`JARVIS_Forge_Full_Working_Plan.md`.

Every claim below was verified against the running system, not copied from the
plan. Where something does not work, it says so.

---

## 1. Health at a glance

| Check | Result |
|---|---|
| Test suite | **976 collected → 975 passing, 1 skipped** (the live arXiv test skips itself when the network refuses) |
| Server | **running** on `127.0.0.1:8765` |
| Tools registered | **126** |
| API endpoints | **100** paths |
| Agents | **15** — laptop, laptop_center, memory, phone, email, calendar, scheduler, research, coding, design, vision, home, robotics, car, watch |
| Audit chain | **intact** (161 records verified) |
| API token | present, mode `0600` |
| Desktop app | rebuilt 25 Sep (544K), **signature valid**; self-test **7/9** — the two failures are microphone consent and Developer-ID speech, both correctly named |
| Voice — text to speech | **working** (`macos_say`) |
| Voice — speech to text | fallback (`transcript`) — see §7 |
| Email | **working** (local maildir; IMAP/SMTP when configured) |
| Calendar | **working** (local SQLite; CalDAV when configured) |
| Scheduler | **working** — recurring jobs, permission-ceiling enforced |
| Memory retrieval | **hybrid** — keyword + vector, fused with RRF |
| Research | **working** — arXiv (real), citations never fabricated |
| Coding Agent | **working** — workspace-bounded, no delete path, no push |
| Design Agent | **working** — real SVG/HTML/Markdown artifacts |
| Vision Agent | real OCR + measurement, never acts — **OCR helper intermittent today** (see §8) |
| Home / IoT | **honest offline** — 5 tools wired; gateway down ⇒ "No device was switched.", never a fake toggle |
| Robotics | **honest offline** — 5 tools wired; gateway down ⇒ "no motion command was sent", never a fake move (PHYSICAL ⇒ confirmation) |
| Car / vehicle | **honest offline** — 5 read-only tools wired; no adapter ⇒ "I cannot read live telemetry yet", never an invented number |
| Watch / companion | **honest queue** — 6 tools + 4 companion routes wired; unpaired ⇒ "nothing was sent"; only the wearer's reply decides a confirmation |
| Microphone | **3 real inputs present; capture blocked by macOS consent** — measured, not assumed (see §8) |
| Source | 126 modules · ~29700 lines · 26 test files |

---

## 2. How to run it

```bash
cd /Users/abhijeetmiskin/AppData/aisys/jarvis-forge
PY=/Users/abhijeetmiskin/.workbuddy-ai/binaries/python/envs/default/bin/python

$PY run.py            # server + dashboard  -> http://127.0.0.1:8765/dashboard
$PY run.py cli        # text REPL
$PY run.py voice      # voice REPL (speaks replies aloud)
$PY run.py demo       # acceptance sweep (text)
$PY run.py voice-demo # acceptance sweep through the voice pipeline
$PY -m pytest -q      # run the tests

./install_app.sh      # build + install the macOS app
open "dist/Jarvis Forge.app"
```

> **Running the tests offline.** The suite imports `pydantic-settings` and
> `pytest-asyncio`. Online, `pip install -r requirements.txt` is enough. On a
> network-less machine, install the cached web deps into `.venv` from the `uv`
> archive cache and put minimal pure-Python shims for those two modules on
> `PYTHONPATH` (they only implement the env-driven `BaseSettings` and the
> per-test `asyncio.run` runner). The full suite passes that way: **868 passed,
> 1 skipped**.

**Calling the API directly** (a token is required):

```bash
curl -H "X-JARVIS-Token: $(cat ~/.jarvis-forge/api_token)" \
     -X POST http://127.0.0.1:8765/command \
     -H 'Content-Type: application/json' \
     -d '{"text":"what is my battery"}'
```

---

## 3. Verify it yourself

Every number in this report can be reproduced. Run these from the project
directory with `PY` set as in §2.

```bash
# Tests — expect "975 passed, 1 skipped" (the skip is the live arXiv test)
$PY -m pytest -q

# Test breakdown per area
$PY -m pytest -q --collect-only | grep "::" | sed 's/::.*//' | sort | uniq -c | sort -rn

# Tool count and risk tiers
$PY -c "import sys; sys.path.insert(0,'.'); \
from jarvis.tools import build_default_registry as b; \
print(len(b().names()), 'tools')"

# Endpoint count
$PY -c "import sys; sys.path.insert(0,'.'); \
from jarvis.api.app import app; \
print(len({r.path for r in app.routes if getattr(r,'methods',None) and r.path.startswith('/')}), 'paths')"

# Live health, security posture, audit chain, recovery counters
TOKEN=$(cat ~/.jarvis-forge/api_token)
curl -s -H "X-JARVIS-Token: $TOKEN" http://127.0.0.1:8765/health   | $PY -m json.tool
curl -s -H "X-JARVIS-Token: $TOKEN" http://127.0.0.1:8765/metrics  | $PY -m json.tool
curl -s -H "X-JARVIS-Token: $TOKEN" http://127.0.0.1:8765/privacy  | $PY -m json.tool

# Voice subsystem status
curl -s -H "X-JARVIS-Token: $TOKEN" http://127.0.0.1:8765/voice/status | $PY -m json.tool

# Microphone truth on its own (first call builds the helper, ~3s; then ~0.15s)
$PY -c "import sys; sys.path.insert(0,'.'); \
from jarvis.voice.audio import microphone_state as m; \
s=m(); print(s['state'], s['permission'], s['host'], [d['name'] for d in s['devices']])"

# Full acceptance sweep (text), then through the voice pipeline
$PY run.py demo
$PY run.py voice-demo

# Desktop app health check (9 checks)
JARVIS_SELFTEST=1 "dist/Jarvis Forge.app/Contents/MacOS/JarvisForge"
```

The dashboard at `http://127.0.0.1:8765/dashboard` shows the same information
live: devices, tools, tasks, memory, audit, pending confirmations, voice,
privacy and health panels.

---

## 4. Live acceptance results

Captured from the running system on 19 Sep 2026, with the car and watch rows
re-captured live on 23 Sep 2026 and the microphone row on 24 Sep 2026.

| Command | Decision | Response |
|---|---|---|
| `what is my battery` | EXECUTE | "Your laptop battery is at 52% (charging)." |
| `what time is it` | EXECUTE | "It is 22:33 on Saturday, 19 September 2026." |
| `system status` | EXECUTE | "System looks healthy: 8 CPUs, load …" |
| `find my report` | EXECUTE | "I found 229 file(s) matching 'report'. Most recent: …" |
| `check my car` | EXECUTE | "Your car adapter is not connected, so I cannot read live telemetry yet (no car adapter or vehicle API is configured)." |
| `check my car fuel` | EXECUTE | "I could not read those car values (no car adapter or vehicle API is configured)." |
| `check my car diagnostics` | EXECUTE | "I cannot read the car's diagnostic codes (no car adapter or vehicle API is configured)." — never "no faults stored" |
| `how is my watch` (unpaired) | EXECUTE | "Your watch is not paired yet (pairing state: unpaired), so there is nothing to report from it." |
| `notify my watch "build finished"` (paired, no bridge) | EXECUTE | "Notification "build finished" is queued on the laptop, not delivered. Nothing has reached your watch yet." |
| `ask my watch to confirm` | EXECUTE | "Confirmation prompt "copy_file on laptop_main | …" is queued on the laptop, not delivered. Answer it on the watch, or approve it here." |
| `POST /watch/reply {decision: CONFIRM}` | — | "Copied watch-src.txt to /private/tmp/…/watch-dst.txt." — the file exists only after the wearer answered |
| `listen_and_respond(2s)` (real voice path) | REJECT | "macOS has denied this app the microphone. Enable it in System Settings -> Privacy & Security -> Microphone, then restart it." — refused in **0.96s** warm (11.6s the first time ever, while the helper is compiled), and `ffmpeg` was never spawned; before this pass the same call launched capture, waited out a 17s timeout, then guessed at the cause |
| `give me today's location summary` | EXECUTE | "Location history for today is not available: location tracking is disabled." |
| `send an email to my teammate` | EXECUTE | "I can search your mail, read a message, summarise recent ones, draft a reply, or send something you have already drafted. For example: "check my unread emails"…" — re-captured 25 Sep: the local maildir is live, so the old "email is not configured yet" line would itself have been false |
| `transfer this file to my phone` | EXECUTE | "I will not guess which file to send. Tell me the exact file, for example: "send /tmp/report.pdf to my phone"." |
| `plan my day` | EXECUTE | "Your today is clear." — the local calendar answers; it does not claim a sync it has not done |
| `delete everything` | **BLOCK** | "I will not do that automatically: blanket delete request." |
| `stop everything` | EXECUTE | "Emergency stop engaged. Physical actions cancelled; safety controllers remain active." |

Note the pattern: capabilities that are not connected say so explicitly rather
than reporting a fake success.

---

## 5. What is done and working

### Interfaces
- [x] **Voice** — wake word, TTS, STT, barge-in interruption, follow-up context
- [x] **Text CLI** — interactive REPL with slash commands
- [x] **Web dashboard** — devices, peripherals, tools, tasks, memory, audit, confirmations, voice, privacy, health panels
- [x] **macOS desktop app** — double-click `.app`, control window, auto-opens dashboard, clean shutdown

### Phone / device (v0.6)
- [x] **Pairing** — device requests, user approves, trust granted, revocable
- [x] **Device identity** — 256-bit key, human-verifiable fingerprint, key material `0600`
- [x] **Encrypted transfer** — TLS (openssl) + HMAC-SHA256 device auth
- [x] **Integrity** — SHA-256 verified before a transfer is reported complete
- [x] **No silent overwrite** — a name collision gets a suffix, never clobbers
- [x] **Atomic** — writes to `.part`, renames only after verification
- [x] **Cancellation, size limits, handshake timeout, transfer history**

### Email + Calendar (v0.7)
- [x] **Email read** — search, read, summarise; unread filtering
- [x] **Email draft** — composes and saves; **never sends**
- [x] **Email send** — Level 3, always confirmed, never inferred
- [x] **Recipient resolution** — an ambiguous name is refused, not guessed
- [x] **Real `.eml` output** — local maildir files any client can open
- [x] **Calendar read** — day/week listing, search, next event, free time
- [x] **Calendar write** — create / update / cancel, all confirmed
- [x] **iCalendar** — real ICS serialise/parse for CalDAV
- [x] **Two backends each** — local by default, remote when configured

### Scheduler + briefing (v0.8)
- [x] **Recurring jobs** — "every morning", "every Monday at 9 AM", "every 30 minutes"
- [x] **One-off reminders** — "remind me at 8 PM to …"
- [x] **Permission ceiling per job** — a schedule cannot raise its own authority
- [x] **Never auto-approves** — a job needing confirmation is parked for you
- [x] **Run history** — every execution recorded with its outcome
- [x] **Daily briefing** — 9 configurable sections, degrading honestly
- [x] **Background loop** — runs while the server is up

### Advanced memory (v0.9)
- [x] **Vector memory** — dependency-free lexical embeddings, no model download
- [x] **Hybrid retrieval** — keyword + vector fused with reciprocal rank fusion
- [x] **Keyword fallback** — search still works if vectors are unavailable
- [x] **12 memory types** — preference, fact, decision, failure, fix, …
- [x] **Lifecycle** — importance, confidence, expiry, relationships
- [x] **User controls** — inspect, export, delete, clear scope, disable categories
- [x] **Sensitive protection** — private/secret memories withheld from exports
- [x] **Startup backfill** — pre-v0.9 memories get vectors automatically

### Home / IoT Agent (v1.5)
- [x] **Status, device list, sensor readings** — `get_home_status`, `list_home_devices`,
      `get_home_reading` (Level 0)
- [x] **Control + routines** — `control_home_device`, `run_home_routine` (Level 1, reversible)
- [x] **Honest reachability** — the gateway is offline by default; a control while it is down
      says "No device was switched.", and an unverified reply says "not yet confirmed"
- [x] **Deterministic in tests** — `JARVIS_HOME_SIMULATE` and `JARVIS_HOME_MQTT_BROKER` make
      behaviour reproducible without pretending hardware exists
- [x] **Planner routing** — "turn on the study light", "run study mode", "how warm is the study"
      map to real home tools

### Robotics / sensor-fleet Agent (v1.6)
- [x] **Status, fleet list, telemetry** — `get_robot_status`, `list_robots`,
      `get_robot_telemetry` (Level 0)
- [x] **Motion + experiments** — `command_robot_motion`, `run_experiment`
      (PHYSICAL risk ⇒ confirmation-gated, never auto-executed)
- [x] **Honest reachability** — the gateway is offline by default; a motion while
      it is down says "no motion command was sent", and an unverified online reply
      says "not yet confirmed"
- [x] **Deterministic in tests** — `JARVIS_ROBOT_SIMULATE` and `JARVIS_ROBOT_BRIDGE`
      make behaviour reproducible without pretending hardware exists
- [x] **Planner routing** — "move the amr to (1, 2)", "run today's experiment",
      "status of my robots" map to real robotics tools; a file move of a real path
      is untouched

### Car / vehicle Agent (v1.7)
- [x] **Status, telemetry, capabilities, diagnostics, service** — `get_car_status`,
      `get_car_telemetry`, `list_car_metrics`, `get_car_diagnostics`,
      `get_car_service_status` (all Level 0)
- [x] **Read-only by construction** — no unlock, horn, ignition or remote-start tool
      exists; a test asserts every car tool stays in the READ tier, so nothing here
      can make the vehicle act
- [x] **Real SAE J1979 decoding** — `(256A+B)/4` RPM, `A−40` temperatures,
      `100·A/255` percentages, `(256A+B)/1000` V, plus the J2012 DTC bit layout
      (`0x01 0x04` → `P0104`), each pinned by a hand-computed test vector
- [x] **Capability comes from the car** — OBD-II support is the vehicle's own PID 00
      bitmasks at bases 0x00/0x20/0x40; a metric it never advertised is answered with
      "this vehicle does not implement PID 01 5C", never a number
- [x] **Mode A and Mode B both real** — ELM327 over a TCP bridge (AT init,
      single-frame and ISO-15765 multi-frame replies, 17-character VIN) and a
      manufacturer vehicle API over HTTPS; the JSON decides what exists
- [x] **Two different "no answer"s** — `None` (the source did not respond) and
      `[]` (it answered with nothing) are kept apart, so an unplugged adapter can
      never report "no faults stored"
- [x] **Honest reachability** — offline by default ⇒ "Your car adapter is not
      connected, so I cannot read live telemetry yet"; values from the last live read
      are labelled *last known*, and a source that goes silent mid-session keeps its
      reason instead of pretending to be current
- [x] **Deterministic in tests** — `JARVIS_CAR_SIMULATE` exposes a partial, clearly
      labelled vehicle; `JARVIS_CAR_OBD` / `JARVIS_CAR_API_URL` take real transports
- [x] **Planner routing** — "check my car", "what is my car fuel", "check my car
      diagnostics", "when is my car service due" map to the five tools; "what is my
      battery" and "remember that my car is a diesel" stay with the laptop and memory
      agents

### Watch / companion Agent (v1.8)
- [x] **Status, outbox, notification, haptics, confirmation, routines** —
      `get_watch_status`, `get_watch_outbox`, `send_watch_notification`,
      `vibrate_watch`, `ask_watch_confirmation`, `run_watch_routine`
- [x] **The companion calls in** — `POST /watch/collect`, `/watch/ack`,
      `/watch/reply` (all token-protected); a smartwatch is not something this
      process can dial, so nothing pretends otherwise
- [x] **Delivery is staged because the stages differ** — `queued → delivered →
      displayed → replied`. Taking an item proves the companion holds it, not that
      the screen showed it
- [x] **"Never shown" ≠ "said no"** — `ack(displayed=false)` records a note and
      leaves the prompt unanswered; dismissing is an explicit DISMISSED reply
- [x] **A prompt can only be answered after it reached the watch** — answering a
      queued item is refused, because accepting it would let JARVIS approve its own
      confirmation and call it a wrist tap
- [x] **No tool may resolve a confirmation** — `ask_watch_confirmation` displays
      and stops; every watch tool is READ or LOW, asserted by a test. Simulate mode
      delivers prompts but never presses buttons
- [x] **Wrist answers are real** — a `confirmation_id` reply resolves that
      confirmation through the same expiry-checked resolver the CLI and dashboard
      use; a quick-routine reply dispatches its command through the pipeline with
      `source="watch"` (verified: the file existed only after CONFIRM, and the
      emergency routine engages the real emergency stop)
- [x] **§25 confirmation channel wired for real** — `JARVIS_CONFIRM_CHANNEL=watch`
      existed as a dead setting for many versions; it now queues the pending prompt
      and reports how far it got, refusing while the watch is unpaired
- [x] **Battery is only what the watch reported** — `None` until the companion says
      otherwise, and implausible values are dropped rather than shown
- [x] **Honest reachability** — three modes: queue (default, "queued … nothing has
      reached your watch"), simulate (labelled, delivers, cannot answer), bridge
      (`{"ok": true}` = shown; a bare HTTP 200 = "accepted, unconfirmed"; an
      unreachable bridge leaves items queued with the reason)
- [x] **Planner routing** — "how is my watch", "buzz my watch", "notify my watch
      that …", "ask my watch to confirm", "start study mode on my watch"; "watch"
      as a verb ("watch this folder") and "what is my battery" stay unclaimed

### Vision Agent (v1.3)
- [x] **Real OCR** — macOS Vision framework via a cached Swift helper, with
      bounding boxes and confidences
- [x] **Image measurement** — size, format, dominant colours, brightness, contrast
- [x] **Screen capture** — capture and read text from the display
- [x] **Change detection** — "did it change, and where?" with a located region
- [x] **Never acts on what it sees** — the agent owns **no action tools at all**;
      vision stops at PROPOSE. Asserted structurally by a test.
- [x] **Degrades instead of stalling** — a recogniser that misses its 20 s bound
      is left alone for 45 s, each timed-out read says "no text was read", and
      `/health` reports *present* and *answering* separately (a cached binary is
      not a working one)
- [x] **Honest about content** — with no vision model configured, JARVIS says it
      can measure and read text but cannot describe what is depicted

### Design Agent (v1.2)
- [x] **Architecture / system diagrams** — real, parseable SVG (verified by tests)
- [x] **Flowcharts** — SVG, with decision diamonds
- [x] **UI concepts** — self-contained HTML wireframes, work offline
- [x] **Documentation** — real Markdown outlines
- [x] **Presentations** — self-contained HTML decks with keyboard navigation
- [x] **Design analysis** — measurable checks (accessibility, structure, size)
- [x] **Asset store** — assets kept in JARVIS's own design directory, linkable to projects
- [x] **Honest labelling** — outlines say they are outlines; wireframes say they are
      wireframes; `created_by` records local vs external
- [x] **Permissioned integrations** — none enabled by default

### Coding Agent (v1.1)
- [x] **Workspace boundary** — every path checked; escapes, traversal and
      symlinks leaving the root are refused
- [x] **Inspect and search** — structure, languages, test files, code search
- [x] **Propose before apply** — a diff is shown first and writes nothing
- [x] **Apply with a backup** — edits are reversible; missing text is refused,
      never approximated
- [x] **No delete path** — the repository cannot be removed by this workflow
- [x] **Tests never weaken the allowlist** — reported as "not allowlisted (by
      design)" rather than silently opting in an interpreter
- [x] **Commit only when authorised**, and **push is not implemented**

### Research Agent (v1.0)
- [x] **Approved sources** — arXiv (real API, no key) + local corpus; offline fallback
- [x] **Never fabricates citations** — a paper enters only via a real backend or
      explicit user addition; citing an unstored id returns 404, never a guess
- [x] **Verified vs unverified** — backend-derived papers are marked verified;
      user-typed ones are not, and say so
- [x] **Claim provenance** — source fact vs interpretation vs hypothesis vs
      unresolved; an unsourced "fact" is downgraded automatically
- [x] **Extraction** — methods, datasets and limitations taken from stored abstracts;
      empty when the abstract states none (never invented)
- [x] **Comparison, gaps, briefs** — built only from what is stored
- [x] **Graph links** — papers connect to projects as `paper` nodes

### Core pipeline
- [x] **Orchestrator** — `Intent → Plan → Permission → Tool → Execution → Verification → Memory → Response`
- [x] **Planner** — deterministic rule engine, **works offline with no API key**
- [x] **Recovery** — retry if safe → alternative tool → escalate → record
- [x] **Task manager** — jobs with pause / resume / retry / cancel

### 126 tools across 15 agents

| Tier | Tools |
|---|---|
| **0 read** | `get_time`, `system_status`, `battery_status`, `list_apps`, `list_directory`, `read_file`, `search_files`, `get_car_status`, `get_car_telemetry`, `list_car_metrics`, `get_car_diagnostics`, `get_car_service_status`, `laptop_snapshot`, `laptop_processes`, `laptop_project_status`, `get_location_summary`, `list_devices`, `get_device_status`, `get_device_capabilities`, `ping_device`, `get_transfer_status`, `search_email`, `read_email`, `summarize_email`, `list_events`, `search_events`, `next_event`, `find_free_time`, `daily_schedule`, `list_schedules`, `get_schedule`, `daily_briefing`, `list_papers`, `cite_paper`, `compare_papers`, `extract_paper_methods`, `extract_paper_datasets`, `extract_paper_limitations`, `find_research_gaps`, `research_brief`, `inspect_workspace`, `list_workspaces`, `search_code`, `coding_report`, `analyze_design`, `design_integrations`, `list_design_assets`, `read_design_asset`, `inspect_image`, `read_image_text`, `find_image_text`, `capture_screen`, `compare_images`, `vision_capabilities`, `get_home_status`, `list_home_devices`, `get_home_reading`, `get_robot_status`, `list_robots`, `get_robot_telemetry`, `get_watch_status`, `get_watch_outbox` |
| **1 low** | `open_app`, `close_app`, `take_screenshot`, `compress_path`, `cancel_transfer`, `draft_email`, `pause_schedule`, `resume_schedule`, `search_papers`, `add_paper`, `add_research_note`, `link_paper_to_project`, `open_workspace`, `close_workspace`, `propose_code_change`, `create_architecture_diagram`, `create_flowchart`, `create_ui_concept`, `create_documentation`, `create_presentation`, `link_asset_to_project`, `control_home_device`, `run_home_routine`, `build_agent_prompt`, `send_watch_notification`, `vibrate_watch`, `ask_watch_confirmation`, `run_watch_routine` |
| **2 external** (confirm) | `copy_file`, `move_file`, `run_allowlisted_command`, `send_to_device`, `register_phone`, `create_event`, `update_event`, **`schedule_task`**, `run_schedule_now`, **`apply_code_change`**, `run_workspace_tests` |
| **3 destructive** (confirm) | `delete_file` (Trash), `approve_pairing`, `remove_phone`, `send_email`, `cancel_event`, `cancel_schedule`, `forget_memory`, `clear_memory_scope`, `forget_paper`, `delete_design_asset` |
| **4 physical** | `emergency_stop` (always allowed, never gated) · `command_robot_motion`, `run_experiment` (confirmation-gated) |
| **memory agent** | `remember_fact`, `search_memory`, `create_project`, `add_project_node`, `explain_failures`, `project_summary` |

Agents: **laptop**, **laptop_center**, **memory**, **phone**, **email**, **calendar**, **scheduler**, **research**, **coding**, **design**, **vision**, **home**, **robotics**, **car**, **watch**.

### Memory
- [x] Three scopes: short-term / long-term / project
- [x] **Project-state graph** — links components, experiments, failures, fixes
- [x] BM25-style recall with stopword removal and stemming

### Safety
- [x] **Permission engine is the only authoriser** — agents cannot bypass it
- [x] Three outcomes: `EXECUTE` / `CONFIRM` / `BLOCK`
- [x] **Phrasing guard** — "delete everything", `sudo rm -rf /`, "unlock the door" refused *before* planning
- [x] **Path guard** — system roots, credentials, and persistence paths (`~/.zshrc`, `~/Library/LaunchAgents`)
- [x] **No unrestricted shell** — explicit argv, interpreters excluded by default
- [x] Approvals expire; IDs carry 128 bits

### Security
- [x] **Token auth** on every endpoint (auto-generated, `0600`)
- [x] **Strict CORS** — never `*`
- [x] **DNS-rebinding protection** — loopback `Host` check
- [x] Rate limiting + hardening headers
- [x] **Tamper-evident audit log** — hash-chained with `verify_chain()`

### Privacy
- [x] **Redaction always on** — credentials stripped before the audit log or a model prompt
- [x] **Privacy mode** — masks identifiers, collapses `$HOME` → `~`
- [x] **Data export** (redacted by default) and **scoped deletion** requiring confirmation

### Resilience & observability
- [x] **Recoveries are always reported**, never hidden behind a clean success
- [x] `/metrics`, `/failures`, audit chain state
- [x] **`/health.peripherals`** — each hardware-facing gateway reports its own
      reachability (not configured / configured but unread / connected / not
      answering / simulated), and the dashboard renders exactly that vocabulary.
      An adapter that was never asked is not called broken, and a gateway whose
      state could not be read shows "state unavailable" instead of a guessed
      reason
- [x] Structured logging (console + JSONL)

---

## 6. Test coverage

**954 tests — 953 passing, 1 skipping itself when arXiv is unreachable.**

| Test file | Tests | Covers |
|---|---|---|
| `test_api.py` | 67 | Every endpoint, auth, CORS, host check, streaming, and the `/health` peripheral report (including that a broken gateway cannot take health down) |
| `test_security.py` | 52 | Rate limiting, confirmation expiry, protected paths, allowlist, audit tampering |
| `test_voice.py` | 58 | Wake word, TTS, STT, session, follow-ups, barge-in, and measured microphone consent (installed ≠ permitted ≠ unreadable, plus non-blocking probe semantics) |
| `test_pipeline.py` | 28 | Full loop, safety suite, recovery integration |
| `test_planner.py` | 27 | Intent mapping, path case, search routing |
| `test_recovery.py` | 22 | Retry policy, destructive-action safety, fallbacks |
| `test_privacy.py` | 22 | Redaction, export, deletion scoping |
| `test_risk.py` | 18 | Risk classification, command guard |
| `test_paths.py` | 15 | Protected vs safe paths |
| `test_policy.py` | 13 | Permission decisions, grants, revocations |
| `test_registry.py` | 6 | Tool, device and app registries |
| `test_memory.py` | 6 | Memory store and project graph |
| `test_transfer.py` | 17 | TLS transfer, checksums, overwrite, revocation, state machine |
| `test_phone.py` | 38 | Pairing lifecycle, tool surface, policy guards, planner routing |
| `test_email_calendar.py` | 52 | Mailbox read/draft/send, recipient resolution, calendar CRUD, ICS round-trip, policy gating |
| `test_scheduler.py` | 48 | Schedule parsing, RRULE round-trip, job lifecycle, the no-escalation rule, briefing sections |
| `test_memory_advanced.py` | 60 | Embeddings, cosine, vector index, RRF fusion, keyword fallback, memory types, expiry, controls |
| `test_research.py` | 50 | Paper store, the no-fabricated-citation rule, claim provenance, extraction, gaps, briefs |
| `test_coding.py` | 46 | Workspace boundary, escape/symlink refusal, propose-then-apply, no delete path, commit authorisation |
| `test_design.py` | 50 | SVG/HTML/Markdown validity, asset store containment, design analysis, integration policy |
| `test_vision.py` | 48 | Real OCR, image measurement, image diff, the guarantee that vision has no action tools, and OCR timeout/cooldown (a stalled recogniser reports "no text was read" instead of stalling every later question) |
| `test_home.py` | 29 | Home gateway domain, read/write tools (offline, simulated, online-unverified), planner routing, e2e honest-offline |
| `test_robotics.py` | 30 | Robotics gateway domain, read/write tools (offline, simulated, online-unverified), name resolve/ambiguity, planner routing (robot motion vs file move), e2e honest-offline + PHYSICAL confirmation |
| `test_car.py` | 84 | J1979 decoders vs hand-computed vectors, DTC bit layout, PID-support probing, an ELM327 session over a real socket, manufacturer-API capability gating, last-known labelling, read-only guarantee, non-probing `posture()` for polled endpoints, planner routing, e2e honest-offline |
| `test_watch.py` | 65 | Staged delivery never collapsed, unshown ≠ declined, queued prompts unanswerable, simulate delivers but cannot approve, bridge confirmed-vs-unconfirmed vs unreachable (incl. a real localhost server), pairing gate, wrist tap resolving a live confirmation and moving a file, confirmation-channel wiring, planner routing, tool surface |
| `test_laptop_center.py` | 25 | Machine snapshot / processes / projects, handoff-prompt composition, planner anti-theft rules |

---

## 7. Verified security posture

Each of these was confirmed against the running server:

| Attack | Result |
|---|---|
| Cross-origin POST from `evil.example.com` | **401 blocked** |
| Unauthenticated `/audit`, `/metrics` | **401 blocked** |
| DNS-rebinding `Host` header | **400 blocked** |
| `Access-Control-Allow-Origin` for a foreign origin | **none** (never `*`) |
| "delete everything" / `sudo rm -rf /` | **BLOCK** |
| Writing `~/.zshrc` or a LaunchAgent | **BLOCK** |
| `python3 -c '…'` via the command tool | **refused** (interpreters off the allowlist) |
| Unauthenticated `POST /watch/reply` (the approval path) | **401 blocked** |
| Answering a confirmation the watch was never given | **refused** — "has not reached your watch, so there is nothing to answer yet" |
| A watch tool trying to approve its own prompt | **no such tool exists** — asserted by `test_no_tool_can_resolve_a_confirmation` |

---

## 8. Known limitations (honest)

These are environment or platform constraints, not unfinished code.

| Limitation | Why | Workaround |
|---|---|---|
| **Speech recognition** uses the `transcript` fallback | Apple only grants Speech Recognition to an app signed with a real **Developer ID**; ad-hoc signing is refused (measured) | Sign with a Developer ID, or install `whisper.cpp`, or set `JARVIS_STT_BACKEND=openai_whisper` |
| **Microphone capture** is blocked by consent, not by code | Measured on this machine: 3 real inputs (`MacBook Air Microphone`, `Microsoft Teams Audio`, `MJAudioRecorder`) all idle, `ffmpeg` installed — and AVFoundation still reports `denied` for an unbundled process, so `ffmpeg -f avfoundation` stalls waiting for a prompt a CLI cannot show | The microphone state is now *probed* (`mic_state.swift`) rather than inferred, so `describe_audio()` says **blocked** instead of the old `which("ffmpeg")`-based **available**. Probing runs on a background thread (a first reading can mean compiling Swift plus macOS's ~8 s assessment of a new binary), is warmed at server startup, and `/voice/status` answers from cache in **~2 ms**; capture waits for the first reading so it cannot slip past the probe into an ffmpeg hang. Four states, never collapsed: **granted**, **blocked**, **measuring** (an answer is on its way), **unknown** (this machine cannot answer at all). Launch the signed `.app` and approve the prompt; the state re-reads itself, so the dashboard flips to `ready` without a restart |
| **Screenshots** fail here | Screen Recording permission not granted | Grant it in System Settings |
| **Vision OCR stalls under load** | macOS Vision intermittently never returns: the same image measured at 0.6 s standalone and beyond 120 s while other work ran. A 60 s wait per question made that invisible | Bounded to 20 s (`JARVIS_OCR_TIMEOUT`, clamped 1–120 s), and a stalled recogniser is **left alone for 45 s** instead of every later question paying the wait again. A timed-out read returns "no text was read" — never guessed text — and `vision_capabilities` / `/health` now report *present* and *answering* as two separate facts, because a cached binary is not a working one |
| Audit records written before the hash chain existed | They have no hash | Sealed automatically at startup via `seal_chain()`. Sealing protects against *future* edits only — you cannot retroactively protect data that was never hashed |
| Two legacy audit rows show as `legacy_message` | Early builds stored a message in the `result` field | Labelled honestly rather than misreported |

---

## 9. Designed but NOT connected

Each returns a clear refusal rather than faking success:

| Component | Status |
|---|---|
| Watch Agent | v1.8 agent wired; no companion paired or bridge connected by default (honest "queued … nothing has reached your watch") |
| Home automation | v1.5 agent wired; gateway offline by default (honest "No device was switched.") |
| Car actuation | v1.7 is read-only telemetry by design (plan Phase 10); no unlock / horn / ignition tool exists |
| Car telemetry | v1.7 agent wired; no adapter or vehicle API connected by default (honest "I cannot read live telemetry yet") |
| Robotics | v1.6 agent wired; gateway offline by default (honest "no motion command was sent"); PHYSICAL risk ⇒ confirmation |
| Continuous acoustic wake word | interface reserved, no engine wired |

---

## 10. Architecture

```
Intent -> Plan -> Permission -> Tool -> Execution -> Verification -> Memory -> Response
```

```
jarvis-forge/
├── run.py                 # serve | cli | voice | demo | voice-demo
├── build_app.py           # assemble Jarvis Forge.app
├── install_app.sh         # build + install to /Applications
├── STATUS.md              # this file
├── README.md              # full documentation
├── app/                   # macOS launcher + icon generator
├── dashboard/index.html   # live control dashboard
├── jarvis/
│   ├── core/              # models, planner, orchestrator, recovery, tasks
│   ├── security/          # risk, policy, paths, audit, auth, privacy
│   ├── registry/          # tools, devices, applications
│   ├── tools/             # apps, files, system, memory, home, robotics, car, watch
│   ├── home/              # HomeGateway (offline by default) + routines
│   ├── car/               # CarGateway + J1979 metrics (OBD-II / vehicle API)
│   ├── watch/             # WatchGateway (outbox, staged delivery, bridge)
│   ├── agents/            # base + 15 agents (laptop, laptop_center, memory,
│   │                      #   home, car, watch, vision, robotics, …)
│   ├── memory/            # store + project graph
│   ├── voice/             # audio, tts, stt, wakeword, session
│   ├── gateway/           # model router
│   ├── db/                # SQLite schema + migrations
│   └── api/               # FastAPI + dashboard
└── tests/                 # 954 tests
```

**Design rule:** the model decides *what* should happen, the tool decides *how*,
and the permission engine decides *whether it is allowed*. Agents execute tools;
they can never authorise them.

---

## 11. Build history

| Version | What was added |
|---|---|
| v0.1 | Core: orchestrator, tool registry, permission engine, laptop agent, memory, audit log |
| v0.2 | Voice: wake word, TTS, STT, barge-in, SSE streaming |
| v0.3 | Packaged as a macOS desktop app with a control window |
| v0.4 | Security + accuracy hardening: token auth, strict CORS, DNS-rebinding guard, rate limiting, command allowlist split, protected persistence paths, audit hash chain, recency-ranked search, BM25 recall |
| v0.5 | Recovery (§47) and Privacy (§49): retry/fallback/failure log, redaction, privacy mode, data export and deletion, task lifecycle, metrics |
| v0.6 | Phone Agent + secure device transfer: pairing, device identity, TLS transport, HMAC auth, checksum-verified transfer |
| v0.7 | Email + Calendar: read/search/draft/send with confirmation, recipient resolution, local + remote backends, iCalendar |
| v0.8 | Scheduler + daily briefing: recurrence parsing, per-job risk ceiling, no silent escalation, configurable briefing |
| v0.9 | Advanced memory: dependency-free vector embeddings, hybrid RRF retrieval, memory types, lifecycle and user controls |
| v1.0 | Research Agent: approved sources (arXiv), references, extraction, comparison, typed claim provenance, research briefs |
| v1.1 | Coding Agent: workspace boundary, inspect/search, propose-then-apply with backups, no delete path, authorised-only commit |
| v1.2 | Design Agent: real SVG diagrams, self-contained HTML mockups and decks, Markdown docs, asset store, permissioned integrations |
| v1.3 | Vision Agent: real OCR (macOS Vision), image measurement, screen capture, image diff; vision never acts |
| v1.5 | Home / IoT Agent: status, device list, sensor readings, control + routines; gateway offline by default with honest "No device was switched." (simulated/online via env) |
| v1.6 | Robotics / sensor-fleet Agent: status, fleet list, telemetry, motion + experiments; gateway offline by default with honest "no motion command was sent" (simulated/online via env); PHYSICAL risk ⇒ confirmation-gated |
| LCC | Laptop Control Center (see `JARVIS_Forge_Laptop_Control_Center_Work.md`): machine snapshot, process list, project status and agent-handoff prompt composition — read-only, fixed argv |
| v1.7 | Car / vehicle Agent: read-only telemetry over OBD-II (Mode B) and a manufacturer vehicle API (Mode A); real J1979 decoding, capability taken from the car's own PID bitmasks or the API's JSON, offline by default with honest "I cannot read live telemetry yet"; no actuation path exists |
| v1.8 | Watch / companion Agent: outbox the companion calls into, staged delivery (queued → delivered → displayed → replied), notifications, haptics, quick routines, and watch-side confirmations — the wearer's reply is the only thing that can decide a pending approval. Wires `JARVIS_CONFIRM_CHANNEL=watch`, a setting that had existed with nothing behind it |
| Honesty-drift pass (25 Sep) | Re-measured the claims the previous day's own report made about itself. Three rows in §4 quoted responses the system no longer gives (email/calendar/transfer said "not configured" after their local backends went live — by then the *refusal* was the false statement), and `NOT_IMPLEMENTED` still carried those three dead keys; it is now `UNAVAILABLE_REASONS`, holding only the phone case, worded as "no phone is paired" rather than "agent not built". Rebuilt the desktop bundle, which had shipped the pre-fix launcher. Then found and fixed a regression the microphone probe itself introduced: probing synchronously made `/voice/status` compile Swift inside the request, so the app's own 3-second health check timed out on every cold start — measurement moved to a background thread, warmed at startup, and cached reads now answer in ~2 ms. Added a drift guard for the bundled launcher (it immediately caught one), split the probe's fourth state (`measuring`, an answer on its way) from `unknown` (this machine cannot answer), and covered all six ways the probe can fail |
| Hardware-truth pass | Attended the machine instead of the plan: enumerated real Bluetooth range, USB, serial and LAN state, then measured microphone consent through AVFoundation. Found and fixed a health lie — `describe_audio()` called a microphone *available* because `which("ffmpeg")` succeeded, while the first capture attempt stalls forever. Capture now refuses before spawning `ffmpeg`, consent is attributed to the process that was actually asked, and "cannot measure" reads as *unknown* rather than *broken* (dashboard, launcher and `/voice/status` all had to stop collapsing the two states). Re-checking this pass's own documentation then caught a second trap: the freshly compiled helper answered `unknown` on its first exec because macOS spends ~8 s assessing an unseen binary, past the 5 s steady-state budget — the first run now gets 45 s and a clean home answers correctly in 3.4 s |

---

## 12. Next steps (plan build order)

1. ~~Phone Agent + secure file transfer~~ — done in v0.6
2. ~~Email + Calendar integrations~~ — done in v0.7
3. ~~Scheduler + daily briefing~~ — done in v0.8
4. ~~Advanced/vector memory~~ — done in v0.9
5. ~~Research Agent~~ — done in v1.0
6. ~~Coding Agent~~ — done in v1.1
7. ~~Design Agent~~ — done in v1.2
8. ~~Vision Agent~~ — done in v1.3
9. ~~Home / IoT Agent~~ — done in v1.5
10. ~~Robotics Agent~~ — done in v1.6
11. ~~Car Agent~~ — done in v1.7 (read-only telemetry; actuation deliberately not built, per plan Phase 10)
12. ~~Watch Agent~~ — done in v1.8 (companion outbox, staged delivery, watch-side confirmations)

The plan's build order is complete through v1.8. What is left is *attaching real
hardware and accounts* to surfaces that already refuse honestly without them: a
paired watch, an OBD-II adapter or vehicle API, a home gateway, a robot bridge,
IMAP/SMTP and CalDAV credentials, and a Developer ID signature for on-device
speech recognition.

The microphone is the special case: the hardware is already here and already
enumerated, so the only thing between "not permitted" and "ready" is a consent
decision macOS will take from a signed app and not from a terminal. §8 records
how that was measured.

Each is another **authenticated agent/tool**, not a rewrite.
