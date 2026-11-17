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

    /// Every mouse event this process actually received, newest last.
    ///
    /// Kept separate from `lastAction` on purpose: `lastAction` only changes when
    /// one of this fixture's own callbacks fires, so a frozen `lastAction` cannot
    /// distinguish "no event arrived" from "an event arrived outside every
    /// control". This records what the application saw regardless of where it
    /// landed, which is the only way to find the first stage where a posted
    /// event disappears.
    @Published var eventTrace: [[String: Any]] = []

    /// Raw SwiftUI global frames, for auditing the coordinate conversion.
    var frames: [String: [String: Double]] = [:]

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
        // `.global` inside a SwiftUI view hosted by NSHostingView is measured in
        // that hosting view's own space: origin at the content view's top-left,
        // y growing down. The window server reports screen points with the origin
        // at the primary display's top-left, y growing down. So the mapping is
        // window-top-left + the point, with one vertical flip for AppKit's
        // bottom-up window frame.
        //
        // Measured against this fixture rather than assumed: a button whose
        // `.global` centre was (60, 134.5) in a window whose top sat at screen
        // y = 86 answered real clicks at screen y 210-230, i.e. 86 + 134.5 = 220.5.
        // Two earlier versions of this function were wrong in opposite
        // directions -- one mirrored every target, the other added the title bar
        // twice and put the button 32 points below where it answered.
        guard let win = window, let primary = NSScreen.screens.first ?? NSScreen.main else {
            return global
        }
        let windowTop = primary.frame.height - (win.frame.origin.y + win.frame.height)
        return CGPoint(x: win.frame.origin.x + global.x,
                       y: windowTop + global.y)
    }

    /// The raw `.global` rectangle, before any conversion, kept alongside the
    /// converted target so a coordinate error can be localised to one step.
    func reportFrame(_ name: String, _ global: CGRect) {
        frames[name] = ["x": global.minX, "y": global.minY,
                        "w": global.width, "h": global.height]
        write()
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
            "event_trace": eventTrace,
            "frames": frames,
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
///
/// Attach it with `.background(...)` only where the background is the same
/// rectangle as the interactive area. For a Button it is not: measured on this
/// fixture, the button answered clicks at screen y 210-230 while the background
/// reporter claimed 252.5 -- a 29-point error that made every click miss and
/// land on the text label beneath it.
///
/// `AnchorReporter` (below) is the correct instrument for controls that own
/// their hit area.
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

/// Reports a control's *interactive* frame using an overlay anchor, which
/// SwiftUI sizes to the control itself rather than to a background layer.
struct AnchorReporter: ViewModifier {
    let name: String

    func body(content: Content) -> some View {
        content
            .overlay(GeometryReader { geo in
                Color.clear
                    // The overlay exists only to measure. Without this it sits
                    // on top of the control and swallows the very clicks it is
                    // measuring -- the instrument would break the experiment.
                    .allowsHitTesting(false)
                    .onAppear { ProbeState.shared.reportTarget(name, geo.frame(in: .global)) }
                    .onChange(of: geo.frame(in: .global)) { _, frame in
                        // A window move or resize changes the frame; a reporter
                        // that only fired onAppear would keep publishing the old
                        // geometry, which is the bug this exists to prevent.
                        ProbeState.shared.reportTarget(name, frame)
                    }
            })
    }
}

extension View {
    func reportingTarget(_ name: String) -> some View {
        modifier(AnchorReporter(name: name))
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
                .background(GeometryReader { geo in
                    Color.clear
                        .onAppear { ProbeState.shared.reportFrame("click_background", geo.frame(in: .global)) }
                        .onChange(of: geo.frame(in: .global)) { _, f in
                            ProbeState.shared.reportFrame("click_background", f)
                        }
                })
                .overlay(GeometryReader { geo in
                    Color.clear
                        .onAppear {
                            ProbeState.shared.reportFrame("click_overlay", geo.frame(in: .global))
                            ProbeState.shared.reportTarget("click", geo.frame(in: .global))
                        }
                        .onChange(of: geo.frame(in: .global)) { _, f in
                            ProbeState.shared.reportFrame("click_overlay", f)
                            ProbeState.shared.reportTarget("click", f)
                        }
                })
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
                    .reportingTarget("double")
                Text("doubles: \(state.doubleClicks)")
            }

            HStack(spacing: 12) {
                RightClickTarget()
                    .frame(width: 160, height: 34)
                    .reportingTarget("right")
                    .reportingTarget("context_menu")
                Text("rights: \(state.rightClicks)  menus: \(state.contextMenuOpenings)")
            }

            Toggle("Enable option", isOn: Binding(
                get: { state.checkbox },
                set: { state.checkbox = $0; state.record("checkbox") }))
                .accessibilityIdentifier("probe.checkbox")
                .reportingTarget("checkbox")

            TextField("Type here", text: Binding(
                get: { state.text },
                set: { state.text = $0; state.record("typing") }))
                .textFieldStyle(.roundedBorder)
                .frame(width: 280)
                .accessibilityIdentifier("probe.field")
                .reportingTarget("field")

            Text("field length: \(state.text.count)")

            HStack(spacing: 12) {
                ShortcutField()
                    .frame(width: 300, height: 44)
                    .reportingTarget("shortcut_field")
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
                    .reportingTarget("drag_source")
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
                    .reportingTarget("drop_target")
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
                .reportingTarget("scroller")
        }
        .padding(22)
        .frame(width: 460, height: 640, alignment: .topLeading)
        .onAppear { state.write() }
    }
}

/// Records every mouse event AppKit delivers to this process.
///
/// A local monitor sees events *before* they are dispatched to a view, and
/// returning the event unchanged means it is never consumed — the fixture
/// behaves exactly as it would without the monitor. That matters: an
/// instrument that changed delivery would destroy the thing it is measuring.
enum EventTrace {
    static var monitor: Any?

    nonisolated(unsafe) static var lastCount = 0

    static func install() {
        guard monitor == nil else { return }
        let mask: NSEvent.EventTypeMask = [
            .leftMouseDown, .leftMouseUp, .rightMouseDown, .rightMouseUp,
            .otherMouseDown, .otherMouseUp, .scrollWheel,
        ]
        monitor = NSEvent.addLocalMonitorForEvents(matching: mask) { event in
            record(event)
            return event          // never consume: the fixture must behave normally
        }
    }

    static func record(_ event: NSEvent) {
        let state = ProbeState.shared
        let win = event.window
        // Window-local coordinates, and the same point converted to this
        // fixture's CGEvent screen space, so a sender can be compared directly
        // against what the receiver saw.
        let local = event.locationInWindow
        let screenPoint = win.map { w -> CGPoint in
            let inWindow = w.convertPoint(toScreen: local)
            let primary = NSScreen.screens.first ?? NSScreen.main
            let h = primary?.frame.height ?? inWindow.y
            return CGPoint(x: inWindow.x, y: h - inWindow.y)
        } ?? .zero

        var entry: [String: Any] = [
            "type": describe(event.type),
            "click_count": event.clickCount,
            "timestamp": event.timestamp,
            "window_number": win?.windowNumber ?? 0,
            "window_is_key": win?.isKeyWindow ?? false,
            "app_is_active": NSApp.isActive,
            "local": ["x": local.x, "y": local.y],
            "screen": ["x": screenPoint.x, "y": screenPoint.y],
            "accepts_first_mouse": win.map { _ in true } ?? false,
        ]
        // Which control would receive it, if any. `hitTest` on the content view
        // answers "was this over a control at all", which is exactly the
        // distinction between "arrived in the window" and "arrived on a control".
        if let win = win, let content = win.contentView {
            let inContent = content.convert(local, from: nil)
            if let hit = content.hitTest(inContent) {
                entry["hit_view"] = String(describing: type(of: hit))
            } else {
                entry["hit_view"] = "none"
            }
        }
        state.eventTrace.append(entry)
        if state.eventTrace.count > 60 { state.eventTrace.removeFirst(state.eventTrace.count - 60) }
        state.write()
    }

    static func describe(_ type: NSEvent.EventType) -> String {
        switch type {
        case .leftMouseDown: return "leftMouseDown"
        case .leftMouseUp: return "leftMouseUp"
        case .rightMouseDown: return "rightMouseDown"
        case .rightMouseUp: return "rightMouseUp"
        case .otherMouseDown: return "otherMouseDown"
        case .otherMouseUp: return "otherMouseUp"
        case .scrollWheel: return "scrollWheel"
        default: return "other"
        }
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
        EventTrace.install()
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