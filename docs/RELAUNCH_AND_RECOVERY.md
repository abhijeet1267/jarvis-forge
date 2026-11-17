# Relaunch and Recovery — live audit of the macOS app's backend lifecycle

Measured 2026-11-17 against the built bundle
`macos/Vantage/Vantage.app` (revision `2fd2c8bc3fb8+dirty`), on a disposable
profile and port so the user's backend was never touched:

- `JARVIS_HOME=/tmp/v23_recovery*` (fresh empty dirs per run)
- `JARVIS_BACKEND_PORT=8790` (verified free with `scripts/dev_port_guard.sh check 8790`)
- The user's backend on `:8765` (pid 30081) was verified LISTEN before and after
  every run and was never signalled.

Method: launched `Vantage.app/Contents/MacOS/Vantage` directly with those
environment variables, inspected processes with `ps`/`lsof`, the port with
`curl /health` and `/health/identity`, the startup trace at
`$JARVIS_HOME/backend-startup.log`, and the UI with `screencapture -l <CGWindowID>`
plus Accessibility-driven menu actions.

## Results

| # | Scenario | What actually happened | Verdict | Code path responsible |
|---|----------|------------------------|---------|-----------------------|
| 1a | First launch, nothing on the port | App (pid 32846) spawned exactly one bundled backend (pid 32848); `/health/identity` reports `service=vantage-backend`, `source_root=` the bundle's `Contents/Resources`. Trace: 3 lines (`BACKEND_START_REQUESTED`, `PYTHON_RUNTIME_RESOLVED`, `BACKEND_PROCESS_STARTED pid=32848`). | PASS | `BackendManager.swift:284-314` (`connect` → `startBackend`), spawn at `:572-659`, trace at `:584/:599/:633` |
| 1b | `kill -9` the **app** only, then relaunch | Orphan backend survived (reparented to ppid 1, `/health` still 401). Relaunch (pid 32935) **attached silently** to it: still exactly 1 backend, trace still 3 lines (no new start), UI badge "Running / Backend connected". No second backend is ever spawned over an owned port. | NEEDS-WORK (attaches, but silently; no trace evidence of adoption) | `connect()` takes the `owned` branch at `:286-310`; `checkBackend():435-452` treats any HTTP answer as "owned"; nothing is written to the trace on adoption |
| 1c | Quit an app that **adopted** an orphan backend (`SIGTERM`) | App exited in ~500 ms; the adopted backend (pid 32848) **kept running** (ppid 1, still serving 401). The port stays occupied by a process the app no longer tracks. | FAIL | `stopBackend():661-665` — `guard let proc = process, proc.isRunning else { process = nil; return }`: an adopted backend was never assigned to `process`, so nothing is stopped |
| 1d | Quit an app that **spawned** its backend (`SIGTERM`) | App exited in ~500 ms; its spawned backend stopped with it, `:8790` released. | PASS | `terminationSignals` handler (`VantageApp.swift:92-104`) → `disconnect()` (`BackendManager.swift:316-324`) → `stopBackend()` (`:666-680`), SIGTERM→SIGINT escalation |
| 2a | `kill -9` the **backend** while the app runs — detection | App detected within one poll (≤30 s): `/health` fails, state becomes `.error`, UI shows red "Down", "Backend unreachable — retrying…". | PASS (detection) | `startHealthPolling():687-701` (`pollInterval = 30`, `:89`) → `refresh()` → `refreshHealth():376-387` |
| 2b | Same crash — automatic restart | No restart, ever. Watched 60 s after `kill -9`: 0 respawns, trace frozen. The poll loop only calls `refresh()`; it never calls `startBackend()`. | FAIL | `startHealthPolling():697-698` — the loop body is `guard canRecover` + `await self.refresh()` only |
| 2c | Same crash — **manual** Backend ▸ Connect | Nothing spawned; **no** trace line; UI wedged permanently at "Starting…"/"Checking…" (neither "Running" nor "Down"). Root cause: `process` still holds the dead `Process` object (no `terminationHandler` ever nils it), so `startBackend` returns at the guard. | FAIL | `startBackend():577-579` — `guard process == nil else { return }` with a stale handle; `connect():285` sets `.checking` first, so the early return leaves the UI in `.checking` forever |
| 2d | Same crash — Backend ▸ **Disconnect**, then Connect | Fully recovers: `disconnect()` → `stopBackend()` clears the stale handle at `:662-665`, then Connect spawns a new backend (pid 33428; trace 3 → 6 lines), badge "Running". This confirms the 2c diagnosis and yields a workaround. | PASS (workaround only) | `stopBackend():662-665` nils `process`; `connect():311-312` then reaches `startBackend()` |
| 3a | **Raw TCP listener** (no HTTP) already on the port | `checkBackend` times out after 3 s and returns "free" (`:438/:449-451`), so the app spawns a doomed child (`BACKEND_PROCESS_STARTED pid=32021`). `run.py` refuses the busy port and exits; its refusal text is written into a `Pipe` nobody reads. After 15 s the UI claims "Backend did not respond within 15s" — misleading; the real reason (port held by pid 31994) is never shown, and the holder is never identified. | NEEDS-WORK | `checkBackend():435-452` cannot distinguish "free" from "listening but not speaking HTTP in time"; output pipe never drained at `:627-629`; timeout message at `:658`; refusal at `run.py:167-181` |
| 3b | **Foreign HTTP service** (answers 200, `service=some-other-tool`) on the port | The app started **nothing** (no trace file at all) and reported it: UI "Down", footer "The service on http://127.0.0.1:8790 answered h…" — i.e. "answered health checks but did not report a Vantage identity. Nothing was started." | PASS (never attaches silently to a foreign HTTP service) | `checkBackend():448` → `refresh()` → `checkIdentity():488-499` |

## Concrete defects

1. **Backend crash is unrecoverable without an explicit Disconnect.**
   Symptom: after the backend dies, Backend ▸ Connect does nothing — no process
   spawns, no trace line is written, and the UI sits at "Starting…"/"Checking…"
   indefinitely (observed: 0 listeners on `:8790` after pressing Connect).
   Cause: `macos/Vantage/Sources/Services/BackendManager.swift:577-579`
   (`guard process == nil else { return }`) — `process` still references the dead
   `Process`; nothing nils it (`terminationHandler` is never set on the spawned
   process), and the poll loop at `:697-698` never restarts anything. This is the
   highest-priority defect: the only correctness-preserving recovery is a manual
   Backend ▸ Disconnect first, which is undiscoverable and not hinted in the UI.

2. **An adopted backend is not stopped when the app quits.**
   Symptom: kill the app while it is attached to an orphan backend, relaunch,
   quit — the original backend survives every app exit (ppid 1, still holding the
   port). Cause: `BackendManager.swift:661-665` — `stopBackend` only terminates a
   `process` the app itself spawned; `connect()`'s adopted path (`:286-310`)
   never records the adopted pid.

3. **A non-HTTP port holder produces a false "did not respond within 15s".**
   Symptom: with a raw listener on the port, the app spawns a child that
   immediately dies, and after 15 s reports a timeout instead of "port in use by
   pid N". Cause: `BackendManager.swift:435-452` treats a 3 s non-answer as a
   free port, so the port-in-use pre-check in `run.py:167-181` fires *after* the
   spawn — and its explanation is then discarded (defect 4).

4. **The backend's own refusal/diagnostic output is discarded.**
   Symptom: `run.py`'s "REFUSED to start -- port 8790 is already in use (pid …)"
   never appears in the trace, the UI, or anywhere a user can see it. Cause:
   `BackendManager.swift:627-629` assigns one `Pipe` to both stdout and stderr
   and never reads it.

5. **The startup trace records spawns only — not attachment, detection, or
   failure to start.**
   Symptom: after relaunch-adoption the trace stays at its previous 3 lines; a
   crashed backend and a refused manual Connect leave no evidence at all, so the
   log cannot explain the most common recovery failures. Cause: `trace()` is
   called exclusively from `startBackend` (`:584`, `:599`, `:633`, `:635`);
   `connect()`'s adopted branch, `refreshHealth()`'s failure path and
   `startBackend`'s early return at `:577-579` are silent.

## Notes and limits

- Experiment 2's first attempt was spoiled by a leftover orphan backend from
  Experiment 1 (the new app attached to it). Every recorded result above is from
  a clean profile/port with pids captured in that run.
- One test app exited on its own before it was signalled; no crash report or
  unified-log entry explained it. It was replaced by a fresh instance and is
  noted as an unexplained anomaly, not a verdict.
- Process teardown and UI states were verified live in this GUI session;
  the SwiftUI views themselves were not modified.