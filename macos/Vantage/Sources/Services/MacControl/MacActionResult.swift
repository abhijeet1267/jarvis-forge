import Foundation

/// What one Mac-control operation produced.
///
/// One type for every outcome, with the verification state explicit. The rule
/// the rest of Vantage learned the hard way applies here: a call returning is
/// not the world changing, so an action that could not be read back says
/// `unverified` instead of `succeeded`.
struct MacActionResult: Equatable {
    enum Status: String, Equatable {
        /// Performed, and read back from the system.
        case verified
        /// Performed, but the result could not be observed. Not a success claim.
        case unverified
        /// Refused before or during execution; nothing (further) was changed.
        case refused
        /// Attempted and the system said no.
        case failed
        case cancelled
    }

    let action: MacActionKind
    let status: Status
    /// One sentence for the person. For a refusal this is the reason.
    let message: String
    /// Named facts the UI can show: which app answered, which window, which
    /// path, the measured value. Values only — never document contents.
    var details: [String: String] = [:]
    /// How long the native call took, for the history row.
    var durationMS: Int = 0

    var succeeded: Bool { status == .verified }

    var symbol: String {
        switch status {
        case .verified: return "checkmark.circle.fill"
        case .unverified: return "questionmark.circle.fill"
        case .refused: return "hand.raised.fill"
        case .failed: return "xmark.octagon.fill"
        case .cancelled: return "slash.circle.fill"
        }
    }

    static func verified(_ kind: MacActionKind, _ message: String,
                         details: [String: String] = [:]) -> MacActionResult {
        MacActionResult(action: kind, status: .verified, message: message, details: details)
    }

    static func unverified(_ kind: MacActionKind, _ message: String,
                           details: [String: String] = [:]) -> MacActionResult {
        MacActionResult(action: kind, status: .unverified, message: message, details: details)
    }

    static func failed(_ kind: MacActionKind, _ message: String,
                       details: [String: String] = [:]) -> MacActionResult {
        MacActionResult(action: kind, status: .failed, message: message, details: details)
    }

    static func refused(_ kind: MacActionKind, _ refusal: MacControlRefusal) -> MacActionResult {
        MacActionResult(action: kind, status: .refused, message: refusal.message)
    }

    static func cancelled(_ kind: MacActionKind) -> MacActionResult {
        MacActionResult(action: kind, status: .cancelled,
                        message: MacControlRefusal.cancelled.message)
    }
}

/// A validated, ready-to-run action — the only thing the executor accepts.
///
/// Producing one is `MacActionRouter`'s job, and it is the gate that makes the
/// rest of the design safe: by the time this type exists, the action kind is
/// known, every required argument is present, the risk class is fixed, and the
/// permission requirement has been checked. An executor arm therefore never has
/// to decide whether what it was handed is allowed.
struct MacActionRequest: Equatable {
    let kind: MacActionKind
    let arguments: MacActionArguments
    let policy: MacActionPolicy
    /// True only when the person pressed the button (or answered the
    /// confirmation). A backend/phone/model-originated request is never this.
    let userInitiated: Bool
    /// The person already saw exactly what would happen and said yes.
    let confirmed: Bool

    var risk: MacRiskClass { policy.risk }
    var requiresPermission: MacPermission? { policy.permission }
}

/// The permission situation on this Mac, as observed — never assumed.
struct MacPermissionStatus: Equatable {
    /// `nil` means "could not be determined", which is not the same as denied.
    var accessibilityGranted: Bool?
    /// Automation is per-target and is only known once an event is sent, so the
    /// honest state is "unknown until used".
    var automationNote: String = ""

    var accessibilityLabel: String {
        switch accessibilityGranted {
        case .some(true): return "Granted"
        case .some(false): return "Not granted"
        case .none: return "Unknown"
        }
    }

    /// Whether an action may be attempted. Unknown is treated as *not* granted:
    /// the native call will fail closed and report it, which is better than
    /// claiming a capability the process does not have.
    func permits(_ permission: MacPermission?) -> Bool {
        switch permission {
        case .none: return true
        case .accessibility: return accessibilityGranted == true
        case .automation: return true   // discovered by attempting a scoped event
        // Notification authorisation is per-app and is asked for at the point
        // of use (the executor reports a refusal with the settings path); the
        // router must not block the attempt before that question is asked.
        case .notifications: return true
        }
    }
}

/// One executed action, kept for the visible history.
///
/// Deliberately narrow: action id, timestamp, target identifier, risk class and
/// outcome. No document contents, no typed text, no full paths to files that
/// were not the action's target, and nothing secret.
struct MacHistoryEntry: Identifiable, Equatable {
    let id: UUID
    let at: Date
    let action: MacActionKind
    let target: String
    let risk: MacRiskClass
    let status: MacActionResult.Status
    let message: String
    let durationMS: Int

    init(result: MacActionResult, target: String) {
        self.id = UUID()
        self.at = Date()
        self.action = result.action
        self.target = target
        self.risk = MacActionPolicy.forKind(result.action)?.risk ?? .read
        self.status = result.status
        self.message = result.message
        self.durationMS = result.durationMS
    }
}