import AppKit
import SwiftUI

// Renders the REAL ConnectionsView offscreen against a running backend so the
// native screen can be inspected where `screencapture` and the Accessibility
// API are refused (the project's documented technique). It hosts the same view
// the app shows in a real borderless NSWindow with dark-aqua forced.
//
// The window renders whichever appearance the caller asks for: pass "-l" (or
// set VANTAGE_RENDER_LIGHT=1) for the designed light appearance, anything else
// for dark. Both are token-driven, so the same file proves the tokens resolve
// in each scheme instead of assuming dark is the only one that works.
//
// This is a verification tool, not part of the shipped app: it lives outside
// `Sources/` and is compiled by `run_connections_render.sh` with the app's own
// source files. Nothing in the shipped bundle references it.

let app = NSApplication.shared
app.setActivationPolicy(.accessory)

let args = CommandLine.arguments
let out = args.count > 1 ? args[1] : "artifacts/connections-native.png"
let wantsLight = !args.contains("-d") && ProcessInfo.processInfo.environment["VANTAGE_RENDER_DARK"] != "1"

func render(width: CGFloat = 1180, height: CGFloat = 1000, to path: String) {
    let appearance = NSAppearance(named: wantsLight ? .aqua : .darkAqua)
    app.appearance = appearance

    let window = NSWindow(
        contentRect: NSRect(x: 0, y: 0, width: width, height: height),
        styleMask: [.borderless],
        backing: .buffered,
        defer: false)
    // A borderless window resolves to the *light* appearance by default; set it
    // explicitly so both schemes are deliberate rather than incidental.
    window.appearance = appearance
    window.backgroundColor = NSColor(calibratedWhite: wantsLight ? 0.98 : 0.07, alpha: 1)
    window.isReleasedWhenClosed = false

    let host = NSHostingView(rootView:
        ConnectionsView()
            .frame(width: width, height: height)
            .preferredColorScheme(wantsLight ? .light : .dark)
    )
    host.frame = NSRect(x: 0, y: 0, width: width, height: height)
    window.contentView = host
    window.orderFrontRegardless()

    // Let the view's .task fetch (both statuses) + render into the layer tree.
    let deadline = Date().addingTimeInterval(6)
    while Date() < deadline {
        RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.05))
    }

    guard let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) else {
        print("FAIL: could not create bitmap rep")
        exit(1)
    }
    host.cacheDisplay(in: host.bounds, to: rep)
    guard let png = rep.representation(using: .png, properties: [:]) else {
        print("FAIL: could not encode png")
        exit(1)
    }
    try? png.write(to: URL(fileURLWithPath: path))
    print("wrote \(path)")
}

render(to: out)
exit(0)