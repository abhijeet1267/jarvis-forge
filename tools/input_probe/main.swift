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
    /// Times the context menu was actually shown, observed from the NSMenu
    /// delegate. `rightClicks` still means "the menu item was chosen".
    @Published var contextMenuOpenings = 0
    /// Live selection in the shortcut field, in UTF-16 units, read from the
    /// NSTextView itself. `cmd+a` is proven by this reaching fieldTextLength.
    @Published var selectionLength = 0
    @Published var fieldTextLength = 0
    /// What the drop target actually received from a real drag.
    @Published var droppedItem = ""
    @Published var dropCount = 0

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

    /// A context menu opened. Called from NSMenuDelegate.menuWillOpen, not from
    /// any guess about what a posted event should have done.
    func observeContextMenuOpen() {
        contextMenuOpenings += 1
        record("context_menu_open")
    }

    /// The shortcut field's real selection changed (NSTextView delegate).
    func updateShortcut(selectionLength: Int, textLength: Int) {
        self.selectionLength = selectionLength
        self.fieldTextLength = textLength
        write()
    }

    /// The scroll view's clip bounds moved (boundsDidChange notification).
    func updateScroll(_ originY: CGFloat) {
        scrollOffset = (Double(originY) * 10).rounded() / 10
        write()
    }

    func write() {
        var payload: [String: Any] = [
            "clicks": clicks, "double_clicks": doubleClicks,
            "right_clicks": rightClicks, "checkbox": checkbox,
            "text": text, "scroll_offset": scrollOffset,
            "last_action": lastAction, "targets": targets,
            "context_menu_openings": contextMenuOpenings,
            "selection_length": selectionLength,
            "field_text_length": fieldTextLength,
            "dropped_item": droppedItem, "drop_count": dropCount,
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

/// The right-click target as a real NSView, so the NSMenu's own delegate can
/// tell us the menu was shown. SwiftUI's `.contextMenu` gives no access to the
/// NSMenu, and inferring "a menu must have opened" from the click would be a
/// guess, not evidence.
final class RightClickNSView: NSView {
    override var isFlipped: Bool { true }

    override func rightMouseDown(with event: NSEvent) {
        guard let menu = menu else { return super.rightMouseDown(with: event) }
        // Blocks for the menu's tracking session, which is exactly the session
        // whose opening menuWillOpen reports.
        NSMenu.popUpContextMenu(menu, with: event, for: self)
    }

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    override func draw(_ dirtyRect: NSRect) {
        NSColor.gray.withAlphaComponent(0.2).setFill()
        NSBezierPath(roundedRect: bounds, xRadius: 6, yRadius: 6).fill()
        let text = "Right-click target" as NSString
        let attrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: 13),
            .foregroundColor: NSColor.labelColor,
        ]
        let size = text.size(withAttributes: attrs)
        text.draw(at: NSPoint(x: (bounds.width - size.width) / 2,
                              y: (bounds.height - size.height) / 2),
                  withAttributes: attrs)
    }
}

struct RightClickTarget: NSViewRepresentable {
    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeNSView(context: Context) -> RightClickNSView {
        let view = RightClickNSView()
        let menu = NSMenu()
        let item = NSMenuItem(title: "Probe menu item",
                              action: #selector(Coordinator.chooseMenuItem),
                              keyEquivalent: "")
        item.target = context.coordinator
        menu.addItem(item)
        menu.delegate = context.coordinator
        view.menu = menu
        view.setAccessibilityElement(true)
        view.setAccessibilityRole(.button)
        view.setAccessibilityIdentifier("probe.right")
        view.setAccessibilityLabel("Right-click target")
        return view
    }

    func updateNSView(_ nsView: RightClickNSView, context: Context) {}

    final class Coordinator: NSObject, NSMenuDelegate {
        @objc func chooseMenuItem() {
            ProbeState.shared.rightClicks += 1
            ProbeState.shared.record("right_click_menu")
        }

        func menuWillOpen(_ menu: NSMenu) {
            ProbeState.shared.observeContextMenuOpen()
        }
    }
}

/// Text view whose selection and length are read straight from the NSTextView,
/// so `cmd+a` can be proven by reading the whole field back as selected.
struct ShortcutField: NSViewRepresentable {
    static let initialText = "Select all of this text"

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeNSView(context: Context) -> NSScrollView {
        let textView = NSTextView(frame: NSRect(x: 0, y: 0, width: 300, height: 44))
        textView.isRichText = false
        textView.font = .monospacedSystemFont(ofSize: 12, weight: .regular)
        textView.string = ShortcutField.initialText
        textView.isVerticallyResizable = true
        textView.isHorizontallyResizable = false
        textView.autoresizingMask = [.width]
        textView.textContainer?.widthTracksTextView = true
        textView.textContainer?.containerSize = NSSize(width: 300, height: CGFloat.greatestFiniteMagnitude)
        textView.setAccessibilityIdentifier("probe.shortcut")
        textView.setAccessibilityLabel("Shortcut field")
        textView.delegate = context.coordinator

        let scroll = NSScrollView(frame: NSRect(x: 0, y: 0, width: 300, height: 44))
        scroll.hasVerticalScroller = true
        scroll.borderType = .bezelBorder
        scroll.documentView = textView

        context.coordinator.textView = textView
        context.coordinator.publish()
        return scroll
    }

    func updateNSView(_ nsView: NSScrollView, context: Context) {}

    final class Coordinator: NSObject, NSTextViewDelegate {
        weak var textView: NSTextView?

        func publish() {
            guard let textView = textView else { return }
            ProbeState.shared.updateShortcut(
                selectionLength: textView.selectedRange().length,
                textLength: (textView.string as NSString).length)
        }

        func textViewDidChangeSelection(_ notification: Notification) { publish() }
        func textViewDidChange(_ notification: Notification) { publish() }
    }
}

/// A flipped document view so the clip view's bounds.origin.y starts at 0 and
/// grows as the content is scrolled down.
final class FlippedView: NSView {
    override var isFlipped: Bool { true }
}

/// The scrollable list as an NSScrollView, so `scroll_offset` is the clip
/// view's real bounds origin reported by boundsDidChange. A wheel or a drag from
/// outside moves it; nothing here scrolls itself.
struct ScrollProbe: NSViewRepresentable {
    final class Coordinator {
        var observer: NSObjectProtocol?
    }

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeNSView(context: Context) -> NSScrollView {
        let rowHeight: CGFloat = 18
        let scroll = NSScrollView(frame: NSRect(x: 0, y: 0, width: 400, height: 90))
        scroll.hasVerticalScroller = true
        scroll.borderType = .bezelBorder
        scroll.setAccessibilityElement(true)
        scroll.setAccessibilityRole(.scrollArea)
        scroll.setAccessibilityIdentifier("probe.scroller")
        scroll.setAccessibilityLabel("Scroller")

        let document = FlippedView(frame: NSRect(x: 0, y: 0, width: 400,
                                                 height: rowHeight * 60 + 4))
        for i in 0..<60 {
            let label = NSTextField(labelWithString: "Scrollable row \(i)")
            label.font = .systemFont(ofSize: 12)
            label.frame = NSRect(x: 6, y: 2 + CGFloat(i) * rowHeight, width: 380, height: 16)
            label.setAccessibilityIdentifier("probe.row.\(i)")
            document.addSubview(label)
        }
        scroll.documentView = document

        scroll.contentView.postsBoundsChangedNotifications = true
        context.coordinator.observer = NotificationCenter.default.addObserver(
            forName: NSView.boundsDidChangeNotification,
            object: scroll.contentView,
            queue: .main) { _ in
                ProbeState.shared.updateScroll(scroll.contentView.bounds.origin.y)
            }
        // Publish the real initial value rather than leaving a default in place
        // as if it had been read.
        ProbeState.shared.updateScroll(scroll.contentView.bounds.origin.y)
        return scroll
    }

    func updateNSView(_ nsView: NSScrollView, context: Context) {}

    static func dismantleNSView(_ nsView: NSScrollView, coordinator: Coordinator) {
        if let observer = coordinator.observer {
            NotificationCenter.default.removeObserver(observer)
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
                RightClickTarget()
                    .frame(width: 160, height: 34)
                    .background(TargetReporter(name: "right"))
                    .background(TargetReporter(name: "context_menu"))
                Text("rights: \(state.rightClicks)  menus: \(state.contextMenuOpenings)")
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

            HStack(spacing: 12) {
                ShortcutField()
                    .frame(width: 300, height: 44)
                    .background(TargetReporter(name: "shortcut_field"))
                Text("selected \(state.selectionLength) of \(state.fieldTextLength)")
                    .font(.caption)
            }

            HStack(spacing: 12) {
                Text("Drag source")
                    .padding(8)
                    .background(Color.gray.opacity(0.2))
                    .cornerRadius(6)
                    .accessibilityIdentifier("probe.dragsource")
                    .accessibilityLabel("Drag source")
                    .background(TargetReporter(name: "drag_source"))
                    .onDrag {
                        // Real mouse-down/drag/up is required to start this;
                        // nothing here initiates a drag.
                        NSItemProvider(object: "vantage-drag-payload" as NSString)
                    }
                Text(state.droppedItem.isEmpty
                     ? "Drop target"
                     : "Dropped: \(state.droppedItem) (\(state.dropCount))")
                    .padding(8)
                    .frame(minWidth: 180, minHeight: 34)
                    .background(Color.gray.opacity(0.2))
                    .cornerRadius(6)
                    .accessibilityIdentifier("probe.droptarget")
                    .accessibilityLabel("Drop target")
                    .background(TargetReporter(name: "drop_target"))
                    .onDrop(of: [.utf8PlainText, .plainText], isTargeted: nil) { providers in
                        guard let provider = providers.first else { return false }
                        _ = provider.loadObject(ofClass: NSString.self) { object, _ in
                            guard let value = object as? String else { return }
                            DispatchQueue.main.async {
                                state.droppedItem = value
                                state.dropCount += 1
                                state.record("drop")
                            }
                        }
                        return true
                    }
            }

            ScrollProbe()
                .frame(height: 90)
                .background(TargetReporter(name: "scroller"))
        }
        .padding(22)
        .frame(width: 460, height: 640, alignment: .topLeading)
        .onAppear { state.write() }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    var window: NSWindow!

    /// A minimal Edit menu so cmd+a is a real application shortcut routed to
    /// the first responder (the shortcut field's NSTextView), instead of
    /// relying on NSTextView's internal key handling.
    private func installMainMenu() {
        let mainMenu = NSMenu()
        let appItem = NSMenuItem()
        mainMenu.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "Quit Vantage Input Probe",
                        action: #selector(NSApplication.terminate(_:)),
                        keyEquivalent: "q")
        appItem.submenu = appMenu
        let editItem = NSMenuItem()
        mainMenu.addItem(editItem)
        let editMenu = NSMenu(title: "Edit")
        editMenu.addItem(withTitle: "Select All",
                         action: #selector(NSText.selectAll(_:)),
                         keyEquivalent: "a")
        editItem.submenu = editMenu
        NSApp.mainMenu = mainMenu
    }

    func applicationDidFinishLaunching(_ note: Notification) {
        _ = ProbeState.shared
        installMainMenu()
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