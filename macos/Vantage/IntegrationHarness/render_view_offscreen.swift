import AppKit
import SwiftUI

// Render one of the list views offscreen. Used to check what the *failed read*
// path draws: every one of these screens used to print "nothing here" underneath
// an error banner, so the render is pointed at a port with no server.
let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let width: CGFloat = 1100, height: CGFloat = 760
let appearance = NSAppearance(named: .aqua)
app.appearance = appearance
let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: width, height: height),
                      styleMask: [.borderless], backing: .buffered, defer: false)
window.appearance = appearance
window.backgroundColor = NSColor(calibratedWhite: 0.07, alpha: 1)
window.isReleasedWhenClosed = false

let which = CommandLine.arguments.count > 2 ? CommandLine.arguments[2] : "devices"
let root: AnyView
switch which {
case "devices": root = AnyView(DevicesView())
case "memory": root = AnyView(MemoryItemsList())
case "projects": root = AnyView(ProjectsList())
case "tasks": root = AnyView(TasksList())
case "audit": root = AnyView(AuditList())
case "providers": root = AnyView(ProvidersView())
default:
    FileHandle.standardError.write("unknown view \(which)\n".data(using: .utf8)!)
    exit(2)
}

let host = NSHostingView(rootView: root
    .frame(width: width, height: height)
    .background(Color(white: 0.07))
    .preferredColorScheme(.light))
host.frame = NSRect(x: 0, y: 0, width: width, height: height)
window.contentView = host
window.orderFrontRegardless()

// Long enough for the async load to fail and the error branch to lay out.
let deadline = Date().addingTimeInterval(6)
while Date() < deadline { RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.05)) }

guard let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { exit(1) }
host.cacheDisplay(in: host.bounds, to: rep)
guard let png = rep.representation(using: .png, properties: [:]) else { exit(1) }
let out = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "artifacts/view.png"
try? png.write(to: URL(fileURLWithPath: out))
print("wrote \(out)")
exit(0)