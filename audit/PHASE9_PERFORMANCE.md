# PHASE 9 — PHASE 12: Performance / Reliability Smoke Test

Measurement only. **Nothing was optimised.** All figures are from this machine
(macOS 25.6.0, arm64), project venv CPython 3.11.16, server bound to `127.0.0.1`.
The audited code is a clean copy with the P0 import defect repaired locally, so handlers
execute instead of raising — measured timings therefore describe the *intended* system, not
the broken one. Wall-clock for the broken tree is given for contrast in 12.5.

## 12.1 Server startup

| measurement | value | method |
|---|---|---|
| import + lifespan + bind, fresh `JARVIS_HOME`, in-process uvicorn | **321.8 ms** / **732.6 ms** (2 runs) | `perf_counter` from process start to `server.started` |
| listener up, warm interpreter, fresh home | **46.2 ms** | thread-started uvicorn on port 8767 |
| tool registry construction | **0.8 / 0.8 / 0.8 / 0.8 / 1.1 ms** (5 runs) | `build_default_registry()` timed |
| SQLite schema migration | **19 log lines** per boot, all inside the startup window | `jarvis.db: schema migrated` ×19 then `database ready` |
| mic consent helper | built **~4 s after** listen began | `20:41:00 INFO jarvis.voice.audio: mic consent helper built` |
| scheduler | starts during lifespan | `jarvis.scheduler.runner: scheduler started` |
| clean shutdown | `SIGTERM` handled; process gone, port released | `lsof` returns empty after kill |

Startup is fast; the only slow part of "usable" is the microphone probe, deliberately
backgrounded. 19 sequential `schema migrated` log lines for 32 tables suggests a
per-table migration call rather than one batched migration — harmless at this size, worth
noting for boot-time growth. **P4.**

## 12.2 Endpoint latency (n=15, warm, median/p95)

| endpoint | min | median | p95 | max |
|---|---|---|---|---|
| `GET /health` | 2.0 ms | **2.8 ms** | 4.3 ms | 4.5 ms |
| `GET /voice/status` | 1.2 ms | 2.2 ms | 6.0 ms | 7.3 ms |
| `GET /tools` (126 specs) | 0.9 ms | 1.0 ms | 1.8 ms | 2.7 ms |
| `GET /devices` | 0.5 ms | 0.7 ms | 0.8 ms | 0.8 ms |
| `GET /dashboard` (HTML) | 0.9 ms | 1.2 ms | 1.7 ms | 2.4 ms |
| `GET /audit` | 0.7 ms | 1.0 ms | 2.3 ms | 2.8 ms |

First request after a cold bind, fresh home: **64.7 ms**, then 1.2–1.9 ms steady state
across 8 samples. Nothing pathological.

`/health` composes security + privacy + device + pairing + peripheral posture on every call
and still returns in under 3 ms. Repeated health calls were flat (no drift across 150
samples; see 12.4).

## 12.3 Pipeline latency, in-process

| utterance | ms | note |
|---|---|---|
| `"what time is it"` | 5.9 | full Intent→Plan→Permission→Tool→Response |
| `"remember that my car is a Hyundai"` | 2.8 | memory write path |
| `"what is my battery"` | 19.9 | spawns `pmset`/sysctl style probes |
| `"find my report"` | **303.3** | filesystem search dominates |

Over HTTP (`POST /command`, n=8): "what time is it" median **5.0 ms** (p95 6.6), "what is my
battery" median **29.0 ms** (p95 35.4, max 84.4). Memory retrieval through the pipeline
median **4.5 ms** (p95 5.4).

Planner is rule-based here (`llm_enabled=false`), so these are local-only figures; with an
LLM backend configured they are not comparable.

**Outlier: `vision_capabilities` took 2226.4 ms** — it probes OCR and image backends on
call. `capture_screen` 275.3 ms, `take_screenshot` 71.1 ms (both failing on permissions).
A 2.2 s capability probe behind a dashboard tile is the single slowest measured local
operation; recorded, not fixed. **P3.**

**Outlier: 2 MB `POST /command` = 4071 ms** for a payload the planner answered "I understood
the words but not the goal yet". Unbounded body size is a CPU-spending vector
(see `PHASE9_API_SECURITY.md` API-1). **P2.**

## 12.4 Repeated-call and rate-limit behaviour

150 sequential `GET /health` as fast as the client could issue them:

```json
{ "200": 118, "429": 32 }   // 0.2 s elapsed
```

Consistent with configured `rate_limit_burst = 120`, `rate_limit_per_minute = 600`. The
limiter engages cleanly, returns a parseable body
(`{"detail":"Too many requests. Slow down."}`), and recovers. No latency inflation before
the knee: all 118 accepted requests stayed in the 1–2 ms band.

Repeated voice-state calls (n=15) show the same flat profile as `/health`.

## 12.5 OCR timeout and recovery behaviour

* The suite's own wall-clock is the clearest recovery/timeout signal:
  **14.14 s** for the fixed tree (1002 tests), **22.07 s** for the broken tree, versus
  **481.35 s** for the injection-harness run in `PHASE9_TEST_AUDIT.md` §2.6. The 34×
  slowdown is the system repeatedly taking exception paths — evidence that a thrown
  `NameError` inside a tool is *caught and retried/recovered* rather than surfacing, which
  is what Phase 6 investigates.
* `tests/test_privacy.py` measured **70.48 s** in isolation under one probe configuration
  while taking **0.04 s** as part of the full run. Same tests, same tree — a 1700× spread
  pointing at module-level state (cached settings/DB) that the full ordering warms and an
  isolated run does not. Recorded as a suite-hygiene observation for Phase 6/7, not a
  product defect. **P2 (test hermeticity).**
* OCR/vision timeout was not directly forced (no image pipeline reachable without a
  configured model); the 2226 ms probe above is the closest measurement. **UNVERIFIED**
  for an actual OCR hang. To verify: configure an unreachable vision endpoint and call
  `/vision/read` on a real image, timing against `settings.listen_seconds`.

## 12.6 Summary judgement

The system is fast. Nothing measured exceeded 100 ms except a filesystem search (303 ms),
a vision capability probe (2.2 s), an oversized request (4.1 s) and the exception path in a
broken tree. Server boot, registry construction, all read endpoints, the confirmation
round-trip and the rate limiter are all comfortably interactive.

Performance is **not** the constraint for the next phase. Correctness and truthfulness are —
see `PHASE9_FINAL_REPORT.md`.

| ID | observation | severity |
|---|---|---|
| PERF-1 | No request body size cap → 4.07 s of planner CPU for one junk 2 MB request | **P2** |
| PERF-2 | `vision_capabilities` 2226 ms synchronous probe behind a UI tile | **P3** |
| PERF-3 | 19 separate `schema migrated` log events per boot | **P4** |
| PERF-4 | `test_privacy.py` 0.04 s in-suite vs 70.48 s isolated → shared-module state | **P2** |
| PERF-5 | Rate-limit knee is clean; steady-state read endpoints ≤3 ms; startup ≤733 ms | **PASS** |

## 12.7 Commands used

```bash
# harness (clean copy, P0 repaired locally)
rsync -a --exclude .venv --exclude __pycache__ --exclude .pytest_cache \
      --exclude dist --exclude app/build \
      /Users/abhijeetmiskin/AppData/jarv/jarvis-forge/ /tmp/p9shadow/
sed -i '' '16s/import ActionResult/import ActionResult, Verification/' /tmp/p9shadow/jarvis/tools/base.py

cd /tmp/p9shadow
JARVIS_HOME=$(mktemp -d) JARVIS_API_TOKEN=<fixture> PYTHONPATH=/tmp/p9shadow \
  /Users/abhijeetmiskin/AppData/jarv/jarvis-forge/.venv/bin/python -m uvicorn \
  jarvis.api.app:app --host 127.0.0.1 --port 8765

/Users/abhijeetmiskin/AppData/jarv/jarvis-forge/.venv/bin/python /tmp/p9_hw_perf.py   # latencies + hardware
/Users/abhijeetmiskin/AppData/jarv/jarvis-forge/.venv/bin/python /tmp/p9_api2.py       # burst / body size
JARVIS_HOME=$(mktemp -d) PYTHONPATH=/tmp/p9shadow \
  /Users/abhijeetmiskin/AppData/jarv/jarvis-forge/.venv/bin/python /tmp/p9_final.py    # registry, planner
```

All servers started for these measurements were terminated; `lsof -nP -iTCP:8765 -sTCP:LISTEN`
returns nothing at the time of writing.
