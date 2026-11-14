import SwiftUI
import AppKit
import Carbon

/// Captures a new global shortcut from the keyboard.
///
/// A small `NSView` behind the SwiftUI surface rather than a full shortcut
/// framework: the app already uses Carbon for the registration, so this only has
/// to turn a key press into `(keyCode, modifiers)` and report it. Escape cancels
/// without changing anything.
///
/// It refuses a modifier-less shortcut for the same reason the global API would:
/// a bare key would swallow that letter everywhere on the Mac.
struct ShortcutRecorder: View {

    let current: ShortcutSpec
    let onCapture: (ShortcutSpec) -> Void

    @State private var recording = false
    @State private var error: String?

    var body: some View {
        VStack(alignment: .trailing, spacing: V.Space.xxs) {
            Button {
                error = nil
                recording.toggle()
            } label: {
                Text(recording ? "Press keys…" : current.displayString)
                    .font(V.Font.mono)
                    .foregroundStyle(recording ? V.accent : V.text)
                    .frame(minWidth: 84)
                    .padding(.horizontal, V.Space.s)
                    .padding(.vertical, 5)
                    .background(recording ? V.accentSoft : V.surface,
                                in: RoundedRectangle(cornerRadius: V.Radius.control))
                    .overlay(
                        RoundedRectangle(cornerRadius: V.Radius.control)
                            .stroke(recording ? V.accent : V.border, lineWidth: 1))
            }
            .buttonStyle(.plain)
            .help("Click, then press the key combination you want")

            if recording {
                KeyCaptureView { spec in
                    recording = false
                    if spec.modifiers == 0 {
                        error = "A shortcut needs at least one modifier (⌥, ⌃, ⇧ or ⌘)."
                        return
                    }
                    onCapture(spec)
                } onCancel: {
                    recording = false
                }
                .frame(width: 0, height: 0)
            }
            if let error {
                Text(error).font(V.Font.caption).foregroundStyle(V.danger)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}

/// The AppKit view that actually receives the key press while recording.
private struct KeyCaptureView: NSViewRepresentable {
    let onCapture: (ShortcutSpec) -> Void
    let onCancel: () -> Void

    func makeNSView(context: Context) -> KeyView {
        let view = KeyView()
        view.onCapture = onCapture
        view.onCancel = onCancel
        DispatchQueue.main.async { view.window?.makeFirstResponder(view) }
        return view
    }

    func updateNSView(_ nsView: KeyView, context: Context) {
        nsView.onCapture = onCapture
        nsView.onCancel = onCancel
        DispatchQueue.main.async { nsView.window?.makeFirstResponder(nsView) }
    }

    final class KeyView: NSView {
        var onCapture: ((ShortcutSpec) -> Void)?
        var onCancel: (() -> Void)?

        override var acceptsFirstResponder: Bool { true }

        override func keyDown(with event: NSEvent) {
            // Escape cancels; a bare modifier is still being held, not a shortcut.
            if event.keyCode == 53 { onCancel?(); return }
            var modifiers: UInt32 = 0
            if event.modifierFlags.contains(.control) { modifiers |= UInt32(controlKey) }
            if event.modifierFlags.contains(.option) { modifiers |= UInt32(optionKey) }
            if event.modifierFlags.contains(.shift) { modifiers |= UInt32(shiftKey) }
            if event.modifierFlags.contains(.command) { modifiers |= UInt32(cmdKey) }
            onCapture?(ShortcutSpec(keyCode: UInt32(event.keyCode), modifiers: modifiers))
        }

        // A bare modifier press should not be captured as "no shortcut".
        override func flagsChanged(with event: NSEvent) {}
    }
}
