import Foundation

/// What the global palette should do with what someone typed.
///
/// This is the single resolver behind both Vantage Anywhere and — through
/// `ConversationService` — Command Center, so a sentence cannot mean one thing in
/// the palette and another in the window. It produces a *proposal*:
/// `MacControlCoordinator` still performs the validation, permission and risk
/// checks, and nothing here executes.
struct AnywhereCommand {

    enum Outcome: Equatable {
        /// A single action to run.
        case action(MacActionKind, MacActionArguments)
        /// A sequence the person described.
        case plan([MacCommandParser.Step])
        /// A saved flow to run.
        case runFlow(Flow)
        /// A saved flow to explain.
        case describeFlow(Flow)
        /// A flow drafted from words, for review. Never run from a draft.
        case draftFlow(Flow, [String])
        /// Wants the list of flows.
        case listFlows([Flow])
        /// A question about the Mac: answer it, do not act.
        case explanation
        /// Needs one more detail before it can act.
        case clarification(String)
        /// Not something the palette handles; the full window will answer.
        case conversation(String)
        /// A flow request that could not be completed.
        case flowProblem(String)

        // --- Phase 3 -------------------------------------------------------

        /// Save something to the Quick Capture inbox. Nothing is created, sent
        /// or run; this is the whole action.
        case capture(CaptureItem)
        /// Show what is waiting in the inbox.
        case showInbox([CaptureItem])
        /// Open the Daily Briefing.
        case briefing
        /// Open the Documents screen — the palette cannot pick a file itself
        /// without taking over the screen, so it hands off.
        case openDocuments
        /// Answer a question about a document the app has open.
        case documentQuestion(String)
    }

    struct Resolution: Equatable {
        let outcome: Outcome
        /// A one-line label for the palette ("Open Safari", "Start Workday").
        let label: String
        /// Whether this will need a confirmation before it runs. Shown up front so
        /// the palette never looks like it bypassed approval.
        let needsConfirmation: Bool
    }

    private let parser: MacCommandParser
    private let authoring: FlowAuthoring
    private let store: FlowStore
    /// The inbox, for captures and the "show my inbox" request. Passed in rather
    /// than reached for, so the resolver stays testable with a temporary store.
    private let captureBox: () -> CaptureStore
    private var capture: CaptureStore { captureBox() }
    private let classifier = CaptureClassifier()
    /// The documents the app currently has open. Only names are known here — the
    /// palette never reads a file, and a question about a document is routed to
    /// the screen that holds its text.
    private let openDocuments: () -> [String]

    init(applications: [String], store: FlowStore,
         capture: CaptureStore? = nil,
         openDocuments: @escaping () -> [String] = { [] }) {
        parser = MacCommandParser(applications: applications)
        authoring = FlowAuthoring(applications: applications)
        self.store = store
        if let capture {
            self.captureBox = { capture }
        } else {
            self.captureBox = { @MainActor in CaptureStore.shared }
        }
        self.openDocuments = openDocuments
    }

    // MARK: - Resolve

    func resolve(_ raw: String) -> Resolution {
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else {
            return Resolution(outcome: .conversation(""), label: "", needsConfirmation: false)
        }
        let lowered = text.lowercased()

        // 0. Phase 3 surfaces that would otherwise be misread as Mac control.
        //
        //    "capture this idea: make Vantage open files faster" contains "open",
        //    and "show my inbox" contains "show" — both would fall into the
        //    parser's hands. Capture is resolved first because it is the most
        //    specific reading, and it is the one that can never execute.
        if let phase3 = resolvePhaseThree(text, lowered: lowered) { return phase3 }

        // 1. Flows, before generic Mac control: "run my Start Workday flow" would
        //    otherwise be caught by nothing at all, and "open Safari" inside a
        //    flow name must not be read as a launch.
        switch authoring.interpret(text, store: store) {
        case .run(let flow):
            return Resolution(outcome: .runFlow(flow), label: "Run “\(flow.name)”",
                              needsConfirmation: flow.steps.contains {
                                  MacActionPolicy.forKind($0.kind ?? .frontmostApp)?
                                      .risk.requiresConfirmation == true
                              })
        case .describe(let flow):
            return Resolution(outcome: .describeFlow(flow),
                              label: "What “\(flow.name)” does",
                              needsConfirmation: false)
        case .unknownFlow(let message):
            return Resolution(outcome: .flowProblem(message), label: "Flow not found",
                              needsConfirmation: false)
        case .draft(let flow):
            return Resolution(outcome: .draftFlow(flow, []),
                              label: "Draft “\(flow.name)” (\(flow.steps.count) steps)",
                              needsConfirmation: false)
        case .incompleteDraft(let flow, let problems):
            return Resolution(outcome: .draftFlow(flow, problems),
                              label: "Draft “\(flow.name)” needs attention",
                              needsConfirmation: false)
        case .notAFlowRequest:
            break
        }

        // 2. Listing saved flows.
        if Self.matches(lowered, #"\b(show|list|what)\b.*\bflows?\b"#)
            || Self.matches(lowered, #"^my flows$"#) {
            return Resolution(outcome: .listFlows(store.flows),
                              label: store.flows.isEmpty ? "No saved flows" : "Saved flows",
                              needsConfirmation: false)
        }

        // 3. Ordinary Mac control — the same parser and the same executor as
        //    Control Mac and Command Center.
        switch parser.parse(text) {
        case .action(let kind, let arguments):
            let policy = MacActionPolicy.forKind(kind)
            return Resolution(outcome: .action(kind, arguments),
                              label: kind.displayName,
                              needsConfirmation: policy?.risk.requiresConfirmation ?? false)
        case .plan(let steps):
            let needs = steps.contains {
                MacActionPolicy.forKind($0.kind)?.risk.requiresConfirmation == true
            }
            return Resolution(outcome: .plan(steps),
                              label: "\(steps.count) steps",
                              needsConfirmation: needs)
        case .explanation:
            return Resolution(outcome: .explanation, label: "Answer", needsConfirmation: false)
        case .clarification(let question):
            return Resolution(outcome: .clarification(question), label: "Needs a detail",
                              needsConfirmation: false)
        case .notMacControl:
            return Resolution(outcome: .conversation(text), label: "Ask Vantage",
                              needsConfirmation: false)
        }
    }

    // MARK: - Phase 3 resolution

    /// The capture, inbox, briefing and document readings of a sentence.
    ///
    /// Order matters and is chosen for safety:
    /// 1. an explicit briefing request;
    /// 2. a capture — checked *before* flows, because "save this as a flow idea"
    ///    is a capture that proposes a flow, not a request to build one now;
    /// 3. the inbox listing;
    /// 4. a document question, when a document is open and the sentence names it.
    ///
    /// A document question is only matched when the app actually has a document
    /// open; otherwise the sentence is left to the normal paths, which is why
    /// this returns nil rather than a "no document" error.
    private func resolvePhaseThree(_ text: String, lowered: String) -> Resolution? {
        // --- Briefing --------------------------------------------------------
        let briefingPatterns = [
            #"^(show|open|give me|what('s| is))?\s*(me\s+)?(my\s+|the\s+)?(daily\s+)?briefing\b"#,
            #"^what('s| is) (on|happening) (today|this morning)\b"#,
            #"^brief me\b"#,
        ]
        if briefingPatterns.contains(where: { Self.matches(lowered, $0) }) {
            return Resolution(outcome: .briefing, label: "Daily briefing",
                              needsConfirmation: false)
        }

        // --- Capture ---------------------------------------------------------
        // `classify` returns `.notACapture` for "create a task…" and "run…",
        // which keeps those on their own paths.
        if let item = classifier.classify(text).item {
            let destination = destinationHint(item)
            let label = destination.map {
                "Save \(item.kind.displayName.lowercased()) to inbox — suggests \($0.title.lowercased())"
            } ?? "Save \(item.kind.displayName.lowercased()) to inbox"
            return Resolution(outcome: .capture(item), label: label,
                              needsConfirmation: false)
        }

        // --- Inbox listing ---------------------------------------------------
        if Self.matches(lowered, #"\b(show|list|open|what('s| is) in)\b.*\b(inbox|captures?)\b"#)
            || Self.matches(lowered, #"^my inbox$"#) {
            let items = capture.items
            return Resolution(outcome: .showInbox(items),
                              label: items.isEmpty ? "Your inbox is empty"
                                                   : "\(items.count) captured item\(items.count == 1 ? "" : "s")",
                              needsConfirmation: false)
        }

        // --- Documents -------------------------------------------------------
        if Self.matches(lowered, #"^(open|show)\b.*\b(documents?|files?)\b"#),
           !Self.matches(lowered, #"\bopen\b.*\b(folder|downloads|documents folder)\b"#) {
            return Resolution(outcome: .openDocuments, label: "Documents",
                              needsConfirmation: false)
        }
        let open = openDocuments()
        if !open.isEmpty,
           let named = open.first(where: { lowered.contains($0.lowercased()) }),
           Self.matches(lowered, #"\b(summar|explain|what|who|when|where|how|question|ask|does|say)\b"#) {
            return Resolution(outcome: .documentQuestion(text),
                              label: "Ask about “\(named)”",
                              needsConfirmation: false)
        }
        return nil
    }

    /// The destination the capture's own words suggest, for the label only. The
    /// person still presses the button; nothing is converted here.
    private func destinationHint(_ item: CaptureItem) -> CaptureDestination? {
        switch item.kind {
        case .taskCandidate: return .task
        case .reminderCandidate: return .reminder
        case .workflowIdea: return .flow
        case .general, .note, .idea, .link: return nil
        }
    }

    // MARK: - Suggestions

    /// Suggestions for the empty palette: real capabilities, recent commands and
    /// saved flows — never invented examples.
    func suggestions(recent: [String], limit: Int = 8) -> [Suggestion] {
        var items: [Suggestion] = []
        // Flows first: a saved flow is the fastest useful thing to run.
        for flow in store.flows.prefix(2) {
            items.append(Suggestion(text: "Run “\(flow.name)”",
                                    detail: "\(flow.steps.count) steps",
                                    symbol: "arrow.triangle.2.circlepath"))
        }
        // Then the inbox, when it has something — a real count, not a prompt.
        let waiting = capture.unreviewed.count
        if waiting > 0 {
            items.append(Suggestion(
                text: "Show my inbox",
                detail: "\(waiting) waiting for review",
                symbol: "tray.full"))
        }
        for command in recent.prefix(2) {
            items.append(Suggestion(text: command, detail: "Recent", symbol: "clock"))
        }
        let capabilities: [(String, String, String)] = [
            ("Capture this idea: ", "Quick Capture", "tray.and.arrow.down"),
            ("Show my briefing", "Daily Briefing", "sun.max"),
            ("Open Safari", "Application", "app"),
            ("Open my Downloads folder", "Folder", "folder"),
            ("Set the volume to 30 percent", "System", "speaker.wave.2"),
            ("Which app is in the foreground?", "Read", "app.badge"),
            ("Show my saved flows", "Flows", "list.bullet.rectangle"),
        ]
        for (text, detail, symbol) in capabilities
        where !items.contains(where: { $0.text == text }) {
            items.append(Suggestion(text: text, detail: detail, symbol: symbol))
        }
        return Array(items.prefix(limit))
    }

    struct Suggestion: Identifiable, Equatable {
        var id: String { text }
        let text: String
        let detail: String
        let symbol: String
    }

    private static func matches(_ text: String, _ pattern: String) -> Bool {
        text.range(of: pattern, options: [.regularExpression, .caseInsensitive]) != nil
    }
}