import AppKit
import SwiftUI

// Render the real WorkView offscreen against a running backend (see
// run_planner_render.sh for why screencapture/Accessibility are not usable here).
let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let width: CGFloat = 1180, height: CGFloat = 1000
let appearance = NSAppearance(named: .aqua)
app.appearance = appearance
let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: width, height: height),
                      styleMask: [.borderless], backing: .buffered, defer: false)
window.appearance = appearance
window.backgroundColor = NSColor(calibratedWhite: 0.07, alpha: 1)
window.isReleasedWhenClosed = false
let host = NSHostingView(rootView: ApprovalsView()
    .frame(width: width, height: height)
    .background(Color(white: 0.07))
    .preferredColorScheme(.light))
host.frame = NSRect(x: 0, y: 0, width: width, height: height)
window.contentView = host
window.orderFrontRegardless()
let deadline = Date().addingTimeInterval(6)
while Date() < deadline { RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.05)) }
guard let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { exit(1) }
host.cacheDisplay(in: host.bounds, to: rep)
guard let png = rep.representation(using: .png, properties: [:]) else { exit(1) }
let out = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "artifacts/approvals.png"
try? png.write(to: URL(fileURLWithPath: out))
print("wrote \(out)")

// Print the laid-out frames so alignment is measured, not eyeballed. SwiftUI
// does not expose subview frames directly, but the hosting view does.
func dump(_ v: NSView, _ depth: Int = 0) {
    if depth > 4 { return }
    let r = v.frame
    if r.width > 100 {
        print(String(repeating: " ", count: depth) +
              "\(type(of: v)) x=\(Int(r.minX)) w=\(Int(r.width))")
    }
    for sub in v.subviews { dump(sub, depth + 1) }
}
dump(host)
exit(0)
