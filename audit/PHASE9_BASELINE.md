# PHASE 9 — PHASE 1: Repository Baseline

Audited 2026-09-25. Auditor: automated agent, executing commands directly.
Every number below was measured in this session. No value was copied from `STATUS.md`.

## 1.1 Repository identity

| Item | Claim under test (brief) | Measured | Verdict |
|---|---|---|---|
| Repository path | `/Users/abhijeetmiskin/AppData/aisys/jarvis-forge` | `/Users/abhijeetmiskin/AppData/jarv/jarvis-forge` | **WRONG PATH** |
| Git repository | implied (asks for branch/HEAD/status) | **not a git repository** | **FALSE** |
| Server on `127.0.0.1:8765` | running | **not listening** | **FALSE (not running)** |

Commands and literal output:

```console
$ ls -d /Users/abhijeetmiskin/AppData/aisys/jarvis-forge
ls: /Users/abhijeetmiskin/AppData/aisys/jarvis-forge: No such file or directory

$ cd /Users/abhijeetmiskin/AppData/jarv/jarvis-forge && git rev-parse --show-toplevel
fatal: not a git repository (or any of the parent directories): .git

$ curl -s -m 3 -o /dev/null -w "health_http=%{http_code}\n" http://127.0.0.1:8765/health
health_http=000
```

The path in the brief is not a typo invented by the brief — it is a real historical
location. 306 `.pyc` files in the tree still embed `/Users/abhijeetmiskin/AppData/aisys/jarvis-forge`
as their `co_filename`, and a leftover shell process from an earlier session (PID 69242)
still carries that path in its command line. The project was **moved** to
`/Users/abhijeetmiskin/AppData/jarv/jarvis-forge` without clearing bytecode caches
and without initialising version control.

```console
$ find . -name '*.pyc' -not -path './.venv/*' -print0 | xargs -0 grep -l "aisys" | wc -l
     306
```

**Consequence for the audit:** the brief's Phase 1 items `git status`, `current branch`,
`current commit` are unmeasurable, and Phase 2 "no destructive git operations" is moot.
There is no commit to return to, so no change in this tree is reversible except by manual
backup. This is recorded as a backlog item (see final report).

## 1.2 Working tree layout

```console
$ ls -la          (repo root, directories and tracked artifacts only)
.env.example      7781  BASELINE_REPORT.md  21418  PROJECT_DETAILS.md  22732
README.md        54980  STATUS.md           42245  build_app.py         6812
install_app.sh    1335  pyproject.toml       883   requirements.txt     202
run.py            3498
app/  dashboard/  dist/  docs/  jarvis/  tests/  audit/  .venv/  __pycache__/
```

Top-level directories (excluding `.venv`, `__pycache__`, `node_modules`):

```console
$ find . -maxdepth 2 -type d -not -path '*/.git*' -not -path '*/__pycache__*' \
        -not -path '*/node_modules*' -not -path '*/.venv/*' | sort
./app ./app/build ./audit ./dashboard ./dist ./dist/Jarvis Forge.app ./dist/build
./docs ./jarvis ./jarvis/agents ./jarvis/api ./jarvis/car ./jarvis/coding ./jarvis/core
./jarvis/db ./jarvis/design ./jarvis/devices ./jarvis/gateway ./jarvis/home
./jarvis/integrations ./jarvis/memory ./jarvis/registry ./jarvis/research ./jarvis/robotics
./jarvis/scheduler ./jarvis/security ./jarvis/tools ./jarvis/transport ./jarvis/vision
./jarvis/voice ./jarvis/watch ./tests
```

23 subpackages under `jarvis/`.

## 1.3 Measured size

```console
$ find jarvis -name '*.py' -not -path '*__pycache__*' | wc -l
     126
$ find tests -name '*.py' | wc -l
      27
$ find . -name '*.py' -not -path './.venv/*' -not -path '*__pycache__*' | wc -l
     158
$ find jarvis -name '*.py' -not -path '*__pycache__*' -exec cat {} + | wc -l
   29975
$ find tests -name '*.py' -exec cat {} + | wc -l
    9395
$ cat run.py build_app.py | wc -l
     304
$ find . -name '*.py' -not -path './.venv/*' -not -path '*__pycache__*' -exec cat {} + | wc -l
   41442
```

| Metric | Measured | Brief's stated figure | Delta |
|---|---|---|---|
| `jarvis/` Python LOC | **29,975** | ~29,700 | +275 (+0.9%) — claim approximately correct |
| `tests/` Python LOC | 9,395 | not stated | — |
| root scripts LOC | 304 | not stated | — |
| **total Python LOC** | **41,442** | ~29,700 (ambiguous scope) | if the claim meant "all Python", it understates by 11,742 |
| Python files | 158 | not stated | — |

Note the coincidence that must not be confused: **126 Python files** in `jarvis/` and
**126 tools** and **126 failing tests** are three unrelated quantities that happen to be equal.

## 1.4 Python environment and dependencies

```console
$ which python3 ; python3 --version
/opt/homebrew/bin/python3
Python 3.14.7                    <-- system python, NOT the project env

$ .venv/bin/python --version
Python 3.11.16                   <-- project env, used for all measurements below

$ .venv/bin/python -m pytest --version
pytest 8.4.2
```

`pyproject.toml` declares `requires-python = ">=3.10"`, project version `0.1.0`,
runtime deps `fastapi>=0.115`, `uvicorn[standard]>=0.30`, `pydantic>=2.7`,
`pydantic-settings>=2.3`, `httpx>=0.27`; dev extras `pytest>=8.2`, `pytest-asyncio>=0.23`.

Bytecode caches in the tree were produced by **three different interpreter/pytest
combinations**: `cpython-311-pytest-8.4.2`, `cpython-313-pytest-9.1.1`, and plain
`cpython-313`. Evidence:

```console
$ find . -name '*.pyc' -not -path './.venv/*' | head
./app/__pycache__/make_icon.cpython-313.pyc
./app/__pycache__/launcher.cpython-311.pyc
./tests/__pycache__/test_privacy.cpython-313-pytest-9.1.1.pyc
./tests/__pycache__/test_api.cpython-311-pytest-8.4.2.pyc
```

So the tree has been executed under Python 3.11 and 3.13 and is now being handed to a
machine whose default `python3` is 3.14. Only 3.11 is verified working here.

`.env` is absent (`ls .env` → No such file or directory); only `.env.example` exists,
so no credential file is in the working tree.

## 1.5 Agents (measured by loading the core)

```console
$ PYTHONPATH=. JARVIS_HOME=$(mktemp -d) .venv/bin/python -c \
    "from jarvis.core.orchestrator import JarvisCore; c=JarvisCore(); \
     print(len(c.agents)); print(sorted(c.agents.keys()))"
15
['calendar', 'car', 'coding', 'design', 'email', 'home', 'laptop', 'laptop_center',
 'memory', 'phone', 'research', 'robotics', 'scheduler', 'vision', 'watch']
```

**15 agents — matches the claim exactly**, including all 15 names the brief enumerates.
`jarvis/agents/` holds 17 modules; `base.py` and `__init__.py` are infrastructure, not agents.

## 1.6 Tools (measured by building the registry)

```console
$ PYTHONPATH=. JARVIS_HOME=$(mktemp -d) .venv/bin/python -c \
    "from jarvis.tools import _all_specs, build_default_registry; \
     s=[t[0] for t in _all_specs()]; print(len(s), len(build_default_registry().all()))"
126 126
```

| Metric | Measured | Claim | Verdict |
|---|---|---|---|
| tool specs | **126** | 126 | **TRUE** |
| registry entries | **126** | — | consistent, no shadowing |
| duplicate tool names | **0** | — | clean |

Distribution by owning agent (sums to 126):

```
laptop 17, memory 13, research 13, design 11, coding 10, phone 10,
calendar 8, scheduler 8, vision 6, watch 6, email 5, home 5, robotics 5, car 5,
laptop_center 4
```

Risk tiers (declared on `ToolSpec`, enum `RiskLevel` 0..4):

| risk | 0 | 1 | 2 | 3 | 4 |
|---|---|---|---|---|---|
| tools | 68 | 33 | 11 | 11 | 3 |

`requires_confirmation=True` on **24 of 126** tools. `reversible=False` on **11**.

Cross-check of the safety invariant "high risk ⇒ confirmation": exactly one tool has
`risk>=3` without `requires_confirmation`:

```
RISK>=3_WITHOUT_CONFIRM  ['emergency_stop']
```

This is a correct exception — an emergency stop must never be gated behind a
confirmation round-trip — and is recorded as intentional, not a defect.

## 1.7 API surface (measured from the FastAPI app object)

```console
$ PYTHONPATH=. JARVIS_HOME=$(mktemp -d) .venv/bin/python -c \
    "from jarvis.api.app import app; \
     print(len(app.routes), len({r.path for r in app.routes if hasattr(r,'path')}))"
106 100
```

| Metric | Measured | Claim | Verdict |
|---|---|---|---|
| route objects | 106 | — | — |
| **unique paths** | **100** | 100 | **TRUE** |
| method-level routes | 110 | — | GET 58, POST 48, HEAD 4 |

## 1.8 Database (measured from a freshly migrated throwaway DB)

```console
$ PYTHONPATH=. JARVIS_HOME=$(mktemp -d) .venv/bin/python -c "... sqlite_master ..."
DB_PATH      $JARVIS_HOME/jarvis.db
TABLE_COUNT  32
INDEX_COUNT  5
TABLES  action_logs, calendar_events, confirmations, contacts, conversations,
        design_assets, device_pairings, devices, email_drafts, experiments, failures,
        files, graph_edges, graph_nodes, integrations, memories, memory_categories,
        messages, papers, permissions, projects, research_notes, schedule_runs,
        scheduled_jobs, sensor_readings, sessions, task_steps, tasks, tools,
        transfer_events, transfers, users
```

`confirmations`, `permissions`, `action_logs`, `failures` and `audit`-bearing tables are
present, so the schema supports the claimed permission/confirmation/recovery model.
Index count (5) is low relative to 32 tables — flagged for the performance phase, not a defect.

## 1.9 Configuration surface

`jarvis/config.py` uses `pydantic-settings` with `env_prefix="JARVIS_"` and declares
**55 settings fields**:

```
allowed_commands api_token audio_device auth_enabled briefing_sections calendar_password
calendar_url calendar_user confirm_channel confirm_timeout cors_origins design_figma_enabled
design_figma_token design_mermaid_enabled design_mermaid_path email_address email_imap_host
email_imap_port email_password email_smtp_host email_smtp_port enforce_host_header env home
host listen_seconds llm_api_key llm_base_url llm_embed_model llm_fast_model llm_model
llm_reasoning_model log_level paranoid port powerful_commands privacy_mode
rate_limit_burst rate_limit_per_minute redact_audit require_wake_word sample_rate
scheduler_enabled stt_backend stt_language stt_model trash_on_delete tts_backend
tts_enabled tts_rate tts_voice vision_model voice_dir voice_enabled wake_words
```

`.env.example` advertises **60** `JARVIS_*`-style names against those 55 fields — a 5-name
gap that Phase 13 reconciles (dead or undocumented variables).

Default data home is `~/.jarvis-forge`, overridable by `JARVIS_HOME`:

```python
# jarvis/config.py:19
return Path(os.path.expanduser(os.environ.get("JARVIS_HOME", "~/.jarvis-forge")))
```

All experiments in this audit set `JARVIS_HOME=$(mktemp -d)`; the user's real
`~/.jarvis-forge` was never written to.

## 1.10 Desktop bundle state (deferred to Phase 10)

Present and not rebuilt during the audit, per the brief's instruction:
`dist/Jarvis Forge.app`, `dist/build/`, `app/build/`, `build_app.py`, `install_app.sh`.
Full drift analysis in `PHASE9_DESKTOP_AUDIT.md`.

## 1.11 Baseline summary

| Dimension | Measured | Claimed | Status |
|---|---|---|---|
| Agents | 15 | 15 | TRUE |
| Tools | 126 | 126 | TRUE |
| API paths | 100 | 100 | TRUE |
| DB tables | 32 | not claimed | measured |
| Source LOC | 29,975 (`jarvis/`) | ~29,700 | TRUE within 1% |
| Tests passing | **875** (+126 failed, +1 skipped) | 975 passing, 1 skipped | **FALSE** |
| Version | `pyproject` says `0.1.0` | v1.8 | **CONTRADICTION** (see Phase 13) |
| Git | absent | implied present | **FALSE** |
| Server running | no | yes | **FALSE** |

The counts (agents/tools/paths/LOC) are honest and independently confirmed.
The two things the brief took on faith that are wrong are **the test result** and
**the assumption that this is a version-controlled, running system**.
