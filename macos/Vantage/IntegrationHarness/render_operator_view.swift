import AppKit
import SwiftUI

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let width: CGFloat = 1180, height: CGFloat = 980
let appearance = NSAppearance(named: .aqua)
app.appearance = appearance
let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: width, height: height),
                      styleMask: [.borderless], backing: .buffered, defer: false)
window.appearance = appearance
window.backgroundColor = NSColor(calibratedWhite: 0.07, alpha: 1)
window.isReleasedWhenClosed = false
// Taller than the shipped window on purpose: the applications list alone fills
// the normal height, so the sections below it were never rendered into the image
// and a screenshot would "confirm" them without showing them.
let host = NSHostingView(rootView: OperatorView()
    .frame(width: width, height: height)
    .background(Color(white: 0.07))
    .preferredColorScheme(.light))
host.frame = NSRect(x: 0, y: 0, width: width, height: height)
window.contentView = host
window.orderFrontRegardless()
// Scroll to the bottom so sections below a long list are captured — the
// applications list alone is taller than the window, and a screenshot that only
// shows the top would "confirm" the inbox without ever seeing it.
if let clip = host.subviews.compactMap({ $0 as? NSScrollView }).first,
   let doc = clip.documentView {
    let bottom = doc.bounds.height - clip.bounds.height
    if bottom > 0 { doc.scroll(NSPoint(x: 0, y: bottom)) }
}

let deadline = Date().addingTimeInterval(8)
while Date() < deadline {
    RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.05))
}
guard let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) else {
    print("FAIL: no bitmap rep"); exit(1)
}
host.cacheDisplay(in: host.bounds, to: rep)
guard let png = rep.representation(using: .png, properties: [:]) else {
    print("FAIL: no png"); exit(1)
}
try? png.write(to: URL(fileURLWithPath: CommandLine.arguments[1]))
print("wrote \(CommandLine.arguments[1])")
