# Correction: the app's backend *can* see the frontmost application

A prior session hypothesised "two GUI sessions". Measured, that hypothesis is
**wrong**, and the real cause is narrower and more specific.

## Measurement one — same session, not different

```
shell              sess=0
Vantage app  65474 sess=0  ppid=1
backend      65478 sess=0  ppid=65474
console owner         abhijeetmiskin
```

Everything shares session 0 and the console belongs to the same user. There is no
second GUI session. The hypothesis that the backend sits in a different session is
**refuted**.

## Correction, later the same day

**The conclusion below is wrong and is kept only as a record of the error.**

Re-measured against the installed app's own backend (`/Applications/Vantage.app`,
pid 77185, spawning backend 77186) three times in a row:

```
frontmost: 'Brave Browser' | screen_capture: True | windows: 25
frontmost: 'Brave Browser' | screen_capture: True | windows: 25
frontmost: 'Brave Browser' | screen_capture: True | windows: 25
```

An app-spawned backend reads the frontmost application perfectly well. The `nil`
I measured earlier was a transient state, most likely a screen-capture denial:
the log shows the display asleep 08:51–09:42, overlapping that measurement, and a
later momentary failure on the same endpoint showed `screen_capture: false` with
the same generic failure shape.

A worker then tested this properly with a negative control — `launchctl bsexec`
into a non-GUI bootstrap — and confirmed that `NSApplication.shared`,
`finishLaunching()`, `setActivationPolicy` and a run loop all change nothing: in a
bootstrap with no GUI session the read stays `nil`, and in every GUI-connected
context (including a `Process` child of a GUI app) it works before any AppKit
call. So there is no launch-context defect to fix and no product change is
warranted.

**What survives from the analysis below:** nothing about the app path. The
measurement is retained because the reasoning error is instructive — a single
`nil` was treated as a structural property when it was a transient, and two
subsequent documents repeated it.

The `expect_app` guard therefore works from the app's backend. It has not been
exercised end to end because that requires posting real keystrokes.

---

## The superseded analysis (kept as record)

## Measurement two — the contrast that identifies the cause

The same executor binary, invoked two ways, at the same moment:

| how it was spawned | `frontmost_application` |
|---|---|
| from a terminal | `'Safari'` |
| by the Vantage app's backend | `null` |

Both run as the same user, in the same session, with the same `NSWorkspace.shared`
call (`native/desktop_executor/main.swift:634`). One sees the frontmost
application; the other gets `nil`.

## What this is

`NSWorkspace.frontmostApplication` is queried from a process without a WindowServer
connection and returns `nil`. A terminal-spawned process inherits the terminal's
GUI connection; a process spawned by a `.app` bundle's backend does not — unless it
connects itself. This is documented AppKit behaviour for non-GUI processes, not a
TCC denial and not a session mismatch.

Supporting evidence from the same process: `windows` **works** from the app's
backend (24 windows enumerated, with real bounds). Window *enumeration* goes
through the window server by a path that does not need frontmost-app state. So the
backend has screen access and lacks only the frontmost-app query.

## What this means for the product

1. **`expect_app` is inert from the app's backend.** The guard compares the
   frontmost application against an expected one; when the read returns `nil` the
   comparison cannot succeed, so a `type` or `key` step carrying `expect_app`
   will refuse with `target_stale` — a safe failure, but a failure nonetheless.
   It is safe in the direction that matters (it does not type into the wrong app),
   and unusable in the direction the feature was for.

2. **The earlier "the fixture stopped publishing AX" conclusion was also wrong.**
   The fixture was healthy. AX queries for *another* application also need the
   caller to be a GUI-connected process. That is the same root cause, not a
   second one.

3. **It does not affect coordinate actions.** `click`, `drag`, `scroll` and
   `move` use window-server bounds and window ids — both of which the app's
   backend reads successfully. The stale-window guard works there: measured
   `target_not_found` and `target_stale` against the bundled executor.

## The fix, and why it is not in this change

The supported remedy is for the process that needs GUI state to connect to the
WindowServer — for the app's backend to be spawned with, or to establish, a GUI
session. Every supported way of doing that is a product change with real
consequences (a helper `.app` with `LSUIElement`, or spawning the executor via
`NSWorkspace` from the GUI process rather than `Process` from the backend), and
none of it should be guessed at.

**Recommended next step, in order of cost:**

1. Have the SwiftUI app spawn the executor itself for operations that need
   frontmost-app state, rather than routing them through the backend's `Process`.
2. Or give the backend process `LSUIElement`-style GUI registration through a
   small helper bundle.
3. Or accept the limitation, and treat `expect_app` as unavailable from the
   app-launched path while documenting that a terminal-run executor can use it.

## What remains genuinely unknown

Whether a Finder-launched `Vantage.app` (as opposed to the checkout binary
launched the way this session launched it) inherits a GUI connection for its
children. That cannot be determined without installing and launching it from
Finder, which is the user's step. **UNVERIFIED.**
