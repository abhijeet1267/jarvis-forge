# Jarvis Forge — Comprehensive Project Documentation

> **Version:** 0.2.0 | **Last Updated:** 2026-10-15 | **Python:** >=3.10

## Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Project Structure](#project-structure)
4. [API Reference](#api-reference)
5. [Module Reference](#module-reference)
6. [Test Suite](#test-suite)
7. [Dashboards](#dashboards)
8. [iOS App](#ios-app)
9. [Working Features](#working-features)
10. [Known Issues & Not Working](#known-issues--not-working)
11. [Next Steps](#next-steps)

---

## Overview

Jarvis Forge is a **permissioned, multi-agent personal AI operating system** — a local FastAPI server that orchestrates LLM-powered agents across devices (laptop, phone, watch, car) with strict security boundaries. Every tool invocation goes through a policy engine; gated actions require confirmation from an authorized surface (dashboard/laptop), not the device that requested them.

**Key principles:**

- **Permission-first:** all tool access is mediated by `security/policy.py` — no bypass
- **Surface-aware:** laptop, phone, watch, car each have distinct authority levels
- **Multi-provider AI:** supports OpenAI, Anthropic, Google, Groq, Ollama, DeepSeek, OpenRouter, Cerebras, xAI, SambaNova, Kiro, Mistral, Together, Replicate, Scrapybara
- **Local-first:** runs on your machine, stores data in SQLite, uses local keychain for credentials

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    FastAPI Server (app.py)                │
│  /command, /ai/chat, /phone, /watch, /car, /dashboard   │
├──────────────────────────────────────────────────────────┤
│  jarvis/core/orchestrator.py (JarvisCore)                │
│  ┌─────────┬──────────┬──────────┬──────────────────┐   │
│  │ Policy  │ Planner  │ Recovery │ Briefing         │   │
│  │ Engine  │          │          │                   │   │
│  └─────────┴──────────┴──────────┴──────────────────┘   │
├──────────────────────────────────────────────────────────┤
│  jarvis/agents/ — Specialized agents                     │
│  phone, watch, car, home, laptop, coding, design,        │
│  research, vision, robotics, email, calendar, memory     │
├──────────────────────────────────────────────────────────┤
│  jarvis/gateway/ — AI Provider routing                   │
│  OpenAI, Anthropic, Google, Groq, Ollama, DeepSeek, …    │
├──────────────────────────────────────────────────────────┤
│  jarvis/db/database.py — SQLite (ai_messages,            │
│  ai_chats, ai_attachments, jobs, confirmations, …)       │
├──────────────────────────────────────────────────────────┤
│  Dashboard HTML files — phone.html, watch.html,          │
│  mission_control.html, ai_connections.html, …            │
└──────────────────────────────────────────────────────────┘
```

### Request Flow

1. Request arrives at `app.py` route → authenticated via `X-JARVIS-Token` or session cookie
2. Route dispatches to `JarvisCore` → intents parsed by `agent/intent.py` + `agent/plan.py`
3. Policy engine evaluates every tool invocation against `security/policy.py`
4. Gated actions queue a confirmation → resolved via `/confirm/{id}`, `/watch/reply`, or voice
5. Tool executes → result returned through the agent → response to caller
6. Everything logged to audit trail (`security/audit.py`)

---

## Project Structure

```
jarvis-forge/
├── pyproject.toml              # Package metadata, dependencies, pytest config
├── jarvis/                     # Main Python package
│   ├── cli.py                  # Entry point: `jarvis` CLI command
│   ├── agent/                  # Intent parsing, plan generation, tool resolution
│   │   ├── executor.py         # Executes planned actions
│   │   ├── intent.py           # Intent classification from NL
│   │   ├── orchestrator.py     # Agent-level orchestration
│   │   ├── plan.py             # Plan generation from intents
│   │   └── tools.py            # Tool definitions for agents
│   ├── agents/                 # Domain-specific agents (16 total)
│   │   ├── base.py             # Abstract base agent
│   │   ├── phone.py            # Phone surface agent
│   │   ├── watch.py            # Watch surface agent
│   │   ├── car.py              # Car surface agent
│   │   ├── home.py             # Home automation agent
│   │   ├── laptop.py           # Laptop control agent
│   │   ├── laptop_center.py    # Laptop center (mission control)
│   │   ├── coding_agent.py     # Code generation & editing
│   │   ├── design_agent.py     # Design & creative work
│   │   ├── research_agent.py   # Web research
│   │   ├── vision_agent.py     # Computer vision / screenshot analysis
│   │   ├── robotics.py         # Robotics control
│   │   ├── calendar_agent.py   # Calendar integration
│   │   ├── email_agent.py      # Email integration
│   │   ├── memory_agent.py     # Memory/persistence agent
│   │   └── scheduler_agent.py  # Scheduled task agent
│   ├── api/                    # FastAPI application
│   │   └── app.py              # ~4140 lines, 140+ routes
│   ├── car/                    # Car integration (gateway, metrics, readers)
│   ├── coding/                 # Code workspace management
│   │   ├── changes.py          # Diff/change tracking
│   │   ├── repo.py             # Git repository operations
│   │   ├── report.py           # Code analysis reports
│   │   ├── validation.py       # Code validation
│   │   └── workspace.py        # Isolated workspace management
│   ├── core/                   # Core orchestration
│   │   ├── orchestrator.py     # JarvisCore — main orchestrator
│   │   ├── planner.py          # Task planner
│   │   ├── recovery.py         # Error recovery system
│   │   ├── briefing.py         # Session briefings
│   │   ├── models.py           # Pydantic models
│   │   └── tasks.py            # Task data structures
│   ├── db/                     # Database layer
│   │   └── database.py         # SQLite schema, migrations, CRUD (~800 lines)
│   ├── design/                 # Design tools
│   │   ├── assets.py           # Asset management
│   │   ├── deck.py             # Slide deck generation
│   │   ├── diagrams.py         # Diagram generation
│   │   ├── docs.py             # Document generation
│   │   ├── integrations.py     # Design tool integrations
│   │   ├── review.py           # Design review
│   │   └── ui.py               # UI generation
│   ├── devices/                # Device management
│   │   └── pairing.py          # Device pairing protocol
│   ├── gateway/                # AI provider gateway
│   │   ├── providers.py        # Provider definitions (OpenAI, Anthropic, etc.)
│   │   ├── router.py           # Request routing to providers
│   │   └── selection.py        # Provider selection logic
│   ├── home/                   # Home automation gateway
│   ├── integrations/           # External service integrations
│   │   ├── calendar_provider.py
│   │   └── email_provider.py
│   ├── memory/                 # Memory & knowledge graph
│   │   ├── embeddings.py       # Vector embeddings
│   │   ├── graph.py            # Knowledge graph
│   │   ├── hybrid.py           # Hybrid search
│   │   ├── store.py            # Memory store
│   │   └── vector_store.py     # Vector database
│   ├── permissions/            # Permission management service
│   ├── registry/               # Tool, device, app registries
│   │   ├── apps.py             # Application registry
│   │   ├── devices.py          # Device registry
│   │   └── tools.py            # Tool registry
│   ├── research/               # Research tools
│   │   ├── brief.py            # Research briefs
│   │   ├── notes.py            # Research notes
│   │   ├── papers.py           # Paper analysis
│   │   └── sources.py          # Source management
│   ├── robotics/               # Robotics gateway
│   ├── runtime/                # Runtime components
│   │   ├── menubar.py          # macOS menu bar app
│   │   └── supervisor.py       # Runtime supervisor
│   ├── scheduler/              # Task scheduler
│   │   ├── runner.py           # Job runner
│   │   ├── schedule.py         # Schedule management
│   │   └── store.py            # Schedule persistence
│   ├── security/               # Security layer
│   │   ├── audit.py            # Audit logging
│   │   ├── auth.py             # Authentication
│   │   ├── paths.py            # Path security
│   │   ├── policy.py           # Permission policy engine
│   │   ├── privacy.py          # Privacy controls
│   │   ├── providers.py        # Provider security
│   │   └── risk.py             # Risk assessment
│   └── tools/                  # Tool implementations (open_app, copy_file, etc.)
│       ├── agents.py, apps.py, base, browser.py, calendar.py, car_tools.py,
│       ├── coding.py, commands.py, contacts.py, design.py, email_tools.py,
│       ├── files.py, home_tools.py, laptop.py, mac_tools.py, music.py,
│       ├── notes.py, phone.py, reminders.py, robotics.py, system.py,
│       ├── web.py, windows.py
├── tests/                      # 117 test files, ~2,150 tests
├── dashboard/                  # HTML dashboards (13 files)
│   ├── mission_control.html    # Unified control panel
│   ├── phone.html              # Phone PWA dashboard
│   ├── ai_connections.html     # AI provider management
│   ├── car.html                # Car interface
│   ├── index.html              # Main dashboard
│   ├── dashboard.html          # Classic dashboard
│   └── ...                     # css/, js/, icons/
├── ios/                        # iOS companion app
│   └── VantageAI/
│       ├── Sources/            # Swift source files
│       ├── VantageAI.xcodeproj/
│       └── README.md
└── dist/                       # Built distributions
```

---

## API Reference

### Core Command Routes

| Method   | Path              | Description                       |
| -------- | ----------------- | --------------------------------- |
| GET      | `/`               | API root, version info            |
| GET      | `/health`         | Health check                      |
| GET/POST | `/auth/token`     | Token-based auth                  |
| POST     | `/auth/session`   | Create session                    |
| DELETE   | `/auth/session`   | End session                       |
| GET      | `/auth/sessions`  | List active sessions              |
| POST     | `/command`        | Execute a command (main dispatch) |
| POST     | `/confirm/{id}`   | Resolve a confirmation            |
| GET      | `/confirmations`  | List pending confirmations        |
| GET      | `/command/stream` | SSE command stream                |

### AI Provider Routes

| Method | Path                             | Description                     |
| ------ | -------------------------------- | ------------------------------- |
| GET    | `/ai/providers`                  | List all AI providers           |
| GET    | `/ai/providers/{id}`             | Get provider details            |
| POST   | `/ai/providers/{id}/test`        | Test provider connection        |
| POST   | `/ai/providers/{id}/store`       | Store provider credentials      |
| DELETE | `/ai/providers/{id}/credentials` | Remove credentials              |
| GET    | `/ai/selection`                  | Get provider selection rules    |
| POST   | `/ai/selection`                  | Set provider selection rules    |
| GET    | `/ai/capabilities`               | List AI capabilities            |
| POST   | `/ai/verify-all`                 | Verify all configured providers |
| GET    | `/ai/connection-health`          | Check provider health status    |

### AI Chat Routes

| Method | Path                            | Description                 |
| ------ | ------------------------------- | --------------------------- |
| POST   | `/ai/chats`                     | Create chat                 |
| GET    | `/ai/chats/{id}`                | Get chat details            |
| DELETE | `/ai/chats/{id}`                | Delete chat                 |
| POST   | `/ai/chats/{id}/messages`       | Add message to chat         |
| GET    | `/ai/chats/{id}/messages`       | Get chat messages           |
| POST   | `/ai/chat/send`                 | Send message & get response |
| GET    | `/ai/chat`                      | Chat UI (dashboard)         |
| POST   | `/ai/attachments/upload`        | Upload file attachment      |
| GET    | `/ai/attachments/{id}`          | Get attachment              |
| DELETE | `/ai/attachments/{id}`          | Delete attachment           |
| GET    | `/ai/attachments/messages/{id}` | Get message attachments     |
| POST   | `/ai/process-attachments`       | Process attachments with AI |

### Device Routes

| Method | Path                         | Description            |
| ------ | ---------------------------- | ---------------------- |
| GET    | `/devices`                   | List paired devices    |
| POST   | `/devices/pair/request`      | Request device pairing |
| GET    | `/devices/pair/pending`      | List pending pairings  |
| POST   | `/devices/pair/{id}/approve` | Approve pairing        |
| POST   | `/devices/pair/{id}/deny`    | Deny pairing           |
| POST   | `/devices/{id}/revoke`       | Revoke device access   |
| POST   | `/devices/{id}/endpoint`     | Update device endpoint |

### Voice Routes

| Method | Path                | Description           |
| ------ | ------------------- | --------------------- |
| GET    | `/voice/status`     | Voice session status  |
| POST   | `/voice/command`    | Execute voice command |
| POST   | `/voice/speak`      | TTS speak             |
| POST   | `/voice/transcribe` | STT transcribe        |
| POST   | `/voice/interrupt`  | Interrupt speaking    |
| POST   | `/voice/reset`      | Reset voice session   |

### Watch Routes

| Method | Path             | Description                 |
| ------ | ---------------- | --------------------------- |
| GET    | `/watch/status`  | Watch gateway status        |
| POST   | `/watch/collect` | Collect watch items         |
| POST   | `/watch/ack`     | Acknowledge watch items     |
| POST   | `/watch/reply`   | Reply to watch confirmation |

### Phone Routes

| Method | Path             | Description            |
| ------ | ---------------- | ---------------------- |
| GET    | `/phone`         | Phone PWA dashboard    |
| POST   | `/phone/command` | Phone command dispatch |

### Laptop Routes

| Method | Path                   | Description       |
| ------ | ---------------------- | ----------------- |
| GET    | `/laptop/snapshot`     | System snapshot   |
| GET    | `/laptop/processes`    | Running processes |
| GET    | `/laptop/projects`     | Active projects   |
| POST   | `/laptop/agent-prompt` | Send agent prompt |

### Vision Routes

| Method | Path                   | Description          |
| ------ | ---------------------- | -------------------- |
| GET    | `/vision/capabilities` | Vision capabilities  |
| POST   | `/vision/inspect`      | Inspect image        |
| POST   | `/vision/read`         | Read text from image |
| POST   | `/vision/find`         | Find in image        |
| POST   | `/vision/screen`       | Screenshot analysis  |
| POST   | `/vision/compare`      | Compare images       |

### Design Routes

| Method | Path                         | Description         |
| ------ | ---------------------------- | ------------------- |
| POST   | `/design/assets`             | Upload design asset |
| GET    | `/design/assets`             | List design assets  |
| POST   | `/design/assets/{id}/delete` | Delete design asset |

### Dashboard Routes

| Method | Path               | Description              |
| ------ | ------------------ | ------------------------ |
| GET    | `/dashboard`       | Main dashboard           |
| GET    | `/ai-connections`  | AI connections dashboard |
| GET    | `/mission-control` | Mission control panel    |

### Other Routes

| Method   | Path                   | Description                        |
| -------- | ---------------------- | ---------------------------------- |
| POST     | `/emergency_stop`      | Emergency stop all actions         |
| GET      | `/tools`               | List available tools               |
| GET      | `/audit`               | Audit log                          |
| GET/POST | `/memory`              | Memory operations                  |
| GET/POST | `/tasks`               | Task management                    |
| POST     | `/tasks/{id}/{action}` | Task actions (pause/resume/cancel) |
| GET      | `/metrics`             | System metrics                     |
| GET      | `/failures`            | Failure log                        |
| GET/POST | `/privacy`             | Privacy controls                   |
| GET/POST | `/permissions`         | Permission management              |
| GET/POST | `/workflows`           | Workflow execution                 |

---

## Module Reference

### jarvis/core/orchestrator.py — JarvisCore

The central orchestrator. Responsibilities:

- Command dispatch: receives parsed intents, resolves tools
- Confirmation lifecycle: create, resolve, expire confirmations
- Session management: voice sessions, API sessions
- Provider routing: delegates AI requests to gateway
- Security integration: every tool call passes through `policy.evaluate()`

### jarvis/security/policy.py — Policy Engine

The permission system:

- `evaluate(action)` — assess whether an action is allowed, gated, or denied
- `resolve(confirmation_id, decision)` — resolve a gated action (approve/deny)
- Self-approval prevention: non-dashboard surfaces cannot approve their own gated actions
- Surface classification: `NARROW_SURFACES` (phone), `dashboard` (laptop browser)
- Confirmation expiry: configurable timeout via `settings.confirm_timeout`

### jarvis/gateway/providers.py — AI Providers

Supports 15+ providers:

- **OpenAI** (GPT-4o, GPT-4.1, o3, o4-mini)
- **Anthropic** (Claude Opus 4.5, Sonnet 4.5, Haiku 4.5)
- **Google** (Gemini 2.5 Pro, Flash)
- **Groq** (Llama 4, Qwen, DeepSeek)
- **DeepSeek** (V3, R1)
- **Ollama** (local models)
- **OpenRouter**, **Cerebras**, **xAI**, **SambaNova**, **Kiro**, **Mistral**, **Together**, **Replicate**, **Scrapybara**

Provider management: health checks, credential storage (macOS Keychain), capability discovery, model listing.

### jarvis/db/database.py — Database

SQLite database with tables:

- `ai_providers` — provider configurations and credentials
- `ai_chats` — chat sessions
- `ai_messages` — chat messages (FK → ai_chats)
- `ai_attachments` — file attachments (FK → ai_messages)
- `jobs` — scheduled/queued tasks
- `confirmations` — pending action confirmations
- `audit_log` — security audit trail
- `memory` — persistent memory store
- `devices` — paired devices
- `pairings` — in-progress device pairings
- `permissions` — tool permission overrides
- `sessions` — active API sessions
- `metrics` — system metrics

### jarvis/agents/ — Agent Implementations

Each agent implements a specific surface or domain:

- **phone.py** — Phone PWA agent, handles `/phone/command` dispatch
- **watch.py** — Watch agent, queue-based interaction model
- **car.py** — Car agent with metrics and reader integration
- **home.py** — Home automation agent
- **laptop.py** — Laptop control (screenshots, processes, projects)
- **coding_agent.py** — Code workspace operations
- **design_agent.py** — Design tool operations
- **research_agent.py** — Web research capabilities
- **vision_agent.py** — Image/screenshot analysis
- **memory_agent.py** — Persistent memory queries
- **calendar_agent.py** — Calendar read/write
- **email_agent.py** — Email operations
- **robotics.py** — Robotics control
- **scheduler_agent.py** — Scheduled task management

---

## Test Suite

**2,150 tests across 117 files.** Tests use pytest with async support (`pytest-asyncio`, `asyncio_mode = "auto"`).

### Test File Categories

#### Phase Tests (Phase 10–97)

These are numbered integration tests that were added as features were built:

| Phase | File                                                           | Tests | Focus                     |
| ----- | -------------------------------------------------------------- | ----- | ------------------------- |
| 10    | `test_phase10_confirmation_revalidation.py`                    | 11    | Confirmation revalidation |
| 10    | `test_phase10_emergency_stop.py`                               | 8     | Emergency stop            |
| 10    | `test_phase10_memory_deletion.py`                              | 17    | Memory deletion           |
| 10    | `test_phase10_provider_status.py`                              | 22    | Provider health status    |
| 10    | `test_phase10_revocation.py`                                   | 9     | Permission revocation     |
| 10    | `test_phase10_token_disclosure.py`                             | 19    | Token security            |
| 10    | `test_phase10_verification_authority.py`                       | 8     | Verification authority    |
| 10    | `test_phase10_watch_authority.py`                              | 17    | Watch authority           |
| 11    | `test_phase11_write_file.py`                                   | 11    | File writing              |
| 12    | `test_phase12_command_path.py`                                 | 14    | Command routing           |
| 13    | `test_phase13_dashboard_truth.py`                              | 13    | Dashboard accuracy        |
| 14    | `test_phase14_workflows.py`                                    | 19    | Workflow engine           |
| 15    | `test_phase15_acceptance.py`                                   | 14    | Acceptance criteria       |
| 16    | `test_phase16_accountability.py`                               | 11    | Audit accountability      |
| 17    | `test_phase17_response_coherence.py`                           | 8     | Response quality          |
| 18    | `test_phase18_simulation_truth.py`                             | 10    | Simulation accuracy       |
| 19    | `test_phase19_refusal_transparency.py`                         | 17    | Refusal clarity           |
| 20    | `test_phase20_dated_recall.py`                                 | 11    | Date-based recall         |
| 21    | `test_phase21_speed.py`                                        | 13    | Performance               |
| 22    | `test_phase22_safety_matrix.py`                                | 17    | Safety matrix             |
| 23    | `test_phase23_deletion_completeness.py`                        | 10    | Deletion completeness     |
| 24    | `test_phase24_refusal_vs_clarification.py`                     | 10    | Refusal vs clarification  |
| 25    | `test_phase25_desktop_truth.py`                                | 18    | Desktop accuracy          |
| 26    | `test_phase26_recovery_trust.py`                               | 30    | Recovery system           |
| 27    | `test_phase27_job_retry.py`                                    | 16    | Job retry logic           |
| 28    | `test_phase28_reading_is_not_writing.py`                       | 18    | Read vs write separation  |
| 29    | `test_phase29_routes_fill_their_own_action.py`                 | 13    | Route action filling      |
| 30    | `test_phase30_capabilities_are_reachable.py`                   | 33    | Capability reachability   |
| 31    | `test_phase31_the_device_you_name_is_the_device_you_switch.py` | 18    | Device switching          |
| 33    | `test_phase33_questions_that_carry_their_own_answer.py`        | 17    | Self-contained queries    |
| 34    | `test_phase34_one_sentence_one_deadline.py`                    | 11    | Deadline parsing          |
| 35    | `test_phase35_reach_the_laptop.py`                             | 30    | Laptop reachability       |
| 36    | `test_phase36_two_applications.py`                             | 30    | Multi-app scenarios       |
| 38    | `test_phase38_scan_to_connect.py`                              | 6     | QR scan to connect        |
| 39    | `test_phase39_the_folder_you_named.py`                         | 17    | Named folder ops          |
| 40    | `test_phase40_the_copy_still_launches.py`                      | 6     | Copy launch               |
| 41    | `test_phase41_the_sentences_you_actually_say.py`               | 18    | Natural language          |
| 42    | `test_phase42_the_model_has_a_job.py`                          | 7     | Model job awareness       |
| 43    | `test_phase43_a_passthrough_is_not_a_mic.py`                   | 6     | Audio passthrough         |
| 44    | `test_phase44_answers_not_command_lists.py`                    | 13    | Answer quality            |
| 45    | `test_phase45_a_key_outside_the_bundle.py`                     | 10    | External keys             |
| 46    | `test_phase46_buttons_not_typing.py`                           | 23    | Button vs typing          |
| 47    | `test_phase47_the_mic_was_never_broken.py`                     | 12    | Mic reliability           |
| 48    | `test_phase48_a_name_that_stays_the_same.py`                   | 19    | Name consistency          |
| 50    | `test_phase50_who_answered.py`                                 | 19    | Answer attribution        |
| 51    | `test_phase51_two_routes_answered_wrong.py`                    | 21    | Route correctness         |
| 52    | `test_phase52_section_six_on_the_laptop.py`                    | 27    | Laptop section 6          |
| 53    | `test_phase53_vantage_talks_about_itself.py`                   | 30    | Self-description          |
| 54    | `test_phase54_the_two_safety_doors.py`                         | 25    | Safety doors              |
| 55    | `test_phase55_a_search_that_searches.py`                       | 19    | Search quality            |
| 56    | `test_phase56_asking_on_purpose.py`                            | 28    | Intentional queries       |
| 57    | `test_phase57_the_machine_stays_reachable.py`                  | 48    | Reachability              |
| 58    | `test_phase58_the_phone_is_not_the_laptop.py`                  | 24    | Phone vs laptop           |
| 59    | `test_phase59_the_relay_is_a_door_not_a_second_laptop.py`      | 27    | Relay security            |
| 60    | `test_phase60_a_second_vendor_without_a_second_authority.py`   | 23    | Multi-vendor              |
| 61    | `test_phase61_desktop_control_that_asks_before_it_touches.py`  | 20    | Desktop confirmations     |
| 63    | `test_phase63_the_panel_says_who_answered.py`                  | 8     | Panel attribution         |
| 64    | `test_phase64_the_api_shape_is_not_for_passersby.py`           | 12    | API shape security        |
| 66    | `test_phase66_the_ledger_has_two_blind_spots.py`               | 11    | Ledger audit              |
| 68    | `test_phase68_a_job_cannot_outlive_its_process.py`             | 9     | Job lifecycle             |
| 69    | `test_phase69_the_intent_harness_can_be_made_red.py`           | 24    | Intent harness            |
| 69    | `test_phase69_a_request_to_erase_data_is_not_a_disk_report.py` | 7     | Erase vs report           |
| 69    | `test_phase69_run_py_does_not_ignore_a_port_it_was_given.py`   | 8     | Port handling             |
| 70    | `test_phase70_a_typo_is_not_a_missing_capability.py`           | 13    | Typo handling             |
| 71    | `test_phase71_phone_authority_hardening.py`                    | 8     | Phone authority           |
| 72    | `test_phase72_ai_provider_security.py`                         | 13    | Provider security         |
| 72    | `test_phase72_security_simple.py`                              | 5     | Simple security           |
| 74    | `test_phase74_attachments.py`                                  | 12    | File attachments          |
| 79    | `test_phase79_integration.py`                                  | 13    | Integration tests         |
| 86    | `test_phase86_runtime_authority.py`                            | 27    | Runtime authority         |
| 87    | `test_phase87_menu_bar.py`                                     | 6     | Menu bar                  |
| 89    | `test_phase89_packaged_acceptance.py`                          | 2     | Packaged acceptance       |
| 91    | `test_phase91_voice_agent_integration.py`                      | 28    | Voice agent               |
| 92    | `test_phase92_browser_automation.py`                           | 36    | Browser automation        |
| 93    | `test_phase93_desktop_control.py`                              | 8     | Desktop control           |
| 93    | `test_phase93_precheck.py`                                     | 2     | Precheck                  |
| 94    | `test_phase94_wizard.py`                                       | 20    | Setup wizard              |
| 95    | `test_phase95_release_integrity.py`                            | 17    | Release integrity         |
| 96    | `test_phase96_release_preparation.py`                          | 13    | Release prep              |
| 97    | `test_phase97_phone_mac_workflow.py`                           | 17    | Phone-Mac workflow        |

#### Domain Tests

| File                            | Tests | Focus                    |
| ------------------------------- | ----- | ------------------------ |
| `test_api.py`                   | 51    | Core API endpoints       |
| `test_voice.py`                 | 46    | Voice session lifecycle  |
| `test_watch.py`                 | 55    | Watch gateway            |
| `test_car.py`                   | 52    | Car integration          |
| `test_phone.py`                 | 28    | Phone PWA                |
| `test_home.py`                  | 29    | Home automation          |
| `test_coding.py`                | 40    | Coding agent             |
| `test_design.py`                | 42    | Design agent             |
| `test_research.py`              | 47    | Research agent           |
| `test_vision.py`                | 39    | Vision agent             |
| `test_email_calendar.py`        | 38    | Email & calendar         |
| `test_robotics.py`              | 30    | Robotics                 |
| `test_memory.py`                | 6     | Memory basics            |
| `test_memory_advanced.py`       | 45    | Advanced memory          |
| `test_scheduler.py`             | 33    | Task scheduler           |
| `test_security.py`              | 31    | Security framework       |
| `test_privacy.py`               | 18    | Privacy controls         |
| `test_pipeline.py`              | 24    | Processing pipeline      |
| `test_planner.py`               | 11    | Task planner             |
| `test_transfer.py`              | 17    | Data transfer            |
| `test_laptop_center.py`         | 18    | Laptop center            |
| `test_registry.py`              | 6     | Registry                 |
| `test_risk.py`                  | 6     | Risk assessment          |
| `test_paths.py`                 | 6     | Path security            |
| `test_ai_chat_provider_path.py` | 9     | AI chat provider routing |
| `test_provider_architecture.py` | 4     | Provider architecture    |
| `test_final_audit_*.py`         | 7     | Final audit checks       |

### Running Tests

```bash
# Install dev dependencies
uv pip install -e ".[dev]"

# Run all tests
pytest tests/ -x -q

# Run specific phase
pytest tests/test_phase92_browser_automation.py -x -v

# Skip slow (network) tests
pytest tests/ -m "not slow"

# Run with coverage
pytest tests/ --cov=jarvis --cov-report=html
```

---

## Dashboards

Located in `dashboard/`:

| File                   | Description                                                      |
| ---------------------- | ---------------------------------------------------------------- |
| `mission_control.html` | Unified control panel — AI providers, chat, Mac actions, console |
| `ai_connections.html`  | AI provider management — add/edit/test/remove providers          |
| `phone.html`           | Phone PWA — mobile-first command interface                       |
| `watch.html`           | Watch simulator — queue-based interaction                        |
| `car.html`             | Car dashboard interface                                          |
| `index.html`           | Main dashboard entry point                                       |
| `dashboard.html`       | Classic dashboard view                                           |
| `css/dashboard.css`    | Shared dashboard styles                                          |
| `js/dashboard.js`      | Shared dashboard JavaScript                                      |
| `icons/`               | PWA icons (192x192, 512x512)                                     |

---

## iOS App

Located in `ios/VantageAI/`:

| Path                                    | Description                   |
| --------------------------------------- | ----------------------------- |
| `Sources/VantageAIApp.swift`            | Main app entry point          |
| `Sources/ContentView.swift`             | Root content view             |
| `Sources/VantageAISettingsView.swift`   | Settings view                 |
| `Sources/VantageAIWebView.swift`        | WebView wrapper for dashboard |
| `Sources/VantageAIVoiceInputView.swift` | Voice input interface         |
| `Sources/Assets.xcassets/`              | App icons and assets          |
| `VantageAI.xcodeproj/`                  | Xcode project                 |
| `README.md`                             | iOS build & run instructions  |

---

## Working Features

- **AI provider management** — add, store (Keychain), test, and route to 15+ LLM providers
- **AI chat** — multi-turn conversations with attachments, provider selection, message persistence
- **Command dispatch** — NL command parsing → intent → plan → tool execution
- **Permission system** — policy engine with allow/gate/deny/confirm workflow
- **Device pairing** — pair phone, watch, car with cryptographic verification
- **Voice sessions** — STT → intent → TTS pipeline with wake word detection
- **Watch gateway** — queue-based interaction, confirmations, routines
- **Laptop control** — screenshots, process listing, project detection, agent prompts
- **Coding agent** — workspace creation, file editing, git operations
- **Design agent** — asset management, slide decks, diagrams, docs
- **Research agent** — web search, note-taking, paper analysis
- **Vision agent** — screenshot analysis, OCR, image comparison
- **Memory system** — vector embeddings, knowledge graph, hybrid search
- **Task scheduler** — scheduled and recurring jobs
- **Privacy controls** — data export, preview, deletion
- **Emergency stop** — kill all running actions immediately
- **Audit trail** — every action logged with timestamp, surface, and result
- **Mission Control dashboard** — unified web UI for all features
- **Phone PWA** — mobile-optimized progressive web app
- **iOS companion app** — native Swift wrapper around dashboards

---

## Known Issues & Not Working

### Test Suite Issues

The test suite currently cannot run due to a module resolution issue:

```
ImportError while loading conftest: No module named 'jarvis'
```

**Fix needed:** configure `uv run pytest` or ensure the `jarvis` package is on `PYTHONPATH`. Running `uv pip install -e .` installs the package but `uv run pytest` still fails to find it.

### Previously Diagnosed Issues (Fixed)

These issues were diagnosed and fixed in the codebase:

1. **test_phase74_attachments.py** — Fixed: client `base_url` changed to `127.0.0.1:8765`, files tuple shape corrected, FK violations resolved with `_ensure_message()` helper, app route switched from `Body(AttachmentRequest)` to `Form(...)` params.

2. **test_phase92_browser_automation.py** — Fixed: added `_browser_test_server` fixture in conftest.py that starts `simple_test_server.py` on port 8765.

3. **test_voice.py** — Fixed: tests refactored to match Phase 71 design (voice sessions cannot self-approve gated actions).

4. **test_watch.py** — Fixed: `test_a_wrist_confirm_runs_the_action` now passes through the orchestrator correctly.

5. **test_phase71_phone_authority_hardening.py** — Fixed: 8-hour sleep replaced with 2-second sleep using `confirm_timeout=1`.

6. **jarvis/core/orchestrator.py** — Fixed: self-approval guard scoped to `NARROW_SURFACES` with empty-session check.

7. **jarvis/agent/handlers.py** — Fixed: duplicate `DEFAULT_HANDLERS` removed, only the dict-extracting lambdas remain.

### Browser Automation Tests

These tests need a running browser test server. The `_browser_test_server` fixture in `conftest.py` now starts this automatically, but all 36 tests in `test_phase92_browser_automation.py` still need Playwright browsers installed (`playwright install chromium`).

### Attachment Tests

The attachment route (`/ai/attachments/upload`) requires valid session context. Tests were fixed to use proper 127.0.0.1 base URL and correct multipart form encoding.

---

## Next Steps

### Immediate (Bug Fixes)

1. **Fix test suite module resolution** — configure PYTHONPATH or uv workspace so `pytest` can find the `jarvis` module
2. **Install Playwright browsers** — `playwright install chromium` to unblock browser automation tests
3. **Run full suite and triage remaining failures** — after the above fixes, run `pytest tests/ -q --tb=line` and catalog remaining issues

### Short-term (Polish)

4. **Clean up skipped tests** — remove or fix `@pytest.mark.skip` decorators
5. **Add test coverage reporting** — integrate `pytest-cov` and set minimum coverage thresholds
6. **Document provider API differences** — each AI provider has unique quirks; document which features work with which providers

### Medium-term (Features)

7. **Improve error messages** — many routes return generic 500 errors; add structured error responses
8. **Add rate limiting** — protect AI provider routes from abuse
9. **Streaming chat responses** — implement SSE for real-time AI chat output
10. **Multi-user support** — currently single-user; add user accounts and per-user permissions

### Long-term (Architecture)

11. **Database migrations framework** — replace manual schema changes with Alembic
12. **Plugin system** — allow third-party agent and tool plugins
13. **Distributed deployment** — support running agents on separate machines
14. **WebSocket real-time sync** — replace polling with WebSocket push for all dashboards
