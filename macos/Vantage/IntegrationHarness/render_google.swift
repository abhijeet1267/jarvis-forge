import AppKit
import SwiftUI

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
app.appearance = NSAppearance(named: .aqua)
let width: CGFloat = 900, height: CGFloat = 1000
let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: width, height: height),
                      styleMask: [.borderless], backing: .buffered, defer: false)
window.appearance = NSAppearance(named: .aqua)
window.backgroundColor = NSColor(calibratedWhite: 0.07, alpha: 1)
let host = NSHostingView(rootView: GoogleView()
    .frame(width: width, height: height)
    .background(Color(white: 0.07))
    .preferredColorScheme(.light))
host.frame = NSRect(x: 0, y: 0, width: width, height: height)
window.contentView = host
window.orderFrontRegardless()
let d = Date().addingTimeInterval(7)
while Date() < d { RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.05)) }
let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds)!
host.cacheDisplay(in: host.bounds, to: rep)
let out = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "artifacts/google.png"
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: out))
print("wrote \(out)")
exit(0)
