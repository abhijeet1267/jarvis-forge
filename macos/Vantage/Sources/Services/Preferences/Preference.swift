import Foundation

/// One preference the person set, on purpose.
///
/// The brief is pointed: this is not an autonomous memory. Every kind here is
/// something a UI can ask for by name ("Preferred browser"), and the only way an
/// entry appears is a person adding it — or approving a proposal that a screen
/// shows first.
struct Preference: Codable, Identifiable, Equatable {

    /// What the preference is about. A closed set: a new kind needs a field in the
    /// UI and a use site, and an open string would let a preference exist that
    /// nothing reads.
    enum Kind: String, Codable, CaseIterable {
        case preferredApplication
        case preferredFolder
        case fileNaming
        case summaryFormat
        case briefingSection
        case defaultFlowPolicy

        var displayName: String {
            switch self {
            case .preferredApplication: return "Preferred application"
            case .preferredFolder: return "Preferred folder"
            case .fileNaming: return "File naming"
            case .summaryFormat: return "Summary format"
            case .briefingSection: return "Briefing section"
            case .defaultFlowPolicy: return "New flow default"
            }
        }

        /// What the value means, in the UI's own words.
        var valueHint: String {
            switch self {
            case .preferredApplication:
                return "The app Vantage should open when you ask for “my editor”, “my browser” — "
                    + "matched by the key you give it."
            case .preferredFolder:
                return "A folder for saved summaries and exports. An absolute path or a "
                    + "well-known name (Downloads, Documents, Desktop)."
            case .fileNaming:
                return "A pattern for generated file names. {name} and {date} are replaced."
            case .summaryFormat:
                return "How summaries are shaped: “brief”, “bullets”, or “detailed”."
            case .briefingSection:
                return "A section to show first in the Daily Briefing: schedule, due, overdue, "
                    + "upcoming, capture, failures, approvals."
            case .defaultFlowPolicy:
                return "Defaults for new flow drafts: “stop” or “continue”."
            }
        }

        var symbol: String {
            switch self {
            case .preferredApplication: return "app"
            case .preferredFolder: return "folder"
            case .fileNaming: return "textformat"
            case .summaryFormat: return "text.justify.left"
            case .briefingSection: return "calendar.day.timeline.left"
            case .defaultFlowPolicy: return "square.stack.3d.up"
            }
        }

        /// Value validation. A preference the app cannot honour is refused at
        /// entry rather than silently ignored at use time.
        func validate(_ value: String) -> String? {
            let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !trimmed.isEmpty else { return "A preference needs a value." }
            switch self {
            case .preferredApplication:
                guard trimmed.count <= 80 else { return "That name is too long." }
                return nil
            case .preferredFolder:
                let expanded = (trimmed as NSString).expandingTildeInPath
                guard expanded.hasPrefix("/") || MacPathGuard.namedFolders[trimmed.lowercased()] != nil
                else {
                    return "Use an absolute path (starting with /) or a well-known folder name."
                }
                if let url = try? MacPathGuard.resolve(trimmed),
                   let refusal = MacPathGuard.refusal(for: url) {
                    return refusal.message
                }
                return nil
            case .fileNaming:
                guard !trimmed.contains("/") && !trimmed.contains(":") else {
                    return "A file name pattern cannot contain a path separator."
                }
                return nil
            case .summaryFormat:
                let allowed = ["brief", "bullets", "detailed"]
                guard allowed.contains(trimmed.lowercased()) else {
                    return "Use one of: \(allowed.joined(separator: ", "))."
                }
                return nil
            case .briefingSection:
                let allowed = ["schedule", "due", "overdue", "upcoming", "capture",
                               "failures", "approvals"]
                guard allowed.contains(trimmed.lowercased()) else {
                    return "Use one of: \(allowed.joined(separator: ", "))."
                }
                return nil
            case .defaultFlowPolicy:
                guard ["stop", "continue"].contains(trimmed.lowercased()) else {
                    return "Use “stop” or “continue”."
                }
                return nil
            }
        }
    }

    let id: UUID
    var kind: Kind
    /// The key inside the kind — "browser", "summaries", "editor". Lower-cased
    /// for matching, shown as typed.
    var key: String
    var value: String
    var createdAt: Date
    var modifiedAt: Date
    /// False when the person turned it off without deleting it, so a disabled
    /// preference can be re-enabled without retyping it.
    var enabled: Bool
    var note: String

    init(id: UUID = UUID(), kind: Kind, key: String, value: String,
         createdAt: Date = Date(), modifiedAt: Date = Date(),
         enabled: Bool = true, note: String = "") {
        self.id = id
        self.kind = kind
        self.key = key
        self.value = value
        self.createdAt = createdAt
        self.modifiedAt = modifiedAt
        self.enabled = enabled
        self.note = note
    }

    var normalizedKey: String {
        key.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
    }

    /// A preference must never hold a credential or a chunk of document text.
    ///
    /// This is a guard, not a scanner: it refuses values that look like a secret
    /// (a long token, a key=value credential, a PEM header) and values that are
    /// too long to be a preference at all (document text). A false positive costs
    /// a person one retype; a false negative stores a secret in a plain JSON file.
    static func secretRefusal(in value: String) -> String? {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.count > 300 {
            return "That is too long for a preference (limit 300 characters). Preferences "
                + "are not a place to store document text."
        }
        let lowered = trimmed.lowercased()
        let secretMarkers = ["api_key", "apikey", "api-key", "secret", "password", "passwd",
                             "token=", "bearer ", "-----begin", "sk-", "ghp_", "xoxb-",
                             "aws_access_key", "private key"]
        if let marker = secretMarkers.first(where: { lowered.contains($0) }) {
            return "That looks like a credential (“\(marker)”). Preferences never store keys, "
                + "tokens or passwords — use Settings → AI Providers for those."
        }
        // A long unbroken base64/hex blob is a key far more often than a setting.
        let compact = trimmed.filter { !$0.isWhitespace }
        if compact.count >= 40,
           compact.allSatisfy({ $0.isHexDigit || $0.isLetter || $0.isNumber || "+/=_-".contains($0) }),
           !trimmed.contains(" ") {
            return "That looks like a key or a token. Preferences never store credentials."
        }
        return nil
    }
}