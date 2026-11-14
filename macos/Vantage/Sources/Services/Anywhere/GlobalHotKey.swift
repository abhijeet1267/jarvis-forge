import Foundation
import AppKit
import Carbon

/// A keyboard shortcut the person can change in Settings.
///
/// Stored as a small Codable value so it lives in `UserDefaults` beside the other
/// preferences (VoiceService already uses the same store) and can be shown back in
/// Settings without a second model.
struct ShortcutSpec: Codable, Equatable, Hashable {
    /// The virtual key code (`kVK_*`), which is layout-independent in the way
    /// that matters here: the physical key, not the character it produces.
    var keyCode: UInt32
    /// Carbon modifier mask (`optionKey`, `controlKey`, …).
    var modifiers: UInt32

    /// Option-Space. Checked against the app's existing command shortcuts
    /// (⌘R / ⌘⇧R for the Backend menu): neither uses Option.
    static let defaultShortcut = ShortcutSpec(keyCode: UInt32(kVK_Space), modifiers: UInt32(optionKey))

    var displayString: String {
        var parts = ""
        if modifiers & UInt32(controlKey) != 0 { parts += "⌃" }
        if modifiers & UInt32(optionKey) != 0 { parts += "⌥" }
        if modifiers & UInt32(shiftKey) != 0 { parts += "⇧" }
        if modifiers & UInt32(cmdKey) != 0 { parts += "⌘" }
        return parts + Self.keyName(keyCode)
    }

    /// A name for the common keys; anything else is shown as a symbol so a
    /// non-US layout never renders a blank shortcut.
    static func keyName(_ code: UInt32) -> String {
        switch Int(code) {
        case kVK_Space: return "Space"
        case kVK_Return: return "Return"
        case kVK_Tab: return "Tab"
        case kVK_Escape: return "Esc"
        case kVK_ANSI_A: return "A"; case kVK_ANSI_B: return "B"
        case kVK_ANSI_C: return "C"; case kVK_ANSI_D: return "D"
        case kVK_ANSI_E: return "E"; case kVK_ANSI_F: return "F"
        case kVK_ANSI_G: return "G"; case kVK_ANSI_H: return "H"
        case kVK_ANSI_I: return "I"; case kVK_ANSI_J: return "J"
        case kVK_ANSI_K: return "K"; case kVK_ANSI_L: return "L"
        case kVK_ANSI_M: return "M"; case kVK_ANSI_N: return "N"
        case kVK_ANSI_O: return "O"; case kVK_ANSI_P: return "P"
        case kVK_ANSI_Q: return "Q"; case kVK_ANSI_R: return "R"
        case kVK_ANSI_S: return "S"; case kVK_ANSI_T: return "T"
        case kVK_ANSI_U: return "U"; case kVK_ANSI_V: return "V"
        case kVK_ANSI_W: return "W"; case kVK_ANSI_X: return "X"
        case kVK_ANSI_Y: return "Y"; case kVK_ANSI_Z: return "Z"
        case kVK_ANSI_1: return "1"; case kVK_ANSI_2: return "2"
        case kVK_ANSI_3: return "3"; case kVK_ANSI_4: return "4"
        default: return "key \(code)"
        }
    }
}

/// Registers the global shortcut through Carbon's `RegisterEventHotKey`.
///
/// Why Carbon: it is Apple's documented API for exactly this, it works without
/// Accessibility permission, and it needs no third-party dependency. The
/// alternatives are an `NSEvent` global monitor (needs Accessibility and cannot
/// consume the event) or a keyboard-extension framework (a large dependency).
///
/// Everything about the registration is observable. `registrationError` is set
/// whenever the shortcut is not actually active, so Settings can say so instead
/// of showing a shortcut that does nothing.
@MainActor
final class GlobalHotKey: ObservableObject {

    enum Registration: Equatable {
        case inactive
        case active
        case failed(String)
    }

    @Published private(set) var registration: Registration = .inactive
    @Published private(set) var current: ShortcutSpec
    /// The spec the person asked for, which may differ from a rejected attempt.
    private var installed: ShortcutSpec?

    private var hotKeyRef: EventHotKeyRef?
    private var handlerRef: EventHandlerRef?
    private var onPress: (() -> Void)?
    private let defaultsKey = "vantage.anywhere.shortcut"

    /// One signature for every hotkey this app registers, so an event meant for
    /// a previous registration cannot be mistaken for the current one.
    private static let signature: OSType = 0x564E5447   // 'VNTG'

    init() {
        if let data = UserDefaults.standard.data(forKey: defaultsKey),
           let spec = try? JSONDecoder().decode(ShortcutSpec.self, from: data),
           spec.modifiers != 0 {
            current = spec
        } else {
            current = .defaultShortcut
        }
        installHandlerOnce()
    }

    var isActive: Bool { registration == .active }

    var statusText: String {
        switch registration {
        case .active: return "\(current.displayString) works anywhere on this Mac."
        case .inactive: return "Not registered yet."
        case .failed(let why): return why
        }
    }

    // MARK: - Registration

    /// Install the Carbon handler exactly once for the process lifetime.
    ///
    /// Registering a per-hotkey handler would leave the previous one installed on
    /// every settings change, so the app would run the action N times after N
    /// edits. One handler, one dispatch point, is what prevents that.
    private func installHandlerOnce() {
        guard handlerRef == nil else { return }
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard),
                                 eventKind: UInt32(kEventHotKeyPressed))
        let callback: EventHandlerUPP = { _, event, userData in
            guard let userData else { return noErr }
            let manager = Unmanaged<GlobalHotKey>.fromOpaque(userData).takeUnretainedValue()
            var received = EventHotKeyID()
            let status = GetEventParameter(event, EventParamName(kEventParamDirectObject),
                                           EventParamType(typeEventHotKeyID), nil,
                                           MemoryLayout<EventHotKeyID>.size, nil, &received)
            guard status == noErr, received.id == 1 else { return noErr }
            // Carbon calls this on the main thread, but hop through the main
            // actor explicitly rather than assuming it.
            // Call straight through. Carbon delivers hotkey events on the main
            // thread already; hopping through `DispatchQueue.main.async` only
            // added a way to lose the press. Measured in a bundled probe: the
            // handler was entered with the right id and the queued block never
            // ran, so the shortcut registered successfully and did nothing —
            // exactly the "claims a shortcut is active" failure this class is
            // meant to prevent.
            manager.fire()
            return noErr
        }
        InstallEventHandler(GetApplicationEventTarget(), callback, 1, &spec,
                            Unmanaged.passUnretained(self).toOpaque(), &handlerRef)
    }

    private func fire() {
        onPress?()
    }

    /// Register the current shortcut. Returns whether it is active.
    ///
    /// Re-registering is idempotent: the previous registration is released first,
    /// so pressing Settings repeatedly cannot accumulate hotkeys.
    @discardableResult
    func register(onPress: @escaping () -> Void) -> Bool {
        self.onPress = onPress
        unregister()
        installHandlerOnce()

        var ref: EventHotKeyRef?
        let id = EventHotKeyID(signature: Self.signature, id: 1)
        let status = RegisterEventHotKey(current.keyCode, current.modifiers, id,
                                         GetApplicationEventTarget(), 0, &ref)
        guard status == noErr, let ref else {
            let why = status == OSStatus(eventHotKeyExistsErr)
                ? "\(current.displayString) is already used by another app. Choose another shortcut."
                : "macOS would not register \(current.displayString) (error \(status)). "
                    + "Choose another shortcut; until then, open Vantage Anywhere from the menu bar."
            registration = .failed(why)
            installed = nil
            return false
        }
        hotKeyRef = ref
        installed = current
        registration = .active
        return true
    }

    /// Assign a new shortcut and try it. A rejected shortcut leaves the previous
    /// one working rather than leaving the app with none.
    @discardableResult
    func update(_ spec: ShortcutSpec, onPress: @escaping () -> Void) -> Bool {
        let previous = current
        current = spec
        if register(onPress: onPress) {
            persist()
            return true
        }
        // Restore the working registration.
        let failure = registration
        current = previous
        if register(onPress: onPress) {
            registration = failure          // still report the rejected attempt
        }
        return false
    }

    func unregister() {
        if let ref = hotKeyRef {
            UnregisterEventHotKey(ref)
            hotKeyRef = nil
        }
        installed = nil
        registration = .inactive
    }

    /// Called on application shutdown. Removes both the hotkey and the handler so
    /// a relaunch cannot see two.
    func shutdown() {
        unregister()
        if let handler = handlerRef {
            RemoveEventHandler(handler)
            handlerRef = nil
        }
        onPress = nil
    }

    private func persist() {
        if let data = try? JSONEncoder().encode(current) {
            UserDefaults.standard.set(data, forKey: defaultsKey)
        }
    }
}