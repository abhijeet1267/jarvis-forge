import Foundation

/// One thing a person captured and has not dealt with yet.
///
/// The inbox is deliberately *not* a second task system. An item here is
/// unprocessed information; the moment it becomes a task, a reminder or a flow,
/// the authoritative record lives in that destination and this item keeps only a
/// pointer to it (`destination`) plus `converted` status. That is what keeps
/// "captured twice" from meaning "two tasks" — the planner stays the owner.
struct CaptureItem: Codable, Identifiable, Equatable {

    /// What the item is, as the person meant it.
    ///
    /// `general` exists so an ambiguous sentence has an honest home. The brief is
    /// explicit that a sentence merely *sounding* like a task must not create a
    /// task, so anything the classifier cannot place stays general and can be
    /// classified by hand later.
    enum Kind: String, Codable, CaseIterable {
        case general
        case note
        case idea
        case taskCandidate
        case reminderCandidate
        case link
        case workflowIdea

        var displayName: String {
            switch self {
            case .general: return "Inbox item"
            case .note: return "Note"
            case .idea: return "Idea"
            case .taskCandidate: return "Task candidate"
            case .reminderCandidate: return "Reminder candidate"
            case .link: return "Link"
            case .workflowIdea: return "Workflow idea"
            }
        }

        var symbol: String {
            switch self {
            case .general: return "tray"
            case .note: return "note.text"
            case .idea: return "lightbulb"
            case .taskCandidate: return "checklist"
            case .reminderCandidate: return "alarm"
            case .link: return "link"
            case .workflowIdea: return "square.stack.3d.up"
            }
        }

        /// Which destination this kind is *offered* for. The person still presses
        /// the button; nothing is converted automatically.
        var suggestedDestination: CaptureDestination? {
            switch self {
            case .taskCandidate, .reminderCandidate: return .task
            case .workflowIdea: return .flow
            case .general, .note, .idea, .link: return nil
            }
        }
    }

    enum Status: String, Codable, CaseIterable {
        case inbox
        case reviewed
        case converted
        case archived

        var displayName: String {
            switch self {
            case .inbox: return "Inbox"
            case .reviewed: return "Reviewed"
            case .converted: return "Converted"
            case .archived: return "Archived"
            }
        }
    }

    let id: UUID
    var content: String
    var kind: Kind
    var status: Status
    var tags: [String]
    var createdAt: Date
    var modifiedAt: Date
    /// Set when the item was converted; the identifier lives in the destination.
    var destinationReference: String?
    var destinationLabel: String?
    /// A date the sentence actually named, kept as a *suggestion* for conversion.
    /// Never applied on its own.
    var suggestedDue: Date?

    init(id: UUID = UUID(), content: String, kind: Kind = .general,
         status: Status = .inbox, tags: [String] = [],
         createdAt: Date = Date(), modifiedAt: Date = Date(),
         destinationReference: String? = nil, destinationLabel: String? = nil,
         suggestedDue: Date? = nil) {
        self.id = id
        self.content = content
        self.kind = kind
        self.status = status
        self.tags = tags
        self.createdAt = createdAt
        self.modifiedAt = modifiedAt
        self.destinationReference = destinationReference
        self.destinationLabel = destinationLabel
        self.suggestedDue = suggestedDue
    }

    var isOpen: Bool { status == .inbox || status == .reviewed }

    /// The first line, for a list row. Full content stays available in the editor.
    var headline: String {
        let firstLine = content
            .split(whereSeparator: \.isNewline)
            .first.map(String.init) ?? content
        let trimmed = firstLine.trimmingCharacters(in: .whitespaces)
        return trimmed.isEmpty ? "(empty)" : trimmed
    }

    /// A stable fingerprint used to refuse a duplicate capture.
    ///
    /// Lower-cased, whitespace-collapsed and punctuation-stripped, so "Remember
    /// to call Sam." and "remember to call sam" are the same capture. The kind is
    /// part of it: the same words captured deliberately as a note and as a
    /// workflow idea are two different decisions, not a duplicate.
    var fingerprint: String {
        let collapsed = content
            .lowercased()
            .components(separatedBy: .whitespacesAndNewlines)
            .filter { !$0.isEmpty }
            .joined(separator: " ")
        let stripped = collapsed.unicodeScalars.filter {
            CharacterSet.alphanumerics.contains($0) || $0 == " "
        }
        return kind.rawValue + "|" + String(String.UnicodeScalarView(stripped))
            .trimmingCharacters(in: .whitespaces)
    }
}

/// A place a captured item can be sent *after a person approves it*.
///
/// Separated from `CaptureItem.Kind` because it is a different question: the kind
/// is what the thing is, this is where it may go. A "workflow idea" can become a
/// flow; a "task candidate" can become a planner task.
enum CaptureDestination: String, CaseIterable {
    case task
    case flow
    case reminder
    case note

    var title: String {
        switch self {
        case .task: return "Planner task"
        case .flow: return "Flow draft"
        case .reminder: return "Reminder"
        case .note: return "Keep as note"
        }
    }

    var detail: String {
        switch self {
        case .task: return "Creates a task you can review and edit in the Planner."
        case .flow: return "Opens an editable flow draft. Nothing is saved or run."
        case .reminder: return "Creates a scheduled reminder job on the backend."
        case .note: return "Marks the item reviewed and keeps it in the inbox."
        }
    }

    var captureKind: CaptureItem.Kind {
        switch self {
        case .task: return .taskCandidate
        case .flow: return .workflowIdea
        case .reminder: return .reminderCandidate
        case .note: return .note
        }
    }
}