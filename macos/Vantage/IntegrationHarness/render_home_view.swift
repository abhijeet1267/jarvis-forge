import AppKit
import SwiftUI

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
let host = NSHostingView(rootView: HomeView()
    .frame(width: width, height: height)
    .background(Color(white: 0.07))
    .preferredColorScheme(.light))
host.frame = NSRect(x: 0, y: 0, width: width, height: height)
window.contentView = host
window.orderFrontRegardless()
let deadline = Date().addingTimeInterval(7)
while Date() < deadline { RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.05)) }
guard let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { exit(1) }
host.cacheDisplay(in: host.bounds, to: rep)
guard let png = rep.representation(using: .png, properties: [:]) else { exit(1) }
let out = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "artifacts/home-native.png"
try? png.write(to: URL(fileURLWithPath: out))
print("wrote \(out)")
exit(0)
