# Executor GUI-connection remedies — measured options

Scope: the app-spawned executor's `NSWorkspace.frontmostApplication` read. The
coordinator's finding (no second GUI session; `frontmostApplication` returns
`nil` when the process has no WindowServer connection) is taken as the premise.
This document answers one question first — **does initializing AppKit in the
executor before the query fix it?** — and records what a supported remedy would
cost.

All measurements on 2026-11-18, macOS 26 (Darwin 25.3.0), user 501, console
session `kCGSSessionIDKey 257`, `asid 100023`. Probes were compiled with
`swiftc -O` and run without modifying any project file.

## Answer to point 3 — NSApplication does not fix it

**No supported in-process AppKit initialization changes the result.** A process
that has no WindowServer connection stays without one even after
`NSApplication.shared`, `finishLaunching()`, `setActivationPolicy(.accessory/
.regular)` and a spun run loop. AppKit initialization does not acquire a
connection; it only configures one that already exists.

The constraint this creates for measurement: a process can only be *without* a
WindowServer connection when its bootstrap is not the GUI session. The running
app's own environment is GUI-connected, so to build a true negative control I
used `launchctl bsexec <pid> …`, which enters another process's bootstrap
namespace without the GUI session.

### The negative control

```
$ launchctl bsexec 77186 /tmp/a2probe/probe2
Failure on line 688 … scheduleApplicationNotification(…): noErr == _LSModifyNotification(…)
before frontmost=nil session=nil
after_finishLaunching frontmost=nil session=nil policy=-1
after_runloop frontmost=nil session=nil
```

`policy=-1` is `NSApplicationActivationPolicyProhibited`: AppKit is telling us
the process is not allowed to become a GUI app at all in that bootstrap.
`CGSessionCopyCurrentDictionary()` returns `nil`, i.e. no window-server session
membership. `NSApplication.shared` completed without crashing and changed
nothing.

The full mode sweep in the same context (all modes, before and after identical):

| mode | frontmost before | frontmost after | activation policy |
|---|---|---|---|
| raw (no AppKit init) | nil | nil | — |
| `NSApplication.shared` | nil | nil | **-1 (prohibited)** |
| `.accessory` | nil | nil | -1 |
| `.regular` | nil | nil | -1 |

### Positive controls (GUI-connected contexts)

The same binary, same modes, when the process is in the GUI session (spawned by
this Claude.app shell, by a GUI-launched `.app`, or by `launchctl submit` into
`gui/501`):

| launch context | frontmost before init | after `NSApplication.shared` |
|---|---|---|
| Claude.app shell (pid tree under 66347) | Brave Browser | Brave Browser |
| minimal GUI `.app` → `Process` child | Brave Browser | Brave Browser |
| … → `/bin/sh` grandchild | Brave Browser | Brave Browser |
| … → `/usr/bin/python3` → `subprocess.run` (executor's exact shape) | Brave Browser | Brave Browser |
| `launchctl submit` into `gui/501` | Brave Browser | Brave Browser |
| `launchctl bsexec 1` (launchd bootstrap, non-GUI) | nil | nil |

So the signal tracks **bootstrap/session membership**, not AppKit initialization
and not the parent app. In every GUI-connected context the read works *before*
any AppKit call; in the non-GUI context no AppKit call helps. Initialization is
not on the causal path in either direction.

Cost of the change, if it were worth having anyway: `NSApplication.shared` adds
~33–51 ms (3 runs: 33, 39, 51) to a short-lived executor process. Since it does
not change the outcome, that cost buys nothing.

## Point 1 — how the backend is spawned

`macos/Vantage/Sources/Services/BackendManager.swift` (`startBackend()`):

```swift
let proc = Process()
proc.executableURL = URL(fileURLWithPath: interpreter)   // Python
proc.arguments = [projectDir + "/run.py", "serve", "--port", …, "--host", …]
proc.environment = env
try proc.run()
```

Plain POSIX `Process` (fork/exec), inheriting the app's environment; **no**
`NSWorkspace.openApplication`, **no** LaunchServices, no `NSTask` GUI semantics
(`Process` *is* `NSTask`'s modern name; there is no GUI-aware variant). The
child is a normal detached process — it keeps the app's session because fork
inherits the bootstrap, which is why a Finder/GUI-launched app's chain is
GUI-connected. The executor is then spawned one level further down by
`jarvis/desktop/executor.py` via `subprocess.run([binary, operation, …])`.

## Point 2 — moving the executor spawn to the GUI process

Not currently possible without new plumbing:

- The executor is invoked **once per operation**, synchronously, from Python:
  `jarvis/desktop/executor.py::_run()` does `subprocess.run([...], capture_output=True, timeout=…)`
  and parses one JSON object from stdout. Every tool and every `/desktop/*`
  route reaches it through that function.
- The SwiftUI app has its own, *separate* desktop path
  (`macos/Vantage/Sources/Services/MacControl/`), which does not go through
  `desktop/executor.py`. The two paths already exist side by side.
- Routing the Python path's operations through the app would require an IPC
  channel (the app would have to expose the executor as a service, or the backend
  would have to hand each call to an app-side helper) — a new contract, a new
  failure mode, and a second implementation of every operation in scope.

Cost: high. Benefit: measured as **none** for this symptom — a child of the GUI
process is GUI-connected already (see positive controls), so the spawn location
is not what determines the read. It would matter only for a backend whose own
bootstrap is non-GUI, and no such backend exists in the product.

## The one thing I could not reproduce

**No real, GUI-session process in this session ever returned `nil`.** Every
momentary `permission_required` I observed on port 8765 was `screen_capture`
false (a TCC state, not a WindowServer one) and resolved within minutes without
a restart:

| when | process on 8765 | status | frontmost | note |
|---|---|---|---|---|
| 12:47 | app backend 65478 (bundle executor) | permission_required | — | `screen_capture:false` for the bundle's executor |
| 12:57 | app backend 65478 (bundle executor) | permission_required | — | same |
| 13:12 | legacy `/Applications/Vantage.app` 77185 → backend 77186 → checkout executor | **verified** | **Brave Browser**, 24 windows | repeated 3× |

The `nil` symptom is only reproducible in a bootstrap with no GUI session.
Whether a GUI-launched app's backend can ever land in such a bootstrap has not
been observed here. `launchctl managername` reports `Aqua` for this shell and for
the app; `ps -o sess=` is `0` for every GUI process, so it does not discriminate.

## Options, ranked by cost

1. **No change (measured recommendation).** Do not add `NSApplication.shared` to
   the executor: it costs 33–51 ms per operation and changes nothing. Record the
   nil behaviour as a property of a process outside the GUI session, and keep the
   existing fail-closed guards (`expect_app` refuses rather than guessing).
2. **Detect and say so.** If the product needs to distinguish "frontmost is nil
   because nothing is frontmost" from "nil because this process is not in a GUI
   session", the discriminator is `CGSessionCopyCurrentDictionary() == nil` (or
   `NSApplication.activationPolicy() == .prohibited`). That is a read-only,
   one-call addition to the executor's error text — no behaviour change.
3. **Helper `.app` with `LSUIElement`.** A small signed GUI helper that the app
   launches and the backend talks to over IPC. This is the only supported way to
   guarantee GUI membership regardless of how the backend was started. It is a
   new process, new IPC, new signing/TCC identity, and duplicates the executor
   surface in scope today. Justified only if the product must run the executor
   from a backend whose bootstrap is not the GUI session.

Option 1 is what the measurements support. Option 2 is the only change with a
positive ratio of information gained to risk introduced.