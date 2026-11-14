import AppKit
import SwiftUI

// Renders the REAL Settings → Connection screen offscreen, against the fake
// backend in `fake_backend.py`, so the identity rows (Version / Revision /
// Branch / Instance / Started / Source folder) and the stale / old-build
// notices can be inspected as pixels rather than assumed.
//
// The connection state shown is whatever the fake backend's mode file says:
//
//   render_settings.swift <out.png>
//
// Environment (set by run_settings_render.sh):
//   JARVIS_BACKEND_PORT  the fake backend's ephemeral port
//   JARVIS_PROJECT_DIR   the source_root the fake backend reports, and the
//                        directory the app is told to expect — so the "same
//                        checkout" branch is taken and the rows render.
//
// This is a verification tool, not part of the shipped app: it lives outside
// `Sources/` and is compiled against the app's own source files. Nothing in
// the shipped bundle references it.

let app = NSApplication.shared
app.setActivationPolicy(.accessory)

let args = CommandLine.arguments
let out = args.count > 1 ? args[1] : "artifacts/settings-connection.png"

// Drive the REAL manager (real /health + /health/identity fetches, real state
// machine) before the view is hosted, so the first paint already has identity.
await BackendManager.shared.connect()
// Ask the real lifecycle script who owns the port BEFORE hosting the view, so
// the first paint shows a settled verdict (state row, action button, or the
// refusal notice) instead of the in-flight spinner.
await BackendManager.shared.refreshLifecycle()

let width: CGFloat = 1180, height: CGFloat = 1000
let appearance = NSAppearance(named: .aqua)
app.appearance = appearance
let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: width, height: height),
                      styleMask: [.borderless], backing: .buffered, defer: false)
window.appearance = appearance
window.backgroundColor = NSColor(calibratedWhite: 0.98, alpha: 1)
window.isReleasedWhenClosed = false

let host = NSHostingView(rootView: SettingsView()
    .frame(width: width, height: height)
    .preferredColorScheme(.light))
host.frame = NSRect(x: 0, y: 0, width: width, height: height)
window.contentView = host
window.orderFrontRegardless()

// Let the refresh land and the layers render.
//
// Poll with `Task.sleep`, NOT a `RunLoop.run` spin: this top-level task runs on
// the MainActor, and a spin loop would hold the actor the whole time, starving
// the hosted view's own `.task { refreshLifecycle() }` (observed: the page was
// captured mid-check with its spinner still on screen). Sleeping suspends this
// task and releases the MainActor so the view's task can finish its real
// Python status run; the capture then photographs a settled state.
try? await Task.sleep(nanoseconds: 1_000_000_000)
var settledSince: Date?
let deadline = Date().addingTimeInterval(25)
while Date() < deadline {
    if !BackendManager.shared.isLifecycleBusy,
       BackendManager.shared.lifecycleState != nil,
       BackendManager.shared.state == .running {
        if let since = settledSince, Date().timeIntervalSince(since) > 0.6 { break }
        if settledSince == nil { settledSince = Date() }
    } else {
        settledSince = nil
    }
    try? await Task.sleep(nanoseconds: 100_000_000)
}

// Capture Settings BEFORE hosting the root view: ContentView's own `.task`
// runs connect() on the shared manager, which would otherwise repaint the
// page already on screen as "Checking..." and hide what this render is for.
guard let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) else {
    print("FAIL: could not create bitmap rep"); exit(1)
}
host.cacheDisplay(in: host.bounds, to: rep)
guard let png = rep.representation(using: .png, properties: [:]) else {
    print("FAIL: could not encode PNG"); exit(1)
}
try? png.write(to: URL(fileURLWithPath: out))

// The sidebar footer is the other place the lifecycle verdict shows. Host the
// real root view in a second window so its footer can be inspected too.
let footerHost = NSHostingView(rootView: ContentView()
    .frame(width: 900, height: 600)
    .preferredColorScheme(.light))
footerHost.frame = NSRect(x: 0, y: 0, width: 900, height: 600)
let footerWindow = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 900, height: 600),
                            styleMask: [.borderless], backing: .buffered, defer: false)
footerWindow.appearance = appearance
footerWindow.contentView = footerHost
footerWindow.orderFrontRegardless()
try? await Task.sleep(nanoseconds: 2_000_000_000)

// Footer crop (bottom-left, the status line) saved beside the page render.
let footerRect = NSRect(x: 0, y: 450, width: 260, height: 150)
if let footerRep = footerHost.bitmapImageRepForCachingDisplay(in: footerRect) {
    footerHost.cacheDisplay(in: footerRect, to: footerRep)
    if let footerPNG = footerRep.representation(using: .png, properties: [:]) {
        let footerPath = (out as NSString).deletingPathExtension + "-footer.png"
        try? footerPNG.write(to: URL(fileURLWithPath: footerPath))
        print("wrote \(footerPath)")
    }
}
print("wrote \(out)")
print("STATE=\(BackendManager.shared.state) STALE=\(BackendManager.shared.stale) "
      + "WARNING=\(BackendManager.shared.warning ?? "nil")")
print("LIFECYCLE=\(BackendManager.shared.lifecycleState?.kind.rawValue ?? "nil") "
      + "ERROR=\(BackendManager.shared.lifecycleError ?? "nil") "
      + "OUTCOME=\(BackendManager.shared.lastLifecycleOutcome ?? "nil")")
if let id = BackendManager.shared.identity {
    print("IDENTITY=\(id.service) rev=\(id.revision ?? "nil") root=\(id.source_root ?? "nil")")
}
exit(0)