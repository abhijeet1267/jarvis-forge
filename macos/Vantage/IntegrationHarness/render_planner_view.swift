import AppKit
import SwiftUI

// Renders the REAL PlannerView offscreen so the native screen can be inspected
// where `screencapture` and the Accessibility API are refused (the project's
// documented technique). It hosts the same view the app shows, in a real
// borderless NSWindow with dark-aqua forced, against a running backend.
//
// This is a verification tool, not part of the shipped app: it lives outside
// `Sources/` and is compiled by `run_planner_render.sh` with the app's own
// source files. Nothing in the shipped bundle references it.

let app = NSApplication.shared
app.setActivationPolicy(.accessory)

func render(width: CGFloat = 1180, height: CGFloat = 1000, to path: String) {
    let appearance = NSAppearance(named: .aqua)
    app.appearance = appearance

    let window = NSWindow(
        contentRect: NSRect(x: 0, y: 0, width: width, height: height),
        styleMask: [.borderless],
        backing: .buffered,
        defer: false)
    // A borderless window resolves to the *light* appearance; force dark or the
    // 5%-white assistant fills read as white-on-white and look empty.
    window.appearance = appearance
    window.backgroundColor = NSColor(calibratedWhite: 0.07, alpha: 1)
    window.isReleasedWhenClosed = false

    let host = NSHostingView(rootView:
        PlannerView()
            .frame(width: width, height: height)
            .background(Color(white: 0.07))
            .preferredColorScheme(.light)
    )
    host.frame = NSRect(x: 0, y: 0, width: width, height: height)
    window.contentView = host
    window.orderFrontRegardless()

    // Let the view's .task fetch + render into the layer tree.
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

let args = CommandLine.arguments
let out = args.count > 1 ? args[1] : "artifacts/planner-native.png"
render(to: out)
exit(0)