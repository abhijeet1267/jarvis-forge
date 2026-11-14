import AppKit
import SwiftUI

// Render the AI-voice controls offscreen:
//   settings         -> the full Settings screen (default tab), proving the
//                       header strip is visible WITHOUT switching tabs
//   voice-tab        -> the Voice tab body on its own
//   command-center   -> the Command Center composer, where the labelled
//                       "AI voice: On/Off" control lives
//
// No backend is reachable on purpose: the voice control is local state, and
// the point of this render is that it is drawn regardless of server state.

let args = CommandLine.arguments
let out = args.count > 1 ? args[1] : "artifacts/voice-controls.png"
let which = args.count > 2 ? args[2] : "settings"

// A bare executable has no bundle id, so UserDefaults.standard persists to
// ~/Library/Preferences/<process-name>.plist. VoiceService writes the
// preference in `speakReplies.didSet`, so a render that turns speech on would
// leave a real key behind in this process's domain — and every harness binary
// here is called `render`, so that key would leak into the other harnesses
// too. Snapshot the key and restore it at exit; never leave test state behind.
let key = "VantageSpeakReplies"
let originalValue = UserDefaults.standard.object(forKey: key)
atexit {
    if let originalValue = originalValue as? Bool {
        UserDefaults.standard.set(originalValue, forKey: key)
    } else {
        UserDefaults.standard.removeObject(forKey: key)
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
// The design is light-first (VantageTheme.swift); the earlier harness forced
// dark, so it never showed what the app actually draws.
let appearance = NSAppearance(named: .aqua)
app.appearance = appearance

let width: CGFloat = CGFloat(
    Double(ProcessInfo.processInfo.environment["VOICE_WIDTH"] ?? "") ?? (which == "command-center" ? 1100 : 882))
let height: CGFloat = CGFloat(
    Double(ProcessInfo.processInfo.environment["VOICE_HEIGHT"] ?? "") ?? (which == "command-center" ? 760 : 562))
let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: width, height: height),
                      styleMask: [.borderless], backing: .buffered, defer: false)
window.appearance = appearance
window.backgroundColor = NSColor(calibratedWhite: 0.98, alpha: 1)
window.isReleasedWhenClosed = false

let root: AnyView
switch which {
case "settings": root = AnyView(SettingsView())
case "voice-tab":
    // Top-aligned: the section body has no spacer of its own (in the real app
    // it lives at the top of a ScrollView), so centering it in the host frame
    // would render it floating mid-window and misrepresent the page.
    root = AnyView(VStack(spacing: 0) {
        VoiceSettings().padding(24).frame(maxWidth: 760)
        Spacer(minLength: 0)
    })
case "command-center": root = AnyView(CommandCenterView())
default:
    FileHandle.standardError.write("unknown view \(which)\n".data(using: .utf8)!)
    exit(2)
}

let host = NSHostingView(rootView: root
    .frame(width: width, height: height)
    .background(V.background)
    .preferredColorScheme(.light))
host.frame = NSRect(x: 0, y: 0, width: width, height: height)
window.contentView = host
window.orderFrontRegardless()

// VOICE_SPEAK=1 starts a real utterance before the render, so the
// "Speaking… / Stop" row is captured while the synthesizer is actually
// playing rather than mocked.
if ProcessInfo.processInfo.environment["VOICE_SPEAK"] == "1" {
    VoiceService.shared.speakReplies = true
    VoiceService.shared.speak(
        "This is a long spoken reply, used to hold the synthesizer in the speaking state "
        + "long enough for the render to capture the speaking row with its stop button.")
}

// Let SwiftUI lay out (and the failed backend reads settle).
let deadline = Date().addingTimeInterval(4)
while Date() < deadline { RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.05)) }

guard let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { exit(1) }
host.cacheDisplay(in: host.bounds, to: rep)
guard let png = rep.representation(using: .png, properties: [:]) else { exit(1) }
try? png.write(to: URL(fileURLWithPath: out))
print("wrote \(out)")
exit(0)