import Foundation

/// Reads a sentence and decides whether it is a *capture* and, if so, what kind.
///
/// This is the line the brief draws most explicitly: capturing an idea and
/// executing an action are different things. "Remember to review the proposal"
/// files a note; it does not create a task, and it certainly does not open
/// anything. "Turn this into a task for tomorrow" is a capture with a *suggested*
/// destination and a *suggested* date, both of which the person confirms before
/// anything is created.
///
/// Everything here is a classifier, not an executor. Its output is a proposal
/// that the inbox screen (or the palette's confirmation card) turns into a
/// stored item.
struct CaptureClassifier {

    /// What the sentence asks for. Kept apart from the capture kinds because the
    /// palette needs to know whether to capture at all.
    enum Intent: Equatable {
        /// A capture. The item is what should be stored.
        case capture(CaptureItem)
        /// Not a capture — let the next resolver look at it (a task, a flow, a
        /// Mac action, or ordinary conversation).
        case notACapture

        var item: CaptureItem? {
            if case .capture(let item) = self { return item }
            return nil
        }
    }

    /// The verbs that mean "put this in my inbox".
    ///
    /// Ordered so the more specific phrase wins: "save this as a note" must be
    /// read as a note, not as a generic save.
    private static let captureVerbs: [String] = [
        "capture this", "capture that", "capture:", "capture ",
        "turn this into", "turn that into", "make this a", "make that a",
        "save this", "save that", "save this as", "save as",
        "remember this", "remember that", "remember to",
        "note this", "note that", "note down", "make a note",
        "add this to my", "add to my", "jot down", "jot this",
        "keep this", "keep a note", "store this",
        "put this in my inbox", "add to inbox", "inbox this",
        "for my next planning session", "for later",
    ]

    /// Phrases that name a destination, which is a capture with a suggestion —
    /// never an immediate creation.
    private static let destinationPhrases: [(String, CaptureDestination)] = [
        ("as a task", .task), ("into a task", .task), ("to a task", .task),
        ("task for", .task), ("turn this into a task", .task),
        ("as a reminder", .reminder), ("into a reminder", .reminder),
        ("remind me", .reminder), ("reminder for", .reminder),
        ("as a flow", .flow), ("into a flow", .flow), ("workflow idea", .flow),
        ("to my workflow ideas", .flow), ("flow idea", .flow),
        ("as a note", .note), ("into a note", .note), ("as an idea", .note),
    ]

    /// Classify a sentence.
    ///
    /// Returns `.notACapture` for anything that does not *ask* to be captured,
    /// so "create a task to call Sam" stays a task request and "run my flow"
    /// stays a flow run. That separation is what keeps capture from swallowing
    /// the rest of the palette.
    func classify(_ raw: String) -> Intent {
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return .notACapture }
        let lowered = text.lowercased()

        // A request to *do* something with an existing thing is not a capture.
        if isExecutionOrCreationRequest(lowered) { return .notACapture }

        guard let hit = firstCaptureVerb(in: lowered) else { return .notACapture }

        // The content is what is left after the verb phrase.
        let content = Self.strip(hit.phrase, from: text)
        guard !content.isEmpty else {
            // "capture this" with nothing after it: the palette asks what to
            // capture rather than storing an empty item.
            return .notACapture
        }

        // A destination phrase names where the person expects it to *go*.
        var destination = Self.destinationPhrases.first {
            lowered.contains($0.0)
        }?.1 ?? Self.destinationFromKindWords(lowered)

        var kind = Self.kind(for: lowered, content: content, destination: destination)

        // A capture whose content is just a URL is a link, whatever vocabulary
        // appeared in the sentence around it. The content decides this one.
        if Self.bareLink(content) != nil {
            kind = .link
            destination = nil
        }

        // A date the sentence actually names becomes a *suggestion*. Vague words
        // ("soon", "sometime") produce nothing: an invented deadline on a task is
        // worse than none.
        let dateMatch = NaturalDates.firstMatch(in: text)
        let title = dateMatch.map { NaturalDates.withoutDatePhrase(content, phrase: $0.phrase) }
            ?? content

        var item = CaptureItem(content: title.trimmingCharacters(in: .whitespacesAndNewlines),
                               kind: kind)
        item.suggestedDue = dateMatch?.date
        return .capture(item)
    }

    /// Whether the sentence asks Vantage to *do* something rather than to file it.
    ///
    /// Deliberately checked before the capture verbs: "remind me to call Sam"
    /// contains "remind me", which is a destination phrase, but a person saying
    /// it in the palette may well mean "create the reminder now". That reading is
    /// wrong under the brief — a capture that *suggests* the reminder and asks
    /// first is always the safe one — so "remind me" is treated as a capture
    /// with a `.reminder` suggestion, while explicit creation verbs are not.
    private func isExecutionOrCreationRequest(_ lowered: String) -> Bool {
        let creation = [
            #"^(please\s+)?(create|make|add|new)\s+(a\s+)?task\b"#,
            #"^(create|make|add)\s+(a\s+)?reminder\b"#,
            #"^(create|make|build|draft)\s+(a\s+)?flow\b"#,
            #"^(run|start|execute)\b"#,
        ]
        return creation.contains { pattern in
            lowered.range(of: pattern, options: [.regularExpression, .caseInsensitive]) != nil
        }
    }

    private struct VerbHit {
        let phrase: String
        let range: Range<String.Index>
    }

    /// The earliest capture verb in the sentence, so "save this idea: …" is read
    /// from "save this" rather than from a phrase later in the content.
    private func firstCaptureVerb(in lowered: String) -> VerbHit? {
        var best: VerbHit?
        for phrase in Self.captureVerbs {
            guard let range = lowered.range(of: phrase) else { continue }
            if let current = best, current.range.lowerBound <= range.lowerBound { continue }
            best = VerbHit(phrase: phrase, range: range)
        }
        return best
    }

    private static func strip(_ phrase: String, from text: String) -> String {
        guard let range = text.lowercased().range(of: phrase) else { return text }
        var rest = String(text[range.upperBound...])
        // "save this idea: make Vantage open files faster" -> drop the label.
        rest = rest.replacingOccurrences(of: #"^\s*(idea|note|thought|task|reminder|link)\s*:"#,
                                         with: "", options: [.regularExpression, .caseInsensitive])
        rest = rest.replacingOccurrences(of: #"^\s*(this|that)\b"#, with: "",
                                         options: [.regularExpression, .caseInsensitive])
        rest = rest.replacingOccurrences(of: #"^\s*[:—\-]\s*"#, with: "",
                                         options: [.regularExpression])
        return rest.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// Kind words inside the sentence, when no explicit destination phrase was used.
    private static func destinationFromKindWords(_ lowered: String) -> CaptureDestination? {
        // Word-boundary matches only: "https://example.com/notes" contains the
        // letters of "note" but is a link, not a request for a note.
        func has(_ word: String) -> Bool {
            lowered.range(of: "\\b" + word + "\\b", options: .regularExpression) != nil
        }
        if has("workflow") || lowered.contains("flow idea") { return .flow }
        if has("reminder") || lowered.contains("remind me") { return .reminder }
        if has("task") { return .task }
        if has("note") { return .note }
        return nil
    }

    private static func kind(for lowered: String, content: String,
                             destination: CaptureDestination?) -> CaptureItem.Kind {
        switch destination {
        case .task: return .taskCandidate
        case .reminder: return .reminderCandidate
        case .flow: return .workflowIdea
        case .note: return .note
        case nil: break
        }
        if lowered.contains("idea") || lowered.contains("what if") { return .idea }
        if lowered.contains("note") { return .note }
        // "remember to…" reads as a to-do the person does not want filed as a
        // task yet — a candidate, which is exactly what the brief calls for.
        if lowered.hasPrefix("remember to") || lowered.contains("remember to") {
            return .taskCandidate
        }
        return .general
    }

    /// A capture verb the palette should offer as a suggestion, matched against
    /// what someone actually typed. Used to label the preview ("Save to inbox").
    static func captureLabel(for raw: String) -> String? {
        let item = CaptureClassifier().classify(raw).item
        return item.map { "Save \($0.kind.displayName.lowercased()) to inbox" }
    }

    /// True when the sentence looks like a link alone ("https://…", "www.…").
    private static func bareLink(_ text: String) -> URL? {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let url = URL(string: trimmed), let scheme = url.scheme,
              scheme == "http" || scheme == "https", url.host != nil else { return nil }
        return url
    }
}