import SwiftUI
import AppKit

/// Owns the Vantage Anywhere panel: one window, one shortcut, one execution path.
///
/// Lifecycle rules this class exists to enforce:
/// * the panel is created once and reused, so repeated shortcut presses cannot
///   accumulate windows;
/// * the Carbon registration happens once, in `start`, and is released in `stop`;
/// * opening focuses the field without activating a second app instance, and
///   closing restores whatever was in front before.
@MainActor
final class AnywhereController: ObservableObject {

    static let shared = AnywhereController()

    @Published var isPresented = false
    @Published private(set) var recent: [String] = []
    /// A request the palette handed to a full-window screen (a document question,
    /// say). Held until that screen takes it, so the palette never answers
    /// something that needs the screen's context.
    @Published var pendingPrefill: String?

    let hotKey = GlobalHotKey()

    private var panel: AnywherePanel?
    private var previousApp: NSRunningApplication?
    private var started = false
    private let recentKey = "vantage.anywhere.recent"

    /// The palette talks to the same coordinator the screens do.
    private var coordinator: MacControlCoordinator { .shared }
    /// Lazily built so the palette never asks the filesystem for applications
    /// before it is first opened.
    private var resolver: AnywhereCommand?

    var flows: FlowStore { FlowStore.shared }

    private init() {
        if let stored = UserDefaults.standard.stringArray(forKey: recentKey) {
            recent = stored
        }
    }

    // MARK: - Lifecycle

    /// Called once from the app delegate after launch.
    func start() {
        guard !started else { return }
        started = true
        hotKey.register { [weak self] in self?.toggle() }
    }

    /// Called once on quit. Releases the hotkey and the Carbon handler so a
    /// relaunch cannot see two of either.
    func stop() {
        hotKey.shutdown()
        panel?.orderOut(nil)
        panel = nil
        started = false
    }

    @discardableResult
    func updateShortcut(_ spec: ShortcutSpec) -> Bool {
        hotKey.update(spec) { [weak self] in self?.toggle() }
    }

    // MARK: - Showing

    func toggle() {
        isPresented ? dismiss() : show()
    }

    func show() {
        // Remember what was in front so dismissing returns the person to it.
        if let front = NSWorkspace.shared.frontmostApplication,
           front.bundleIdentifier != Bundle.main.bundleIdentifier {
            previousApp = front
        }
        let panel = existingOrNewPanel()
        // Accessory so the palette can take keys without becoming a "real" app
        // window that shows up in the Dock or Cmd-Tab.
        NSApp.setActivationPolicy(.accessory)
        NSApp.activate(ignoringOtherApps: true)
        panel.makeKeyAndOrderFront(nil)
        isPresented = true
        NotificationCenter.default.post(name: .anywhereDidOpen, object: nil)
    }

    func dismiss() {
        panel?.orderOut(nil)
        isPresented = false
        // Hand focus back where it came from. If the previous app has gone, fall
        // back to letting macOS decide rather than activating nothing.
        if let previous = previousApp, !previous.isTerminated {
            previous.activate(options: [])
        }
        previousApp = nil
        NSApp.setActivationPolicy(.regular)
        NotificationCenter.default.post(name: .anywhereDidClose, object: nil)
    }

    /// Open the full window for anything the palette hands off (approvals, the
    /// flow editor, conversation). Deliberately the same app instance.
    func openMainWindow() {
        dismiss()
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
        for window in NSApp.windows where window.canBecomeMain && window !== panel {
            window.makeKeyAndOrderFront(nil)
            return
        }
        // No window exists (it was closed): ask SwiftUI to build one.
        NSApp.sendAction(#selector(NSApplication.newWindowForTab(_:)), to: nil, from: nil)
    }

    private func existingOrNewPanel() -> AnywherePanel {
        if let panel { return panel }
        let panel = AnywherePanel()
        panel.contentView = NSHostingView(rootView: AnywherePaletteView(controller: self))
        self.panel = panel
        return panel
    }

    // MARK: - Resolving

    func resolverInstance() -> AnywhereCommand {
        if let resolver { return resolver }
        let built = AnywhereCommand(
            applications: MacControlCoordinator.shared.installedApplications,
            store: flows,
            capture: CaptureRuntime.shared.store,
            openDocuments: { DocumentRuntime.shared.openDocumentNames })
        resolver = built
        return built
    }

    func remember(_ text: String) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        recent.removeAll { $0.caseInsensitiveCompare(trimmed) == .orderedSame }
        recent.insert(trimmed, at: 0)
        if recent.count > 8 { recent.removeLast(recent.count - 8) }
        UserDefaults.standard.set(recent, forKey: recentKey)
    }
}

extension Notification.Name {
    static let anywhereDidOpen = Notification.Name("vantage.anywhere.open")
    static let anywhereDidClose = Notification.Name("vantage.anywhere.close")
}

/// A borderless, non-activating panel that can still take keyboard focus.
///
/// `NSPanel` with `.nonactivatingPanel` would refuse key status for typing, so
/// this uses a normal panel that hides its title bar and closes on Escape. It is
/// not `.floating` permanently — floating would sit over every app after it is
/// dismissed.
final class AnywherePanel: NSPanel {
    init() {
        super.init(contentRect: NSRect(x: 0, y: 0, width: 620, height: 420),
                   styleMask: [.titled, .fullSizeContentView, .closable],
                   backing: .buffered, defer: false)
        titleVisibility = .hidden
        titlebarAppearsTransparent = true
        isMovableByWindowBackground = true
        standardWindowButton(.closeButton)?.isHidden = true
        standardWindowButton(.miniaturizeButton)?.isHidden = true
        standardWindowButton(.zoomButton)?.isHidden = true
        level = .floating
        collectionBehavior = [.transient, .moveToActiveSpace, .fullScreenAuxiliary]
        hidesOnDeactivate = false
        isReleasedWhenClosed = false
        backgroundColor = .clear
        isOpaque = false
        hasShadow = true
    }

    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }

    /// Centre it near the top of the screen the person is looking at, which is
    /// where a command palette is expected.
    override func center() {
        guard let screen = NSScreen.main else { return super.center() }
        let visible = screen.visibleFrame
        let size = frame.size
        setFrameOrigin(NSPoint(x: visible.midX - size.width / 2,
                               y: visible.maxY - size.height - 120))
    }
}