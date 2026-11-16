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

    /// Where each control actually is, in CGEvent screen coordinates: points,
    /// origin at the top-left of the primary display, y growing down. That is
    /// the same space the executor posts events in, so a caller never has to
    /// redo the AppKit conversion and get it subtly wrong.
    var targets: [String: [String: Double]] = [:]

    /// The window the targets live in, set by the delegate once it exists.
    var window: NSWindow?

    private let logPath = "/tmp/vantage_probe_app/state.json"

    func record(_ action: String) {
        lastAction = action
        write()
    }

    /// Convert a SwiftUI `.global` point to CGEvent screen coordinates.
    ///
    /// Done with AppKit's own conversions rather than arithmetic: the window may
    /// have a title bar, may have been moved by the user, and the content view
    /// may not sit at the window origin.
    ///
    /// This is the second version. The first mirrored every target vertically --
    /// measured, it put the first control at y=623.5 and the fifth at y=461 --
    /// because it subtracted the content height from a coordinate that was
    /// already flipped. A click derived from it would have landed outside the
    /// window, and the selftest would have reported a permission failure for what
    /// was really an arithmetic defect.
    func cgPoint(_ global: CGPoint) -> CGPoint {
        guard let win = window, let content = win.contentView else { return global }
        // NSHostingView is a flipped view: its bounds origin is the top-left
        // and y grows downward, exactly like SwiftUI's `.global` space. So the
        // point passes through unchanged. Subtracting the height here (the
        // obvious-looking thing) mirrors every target vertically — measured:
        // it put the first control below the fifth.
        let inWindow = content.convert(global, to: nil as NSView?)
        let onScreen = win.convertPoint(toScreen: inWindow)
        // AppKit screen y grows up; CGEvent y grows down from the primary
        // display's top edge. The primary display is screens[0].
        let primary = NSScreen.screens.first ?? NSScreen.main
        let primaryHeight = primary?.frame.height ?? onScreen.y
        return CGPoint(x: onScreen.x, y: primaryHeight - onScreen.y)
    }

    func reportTarget(_ name: String, _ global: CGRect) {
        let centre = cgPoint(CGPoint(x: global.midX, y: global.midY))
        targets[name] = ["x": (centre.x * 100).rounded() / 100,
                         "y": (centre.y * 100).rounded() / 100]
        write()
    }

    func write() {
        var payload: [String: Any] = [
            "clicks": clicks, "double_clicks": doubleClicks,
            "right_clicks": rightClicks, "checkbox": checkbox,
            "text": text, "scroll_offset": scrollOffset,
            "last_action": lastAction, "targets": targets,
        ]
        // The geometry goes in the file so the conversion is auditable from
        // outside: a reported target can be checked against the window frame
        // instead of being taken on faith.
        if let win = window {
            payload["window_frame_ns"] = [
                "x": win.frame.minX, "y": win.frame.minY,
                "width": win.frame.width, "height": win.frame.height,
            ]
            if let content = win.contentView {
                payload["content_height"] = content.bounds.height
            }
            payload["primary_screen_height"] = NSScreen.screens.first?.frame.height ?? 0
        }
        if let data = try? JSONSerialization.data(withJSONObject: payload, options: [.prettyPrinted]) {
            try? data.write(to: URL(fileURLWithPath: logPath))
        }
    }
}

/// Reports a view's frame in window-global space without drawing anything or
/// taking part in hit testing.
struct TargetReporter: View {
    let name: String
    var body: some View {
        GeometryReader { geo in
            Color.clear.onAppear {
                ProbeState.shared.reportTarget(name, geo.frame(in: .global))
            }
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
                .background(TargetReporter(name: "click"))
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
                    .background(TargetReporter(name: "double"))
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
                    .background(TargetReporter(name: "right"))
                Text("rights: \(state.rightClicks)")
            }

            Toggle("Enable option", isOn: Binding(
                get: { state.checkbox },
                set: { state.checkbox = $0; state.record("checkbox") }))
                .accessibilityIdentifier("probe.checkbox")
                .background(TargetReporter(name: "checkbox"))

            TextField("Type here", text: Binding(
                get: { state.text },
                set: { state.text = $0; state.record("typing") }))
                .textFieldStyle(.roundedBorder)
                .frame(width: 280)
                .accessibilityIdentifier("probe.field")
                .background(TargetReporter(name: "field"))

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
        ProbeState.shared.window = win
        ProbeState.shared.write()
        print("probe window frame (points): \(win.frame)")
        if let content = win.contentView {
            print("content view bounds: \(content.bounds)")
        }
        print("primary screen: \(NSScreen.screens.first?.frame ?? .zero)")
        print("probe pid: \(ProcessInfo.processInfo.processIdentifier)")
    }
}

// Top-level entry point rather than `@main`: this file is named main.swift,
// and Swift rejects the `main` attribute alongside main.swift's implicit
// top-level code.
let probeApp = NSApplication.shared
let probeDelegate = AppDelegate()
probeApp.delegate = probeDelegate
probeApp.setActivationPolicy(.regular)
probeApp.run()