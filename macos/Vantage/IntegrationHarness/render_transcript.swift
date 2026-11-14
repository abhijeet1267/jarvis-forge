import AppKit
import SwiftUI

// Render the real transcript rows offscreen with a hand-built conversation.
// The conversation is fed through the public Conversation mutators, so what is
// drawn is what the app draws — not a re-implementation of a bubble.
//
//   render_transcript.swift <out.png> [width] [height]
let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let out = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "artifacts/transcript.png"
let width: CGFloat = CommandLine.arguments.count > 2 ? CGFloat(Double(CommandLine.arguments[2]) ?? 820) : 820
let height: CGFloat = CommandLine.arguments.count > 3 ? CGFloat(Double(CommandLine.arguments[3]) ?? 1000) : 1000

var convo = Conversation()
convo.appendUser("Summarise what changed in the last commit and tell me if anything looks risky.")
convo.appendAssistant(
    "Three files changed. The largest edit is in the planner's rule chain: a new rule for rescheduling work now runs before the file-rename rule, so \"reschedule the report to 5 PM\" is read as a task change rather than a path change. That ordering is the risky part — every widened gate can take a sentence from a rule below it.",
    meta: .init(model: "9router:ag/gemini-2.5-flash", durationMS: 1840, sessionID: "SESS-1"))
convo.appendUser("ok")
convo.appendAssistant("Got it.",
    meta: .init(model: "rules", durationMS: 12))
convo.appendUser("Now delete /tmp/old-build.zip")
convo.appendAssistant("Deleting that file is consequential, so I have held it for your approval.",
    meta: .init(model: "rules", awaitingApproval: true))
convo.appendError("The backend did not answer in time. Your question is still in the transcript; retry when the provider is back.")
convo.appendAssistant("", meta: .init(model: "", durationMS: nil))

let appearance = NSAppearance(named: .aqua)
app.appearance = appearance
let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: width, height: height),
                      styleMask: [.borderless], backing: .buffered, defer: false)
window.appearance = appearance
window.backgroundColor = NSColor(calibratedWhite: 0.07, alpha: 1)
window.isReleasedWhenClosed = false

struct TranscriptProbe: View {
    let conversation: Conversation
    var body: some View {
        VStack(spacing: 0) {
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 14) {
                    ForEach(conversation.messages) { msg in
                        MessageBubble(message: msg)
                    }
                }
                .padding(.horizontal, 20)
                .padding(.vertical, 16)
                .frame(maxWidth: 720, alignment: .leading)
                .frame(maxWidth: .infinity, alignment: .center)
            }
            Divider()
            Text("input bar omitted")
                .font(.caption2).foregroundStyle(.secondary)
                .frame(maxWidth: .infinity)
                .padding(8)
        }
    }
}

let host = NSHostingView(rootView: TranscriptProbe(conversation: convo)
    .frame(width: width, height: height)
    .background(Color(white: 0.07))
    .preferredColorScheme(.light))
host.frame = NSRect(x: 0, y: 0, width: width, height: height)
window.contentView = host
window.orderFrontRegardless()
let deadline = Date().addingTimeInterval(5)
while Date() < deadline { RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.05)) }
guard let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { exit(1) }
host.cacheDisplay(in: host.bounds, to: rep)
guard let png = rep.representation(using: .png, properties: [:]) else { exit(1) }
try? png.write(to: URL(fileURLWithPath: out))
print("wrote \(out)")
exit(0)