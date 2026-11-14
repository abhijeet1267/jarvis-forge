import Foundation
import ApplicationServices

/// Reads the Mac's permission state for this process.
///
/// Every answer is observed, never assumed. Accessibility is a direct query to
/// the OS; Automation is per-target and cannot be pre-flighted, so it is
/// reported as unknown until an event is attempted (and the executor converts
/// the OS refusal into a named permission error rather than a generic failure).
enum MacPermissionInspector {
    static func current() -> MacPermissionStatus {
        MacPermissionStatus(
            accessibilityGranted: AXIsProcessTrusted(),
            automationNote: "Automation is granted per target application. Vantage asks macOS "
                + "the first time it sends an event; the prompt appears then."
        )
    }

    /// Ask macOS to show the Accessibility consent prompt for this app, once.
    ///
    /// `AXIsProcessTrustedWithOptions` with the prompt option is the documented
    /// call. It does not grant anything — the person still flips the switch — so
    /// the result is only "the request was made", and the caller must recheck.
    @discardableResult
    static func requestAccessibilityPrompt() -> Bool {
        let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true]
        return AXIsProcessTrustedWithOptions(options as CFDictionary)
    }
}