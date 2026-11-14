import Foundation

/// A task proposed from a document, for review before it exists.
///
/// Nothing in this type is a task yet. The brief requires extracted actions to
/// stay drafts until a person approves them, so this is the editable middle step:
/// title, optional detail, an *optional* due date and a source link. A
/// `dueDate` is only set when the document (or the model's answer) stated one
/// outright — `dueConfidence` says how sure that is, and `.none` means "no date".
struct ProposedAction: Identifiable, Equatable {
    let id = UUID()
    var title: String
    var detail: String
    var dueDate: Date?
    /// What made the date credible. `.stated` is a date the text named;
    /// `.inferred` is a weekday or relative word; `.none` is no date at all.
    enum DueConfidence: String, Equatable {
        case stated, inferred, none
        var label: String {
            switch self {
            case .stated: return "Date stated in the document"
            case .inferred: return "Date inferred from wording — please check"
            case .none: return "No date"
            }
        }
    }
    var dueConfidence: DueConfidence = .none
    /// Who the document says should do it, when it says.
    var owner: String = ""
    /// The source this came from, so a task created from it can cite the file.
    var sourceName: String = ""
    var sourceIdentity: String = ""
    var isApproved: Bool = false

    var hasDate: Bool { dueDate != nil }
}

/// Reads a model's action-item answer into `ProposedAction`s.
///
/// This parses *without* guessing: a numbered or bulleted line becomes one
/// proposal; "owner: X" and "due: X" sub-lines are read when present; a date is
/// only attached when the text names one. A line that is prose rather than a
/// list item is kept as a single proposal only when the answer is a single
/// sentence — otherwise the answer is shown raw and no tasks are proposed, which
/// is more honest than inventing a list from a paragraph.
struct ActionItemParser {

    struct Parsed {
        var actions: [ProposedAction]
        /// Set when the answer could not be read as a list. The screen shows the
        /// raw text and explains why there are no proposed tasks.
        var problem: String?
    }

    /// Parse the model's answer for the given document.
    ///
    /// `now` and `calendar` are parameters so the date resolution is
    /// deterministic in tests and uses the person's own time zone in production.
    func parse(_ answer: String, source: DocumentSource,
               now: Date = Date(), calendar: Calendar = .current) -> Parsed {
        let lowered = answer.lowercased()
        if lowered.contains("no action items found") || lowered.contains("none found") {
            return Parsed(actions: [], problem: nil)
        }

        let lines = answer.components(separatedBy: .newlines)
            .map { $0.trimmingCharacters(in: .whitespaces) }
        var actions: [ProposedAction] = []
        var current: ProposedAction?

        for line in lines {
            guard !line.isEmpty else { continue }
            if let item = listItemText(line) {
                // A new numbered/bulleted line starts a new action.
                if var pending = current { finalise(&pending); actions.append(pending) }
                current = ProposedAction(title: item, detail: "",
                                         sourceName: source.displayName,
                                         sourceIdentity: source.identity)
                continue
            }
            // A continuation line belongs to the action above it: "owner:" and
            // "due:" are read into fields, anything else into the detail.
            guard var pending = current else { continue }
            let field = fieldText(line)
            switch field.key {
            case "owner", "assigned to", "who":
                pending.owner = field.value
            case "due", "deadline", "when", "by":
                if let match = NaturalDates.firstMatch(in: field.value, now: now,
                                                       calendar: calendar) {
                    pending.dueDate = match.date
                    pending.dueConfidence = .stated
                } else {
                    // "next week"-style words are read by the same parser; a
                    // phrase it does not know leaves the date off.
                    pending.detail = [pending.detail, field.value]
                        .filter { !$0.isEmpty }.joined(separator: " · ")
                }
            default:
                if field.key.isEmpty {
                    pending.detail = [pending.detail, line]
                        .filter { !$0.isEmpty }.joined(separator: " ")
                }
            }
            current = pending
        }
        if var pending = current { finalise(&pending); actions.append(pending) }

        if actions.isEmpty {
            // No list. A single short sentence that reads like an instruction is
            // one proposal; anything longer is shown raw.
            let trimmed = answer.trimmingCharacters(in: .whitespacesAndNewlines)
            // Only a *short imperative* is a proposal. A longer sentence — or
            // one that reads as description ("This document describes…") — is
            // reported as "not a list" rather than being turned into a task the
            // document never asked for.
            let imperativeVerbs = ["send", "book", "update", "call", "email", "reply", "review",
                                   "prepare", "draft", "schedule", "create", "add", "write",
                                   "finish", "complete", "confirm", "submit", "share", "fix",
                                   "contact", "ask", "check", "order", "file", "sign", "pay",
                                   "read", "follow", "set", "make", "get", "do", "delete",
                                   "remove", "cancel", "upload", "download", "test"]
            let firstWord = trimmed.lowercased()
                .split(whereSeparator: { !$0.isLetter }).first.map(String.init) ?? ""
            let looksImperative = imperativeVerbs.contains(firstWord)
            if !trimmed.isEmpty, trimmed.count <= 160,
               !trimmed.contains("\n\n") || looksImperative,
               looksImperative || trimmed.contains("\n") {
                var single = ProposedAction(title: trimmed, detail: "",
                                            sourceName: source.displayName,
                                            sourceIdentity: source.identity)
                finalise(&single)
                return Parsed(actions: [single], problem: nil)
            }
            return Parsed(actions: [], problem:
                "That answer was not a list of actions, so no tasks are proposed. Read the "
                + "answer above and add anything you want by hand.")
        }
        return Parsed(actions: actions, problem: nil)
    }

    /// Fill in a title/date that came from the same line ("- Send the deck by Friday").
    private func finalise(_ action: inout ProposedAction) {
        let title = action.title.trimmingCharacters(in: .whitespacesAndNewlines)
        if let match = NaturalDates.firstMatch(in: title) {
            action.dueDate = match.date
            action.dueConfidence = .stated
            action.title = NaturalDates.withoutDatePhrase(title, phrase: match.phrase)
        }
        if action.title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            action.title = title
        }
        action.title = action.title.trimmingCharacters(in: CharacterSet(charactersIn: " -–—:.,"))
    }

    /// The text of a numbered ("1. …", "1) …") or bulleted ("- …", "• …", "* …") line.
    private func listItemText(_ line: String) -> String? {
        let patterns = [#"^\d{1,2}[.)]\s+(.+)$"#, #"^[-•*–—]\s+(.+)$"#, #"^\*\*(.+)\*\*$"#]
        for pattern in patterns {
            if let regex = try? NSRegularExpression(pattern: pattern),
               let match = regex.firstMatch(in: line, range: NSRange(line.startIndex..., in: line)),
               match.numberOfRanges > 1,
               let range = Range(match.range(at: 1), in: line) {
                return String(line[range]).trimmingCharacters(in: .whitespaces)
            }
        }
        return nil
    }

    /// Split "Owner: Priya" / "Due: Friday" into key and value.
    private func fieldText(_ line: String) -> (key: String, value: String) {
        guard let colon = line.firstIndex(of: ":") else { return ("", line) }
        let key = line[line.startIndex..<colon]
            .trimmingCharacters(in: CharacterSet(charactersIn: " -–—•*"))
            .lowercased()
        let value = line[line.index(after: colon)...]
            .trimmingCharacters(in: CharacterSet(charactersIn: " -–—•*"))
        // A "key" with more than three words is a sentence that contains a colon.
        guard key.split(separator: " ").count <= 3 else { return ("", line) }
        return (key, value)
    }
}