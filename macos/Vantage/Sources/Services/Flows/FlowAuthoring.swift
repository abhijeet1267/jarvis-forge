import Foundation

/// Drafts a flow from a sentence, for a person to review before anything is saved.
///
/// This is deliberately the *same* parser Command Center uses for single actions
/// (`MacCommandParser`), run clause by clause, so a flow step and a typed command
/// cannot mean different things. What it must never do is execute: the result of
/// `draft(_:)` is a `Flow` in memory that the editor shows. Saving is a separate,
/// explicit act and running is a third.
///
/// The three intents the brief separates are decided before any drafting:
/// * "create/save a flow that …" -> `.draft`
/// * "run my X flow" -> `.run`
/// * "what does my X flow do" -> `.describe`
struct FlowAuthoring {

    enum Intent: Equatable {
        case draft(Flow)
        case run(Flow)
        case describe(Flow)
        /// Named a flow that does not exist; the string is the message to show.
        case unknownFlow(String)
        /// A flow request with a part that could not be mapped; the draft is
        /// still returned so the person can fix it, with the problems named.
        case incompleteDraft(Flow, [String])
        /// Not a flow request.
        case notAFlowRequest
    }

    private let parser: MacCommandParser

    /// Installed application names. Authoring checks an open/launch target
    /// against this list, because `MacCommandParser` treats "open <anything>" as
    /// a launch request (correct for a typed command, where the executor then
    /// reports "not installed"). For a *draft flow* an unresolvable target is not
    /// a step to save — measured: "…and makes me a sandwich" drafted a step that
    /// would open an app called "makes me a sandwich".
    private let applications: [String]

    init(applications: [String]) {
        parser = MacCommandParser(applications: applications)
        self.applications = applications
    }

    func interpret(_ text: String, store: FlowStore) -> Intent {
        let lowered = text.lowercased()
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)

        // --- describe -----------------------------------------------------
        if Self.matches(lowered, #"\b(what (does|do)|show me what|describe|explain)\b.*\bflow\b"#)
            || Self.matches(lowered, #"^what('s| is) in my\b.*\bflow"#) {
            let name = Self.flowName(in: text) ?? store.flows.first?.name ?? ""
            guard let flow = store.flow(named: name) else {
                return .unknownFlow("I could not find a flow called “\(name)”. "
                    + (store.flows.isEmpty ? "You have no saved flows yet."
                       : "Your flows are: \(store.flows.map(\.name).joined(separator: ", "))."))
            }
            return .describe(flow)
        }

        // --- run ----------------------------------------------------------
        if Self.matches(lowered, #"^(please\s+)?(run|start|execute|do)\b.*\bflow\b"#)
            || Self.matches(lowered, #"^(please\s+)?run my\b"#) {
            let name = Self.flowName(in: text) ?? ""
            guard !name.isEmpty else {
                return .unknownFlow("Which flow should I run? "
                    + (store.flows.isEmpty ? "You have no saved flows yet."
                       : "Your flows are: \(store.flows.map(\.name).joined(separator: ", "))."))
            }
            guard let flow = store.flow(named: name) else {
                return .unknownFlow("I could not find a flow called “\(name)”. "
                    + (store.flows.isEmpty ? "You have no saved flows yet."
                       : "Your flows are: \(store.flows.map(\.name).joined(separator: ", "))."))
            }
            return .run(flow)
        }

        // --- draft --------------------------------------------------------
        guard Self.matches(lowered, #"\b(create|make|build|new|add|set up|prepare|compose|draft)\b.*\bflow\b"#)
              || Self.matches(lowered, #"\bflow\b.*\b(that|which|to)\b"#) else {
            return .notAFlowRequest
        }

        let (name, body) = Self.splitNameAndBody(trimmed)
        var draft = Flow(name: name.isEmpty ? "New Flow" : name)

        var problems: [String] = []
        var steps: [FlowStep] = []
        for clause in Self.expandClauses(body) {
            let cleaned = Self.imperative(clause)
            guard !cleaned.isEmpty else { continue }
            switch parser.parse(cleaned) {
            case .action(let kind, let arguments):
                if let note = targetProblem(kind: kind, arguments: arguments) {
                    problems.append("“\(cleaned)”: \(note)")
                    continue
                }
                steps.append(FlowStep(actionID: kind.id, arguments: StoredArguments(arguments)))
            case .plan(let planned):
                for step in planned {
                    steps.append(FlowStep(actionID: step.kind.id,
                                          arguments: StoredArguments(step.arguments)))
                }
            case .explanation:
                problems.append("“\(cleaned)” is a question, not a step. I left it out.")
            case .clarification(let question):
                problems.append("“\(cleaned)”: \(question)")
            case .notMacControl:
                problems.append("“\(cleaned)” is not an action Vantage can perform, so I left "
                                + "it out of the draft.")
            }
        }

        draft.steps = steps
        draft.description = "Drafted from: \(trimmed)"
        if steps.isEmpty {
            problems.append("No step in that sentence matched a supported action.")
            return .incompleteDraft(draft, problems)
        }
        return problems.isEmpty ? .draft(draft) : .incompleteDraft(draft, problems)
    }

    /// A step whose target cannot be resolved is not a draft step.
    ///
    /// Only the two cases where authoring has enough information to tell: an
    /// application that is not installed, and a folder name that is not one of
    /// the standard ones. A path is left alone — the executor validates it, and
    /// it may legitimately not exist yet.
    private func targetProblem(kind: MacActionKind, arguments: MacActionArguments) -> String? {
        switch kind {
        case .openApplication, .activateApplication, .quitApplication:
            let wanted = arguments.application.trimmingCharacters(in: .whitespaces)
            guard !wanted.isEmpty else { return "no application was named" }
            let known = applications.contains { $0.caseInsensitiveCompare(wanted) == .orderedSame }
            guard known else {
                return "“\(wanted)” is not an application installed on this Mac, so I left it "
                    + "out of the draft."
            }
            return nil
        case .openFolder:
            let allowed = ["downloads", "documents", "desktop", "pictures", "music",
                           "movies", "applications", "public"]
            let value = arguments.path.trimmingCharacters(in: .whitespaces)
            if value.hasPrefix("/") { return nil }
            guard allowed.contains(value.lowercased()) else {
                return "“\(value)” is not one of the folders Vantage opens."
            }
            return nil
        default:
            return nil
        }
    }

    // MARK: - Text helpers

    private static func matches(_ text: String, _ pattern: String) -> Bool {
        text.range(of: pattern, options: [.regularExpression, .caseInsensitive]) != nil
    }

    /// The flow name in quotes if there is one, otherwise the words between
    /// "flow" and the sentence's body.
    ///
    /// Prefers a quoted name because that is unambiguous; the unquoted form is
    /// the trailing "… flow" or "my X flow" a person writes in a hurry.
    static func flowName(in text: String) -> String? {
        let quoted = #"["“']([^"”']{2,60})["”']"#
        if let regex = try? NSRegularExpression(pattern: quoted),
           let match = regex.firstMatch(in: text, range: NSRange(text.startIndex..., in: text)),
           let range = Range(match.range(at: 1), in: text) {
            return String(text[range]).trimmingCharacters(in: .whitespaces)
        }
        // "run my Start Workday flow" / "the Start Workday flow"
        let named = #"(?:my|the)\s+([A-Za-z0-9][A-Za-z0-9 ._\-]{1,40}?)\s+flow\b"#
        if let regex = try? NSRegularExpression(pattern: named, options: [.caseInsensitive]),
           let match = regex.firstMatch(in: text, range: NSRange(text.startIndex..., in: text)),
           let range = Range(match.range(at: 1), in: text) {
            return String(text[range]).trimmingCharacters(in: .whitespaces)
        }
        return nil
    }

    /// Split "Create a Start Workday flow that opens Safari and Finder" into
    /// ("Start Workday", "opens Safari and Finder").
    static func splitNameAndBody(_ text: String) -> (name: String, body: String) {
        // The body starts at "that"/"which"/"to" after the word "flow", or after
        // a colon. Everything before it is the description of the name.
        let pattern = #"\bflow\b\s*(?::|that\b|which\b|to\b)\s*"#
        if let regex = try? NSRegularExpression(pattern: pattern, options: [.caseInsensitive]),
           let match = regex.firstMatch(in: text, range: NSRange(text.startIndex..., in: text)),
           let bodyRange = Range(match.range, in: text) {
            let head = String(text[text.startIndex..<bodyRange.lowerBound])
            let body = String(text[bodyRange.upperBound...])
            return (cleanedName(head), body)
        }
        // No "that": "Create a Start Workday flow" with no steps.
        if let name = flowName(in: text), text.lowercased().contains("flow") {
            return (name, "")
        }
        return ("", text)
    }

    /// Strip the imperative and the article from "Create a Start Workday".
    private static func cleanedName(_ head: String) -> String {
        var name = head.trimmingCharacters(in: .whitespacesAndNewlines)
        let verbs = #"^(please\s+)?(create|make|build|add|set up|prepare|compose|draft)\s+"#
        name = name.replacingOccurrences(of: verbs, with: "", options: [.regularExpression, .caseInsensitive])
        name = name.replacingOccurrences(of: #"^(a|an|the|my)\s+"#, with: "",
                                         options: [.regularExpression, .caseInsensitive])
        name = name.replacingOccurrences(of: #"\s+flow$"#, with: "",
                                         options: [.regularExpression, .caseInsensitive])
        name = name.trimmingCharacters(in: .whitespacesAndNewlines)
        // "create a flow that …" has no name at all; after stripping the
        // article the head is the bare word "a", which would become the flow's
        // name. A nameless draft gets the editor's default instead.
        let placeholders = ["a", "an", "the", "my", "new", "flow"]
        if placeholders.contains(name.lowercased()) { return "" }
        return name
    }

    /// Split a described body into one clause per action.
    ///
    /// Two normalisations a person's sentence needs and a typed command does not:
    ///
    /// * **"opens Safari, Finder and Documents"** is one verb and three objects.
    ///   Splitting on the commas alone would make three fragments of which one
    ///   parses, so the verb is carried across the list.
    /// * **Past-tense / third-person verbs** ("opens", "sets", "then sets") are
    ///   how a flow is described. `MacCommandParser` expects the imperative
    ///   ("open", "set"), so the verb is rewritten rather than the parser being
    ///   taught a second grammar.
    static func expandClauses(_ body: String) -> [String] {
        let pieces = MacCommandParser.splitSequence(body).map {
            $0.replacingOccurrences(of: #"^(and|then|also|plus)\s+"#, with: "",
                                    options: [.regularExpression, .caseInsensitive])
        }
        // The verb of the first piece governs the following bare items.
        guard let first = pieces.first,
              let verb = leadingVerb(in: first) else { return pieces }

        var out: [String] = []
        for (index, piece) in pieces.enumerated() {
            let base = leadingVerb(in: piece) != nil ? piece : "\(verb) \(piece)"
            if index == 0 {
                out.append(base)
                continue
            }
            // "Finder and my Documents folder" is two objects of one verb.
            let parts = base.components(separatedBy: " and ")
            if parts.count > 1, leadingVerb(in: parts.first ?? "") != nil,
               parts.dropFirst().allSatisfy({ leadingVerb(in: $0) == nil }) {
                for part in parts {
                    let cleaned = leadingVerb(in: part) != nil ? part : "\(verb) \(part)"
                    out.append(cleaned)
                }
            } else {
                out.append(base)
            }
        }
        return out
    }

    /// The imperative verb at the start of a clause, if it is one this router
    /// knows. Third-person forms map to it ("opens" -> "open").
    private static func leadingVerb(in clause: String) -> String? {
        let trimmed = clause.trimmingCharacters(in: .whitespaces)
        guard let word = trimmed.split(separator: " ").first.map(String.init)?.lowercased() else {
            return nil
        }
        let known = ["open", "opens", "launch", "launches", "start", "starts", "switch",
                     "switches", "set", "sets", "minimize", "minimizes", "minimise",
                     "minimises", "close", "closes", "quit", "quits", "mute", "mutes",
                     "unmute", "unmutes", "move", "moves", "activate", "activates",
                     "show", "shows", "list", "lists", "go", "goes", "bring", "brings"]
        return known.contains(word) ? word : nil
    }

    /// Rewrite a described clause into the imperative the action parser expects.
    private static func imperative(_ clause: String) -> String {
        let trimmed = clause.trimmingCharacters(in: .whitespaces)
        let pairs: [(String, String)] = [
            ("opens", "open"), ("launches", "open"), ("starts", "open"),
            ("switches", "switch"), ("sets", "set"), ("minimizes", "minimize"),
            ("minimises", "minimize"), ("closes", "close"), ("quits", "quit"),
            ("mutes", "mute"), ("unmutes", "unmute"), ("moves", "move"),
            ("activates", "switch to"), ("shows", "show"), ("lists", "show"),
            ("goes", "go"),
        ]
        for (from, to) in pairs where trimmed.lowercased().hasPrefix(from + " ") {
            return to + String(trimmed.dropFirst(from.count))
        }
        return trimmed
    }

    /// "and opens Finder" in the middle of a clause list reads as a verb phrase;
    /// a leading conjunction is noise, not a step.
    private static func stripLeadingAnd(_ clause: String) -> String {
        clause.replacingOccurrences(of: #"^(and|then|also|plus)\s+"#, with: "",
                                    options: [.regularExpression, .caseInsensitive])
    }
}