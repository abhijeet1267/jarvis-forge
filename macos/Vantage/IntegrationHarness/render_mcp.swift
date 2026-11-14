import AppKit
import SwiftUI

// Renders the REAL MCPView offscreen against a running backend, at a height
// tall enough to show the configuration editor as well as the connection
// card. Verification tool only: lives outside `Sources/` and is compiled by
// run_mcp_render.sh with the app's own sources.

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let wantsLight = !CommandLine.arguments.contains("-d")
let appearance = NSAppearance(named: wantsLight ? .aqua : .darkAqua)
app.appearance = appearance

let args = CommandLine.arguments
let out = args.count > 1 && !args[1].hasPrefix("-") ? args[1] : "artifacts/mcp-native.png"
let width: CGFloat = 1180
let height: CGFloat = 1900

let window = NSWindow(
    contentRect: NSRect(x: 0, y: 0, width: width, height: height),
    styleMask: [.borderless], backing: .buffered, defer: false)
window.appearance = appearance
window.backgroundColor = NSColor(calibratedWhite: wantsLight ? 0.98 : 0.07, alpha: 1)
window.isReleasedWhenClosed = false

let host = NSHostingView(rootView:
    MCPView()
        .frame(width: width, height: height)
        .preferredColorScheme(wantsLight ? .light : .dark)
)
host.frame = NSRect(x: 0, y: 0, width: width, height: height)
window.contentView = host
window.orderFrontRegardless()

let deadline = Date().addingTimeInterval(8)
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
try? png.write(to: URL(fileURLWithPath: out))
print("wrote \(out)")
exit(0)