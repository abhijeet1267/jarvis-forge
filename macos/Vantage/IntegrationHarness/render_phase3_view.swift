import AppKit
import SwiftUI

// Renders one of the Phase 3 screens offscreen, against a real backend where the
// screen reads one, so the native UI can actually be inspected — `screencapture`
// and the Accessibility API are refused in this environment, which is the same
// reason every other screen here has a renderer.
//
// The screen is chosen by argv[1]; the PNG path by argv[2]. It hosts the real
// view, not a mock, so a layout or binding error shows up as a blank or broken
// image rather than passing silently.

let app = NSApplication.shared
app.setActivationPolicy(.accessory)

let screen = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "capture"
let out = CommandLine.arguments.count > 2 ? CommandLine.arguments[2] : "artifacts/phase3.png"

// A real document, written to a scratch file and read through the real
// extractor, so the Documents screen renders its populated state. This is the
// renderer's own setup, not something the app does: the app only reads a file a
// person selected.
if screen == "documents", let dir = ProcessInfo.processInfo.environment["VANTAGE_RENDER_DOC_DIR"] {
    let url = URL(fileURLWithPath: dir).appendingPathComponent("Quarterly review.txt")
    let body = """
    Quarterly review — AMR programme

    The demo is booked for 14 November. Priya owns the deck and will send a draft by Friday.

    Decisions:
    - Ship the navigation fix before the demo.
    - Hold the second hardware order until the pilot closes.

    Action items:
    1. Send the deck to Priya for review
    2. Book the demo room
    3. Update the roadmap with the new dates
    """
    try? body.write(to: url, atomically: true, encoding: .utf8)
    if let source = try? DocumentExtractor.describe(url),
       let extracted = try? DocumentExtractor.extract(source) {
        MainActor.assumeIsolated { DocumentRuntime.shared.setPrimary(extracted) }
    }
}

func rootView(_ name: String) -> AnyView {
    switch name {
    case "documents": return AnyView(DocumentsView())
    case "briefing": return AnyView(BriefingView())
    case "preferences": return AnyView(PreferencesView())
    default: return AnyView(CaptureInboxView())
    }
}

let width: CGFloat = 1180
let height: CGFloat = 1000
let appearance = NSAppearance(named: .aqua)
app.appearance = appearance
let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: width, height: height),
                      styleMask: [.borderless], backing: .buffered, defer: false)
window.appearance = appearance
window.backgroundColor = NSColor(calibratedWhite: 0.07, alpha: 1)
window.isReleasedWhenClosed = false
let host = NSHostingView(rootView: rootView(screen)
    .frame(width: width, height: height)
    .background(Color(white: 0.07))
    .preferredColorScheme(.light))
host.frame = NSRect(x: 0, y: 0, width: width, height: height)
window.contentView = host
window.orderFrontRegardless()

// Long enough for the screen's `.task` to make its backend reads; a briefing
// against a real server needs a moment.
let deadline = Date().addingTimeInterval(8)
while Date() < deadline {
    RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.05))
}

guard let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { exit(1) }
host.cacheDisplay(in: host.bounds, to: rep)
guard let png = rep.representation(using: .png, properties: [:]) else { exit(1) }
try? png.write(to: URL(fileURLWithPath: out))
print("wrote \(out)")
exit(0)