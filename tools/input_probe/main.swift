import AppKit
import ApplicationServices
import SwiftUI

// A disposable target for real-input testing. Every control is labelled for
// Accessibility, every effect is observable from outside the process, and the
// window reports its own state through a tiny text file so a test can read what
// actually happened without trusting a claim.
//
// It does nothing on its own: no animation, no timer, no synthetic events. Any
// change recorded here was caused by something outside the process.

final class ProbeState: ObservableObject {
    static let shared = ProbeState()
    @Published var clicks = 0
    @Published var doubleClicks = 0
    @Published var rightClicks = 0
    @Published var checkbox = false
    @Published var text = ""
    @Published var scrollOffset = 0.0
    @Published var lastAction = ""

    private let logPath = "/tmp/vantage_probe_app/state.json"

    func record(_ action: String) {
        lastAction = action
        write()
    }

    func write() {
        let payload: [String: Any] = [
            "clicks": clicks, "double_clicks": doubleClicks,
            "right_clicks": rightClicks, "checkbox": checkbox,
            "text": text, "scroll_offset": scrollOffset,
            "last_action": lastAction,
        ]
        if let data = try? JSONSerialization.data(withJSONObject: payload, options: [.prettyPrinted]) {
            try? data.write(to: URL(fileURLWithPath: logPath))
        }
    }
}

struct ProbeView: View {
    @ObservedObject var state = ProbeState.shared

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Vantage Input Probe").font(.title2).bold()
            Text("A disposable target for real mouse and keyboard testing.")
                .font(.caption).foregroundColor(.secondary)

            HStack(spacing: 12) {
                Button("Click me") {
                    state.clicks += 1
                    state.record("click")
                }
                .accessibilityIdentifier("probe.click")
                .accessibilityLabel("Click me")
                Text("clicks: \(state.clicks)").accessibilityIdentifier("probe.click.count")
                    .accessibilityLabel("click count \(state.clicks)")
            }

            HStack(spacing: 12) {
                Text("Double-click target")
                    .padding(8)
                    .background(Color.gray.opacity(0.2))
                    .cornerRadius(6)
                    .onTapGesture(count: 2) {
                        state.doubleClicks += 1
                        state.record("double_click")
                    }
                    .accessibilityIdentifier("probe.double")
                    .accessibilityLabel("Double-click target")
                Text("doubles: \(state.doubleClicks)")
            }

            HStack(spacing: 12) {
                Text("Right-click target")
                    .padding(8)
                    .background(Color.gray.opacity(0.2))
                    .cornerRadius(6)
                    .contextMenu {
                        Button("Probe menu item") {
                            state.rightClicks += 1
                            state.record("right_click_menu")
                        }
                    }
                    .accessibilityIdentifier("probe.right")
                    .accessibilityLabel("Right-click target")
                Text("rights: \(state.rightClicks)")
            }

            Toggle("Enable option", isOn: Binding(
                get: { state.checkbox },
                set: { state.checkbox = $0; state.record("checkbox") }))
                .accessibilityIdentifier("probe.checkbox")

            TextField("Type here", text: Binding(
                get: { state.text },
                set: { state.text = $0; state.record("typing") }))
                .textFieldStyle(.roundedBorder)
                .frame(width: 280)
                .accessibilityIdentifier("probe.field")

            Text("field length: \(state.text.count)")

            ScrollView {
                VStack(alignment: .leading) {
                    ForEach(0..<60, id: \.self) { i in
                        Text("Scrollable row \(i)").font(.system(size: 12))
                            .accessibilityIdentifier("probe.row.\(i)")
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .frame(height: 90)
            .border(Color.gray.opacity(0.4))
            .accessibilityIdentifier("probe.scroller")
        }
        .padding(22)
        .frame(width: 460, height: 640, alignment: .topLeading)
        .onAppear { state.write() }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    var window: NSWindow!
    func applicationDidFinishLaunching(_ note: Notification) {
        _ = ProbeState.shared
        let win = NSWindow(contentRect: NSRect(x: 120, y: 120, width: 460, height: 640),
                           styleMask: [.titled, .closable, .miniaturizable],
                           backing: .buffered, defer: false)
        win.title = "Vantage Input Probe"
        win.contentView = NSHostingView(rootView: ProbeView())
        win.isReleasedWhenClosed = false
        win.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        window = win
        ProbeState.shared.write()
        print("probe window frame (points): \(win.frame)")
        print("probe pid: \(ProcessInfo.processInfo.processIdentifier)")
    }
}

@main
struct ProbeMain {
    static func main() {
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate
        app.setActivationPolicy(.regular)
        app.run()
    }
}