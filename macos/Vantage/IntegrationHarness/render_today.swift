import AppKit
import SwiftUI

// Offscreen render of the Today workspace against a real backend (a scratch
// home with no Google token, so the honest failure/empty states are what show).
// This is pixels — it proves the screen draws its states, not that Google was
// called.

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
app.appearance = NSAppearance(named: .aqua)

let width: CGFloat = 900, height: CGFloat = 1180
let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: width, height: height),
                      styleMask: [.borderless], backing: .buffered, defer: false)
window.appearance = NSAppearance(named: .aqua)
window.backgroundColor = NSColor(calibratedWhite: 0.07, alpha: 1)

let host = NSHostingView(rootView: TodayView()
    .frame(width: width, height: height)
    .background(Color(white: 0.07))
    .preferredColorScheme(.light))
host.frame = NSRect(x: 0, y: 0, width: width, height: height)
window.contentView = host
window.orderFrontRegardless()

// Long enough for the screen's `.task` to finish its reads against the backend.
let deadline = Date().addingTimeInterval(10)
while Date() < deadline {
    RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.05))
}

let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds)!
host.cacheDisplay(in: host.bounds, to: rep)
let out = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "artifacts/today.png"
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: out))
print("wrote \(out)")
exit(0)