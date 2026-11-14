import Foundation

/// Turns a typed request into a Mac action, or says that it is not one.
///
/// Why a rule router beside the model planner: the model can propose *what to
/// do*, but it must never produce the executable step. This maps a sentence to
/// one of the typed `MacActionKind` cases with named arguments, and hands the
/// result to `MacActionRouter` for validation. Anything this cannot map with
/// confidence returns `nil` (not a guess), so Command Center falls through to
/// the ordinary conversational path and the person gets an answer instead of a
/// surprise.
///
/// It also draws the line the brief calls out: "how do I open Safari" is a
/// question, not a command, and asking for instructions must not launch
/// anything.
struct MacCommandParser {

    struct Step: Equatable {
        let kind: MacActionKind
        let arguments: MacActionArguments
    }

    enum Outcome: Equatable {
        case action(MacActionKind, MacActionArguments)
        /// A sequence the person asked for, in the order they said it.
        case plan([Step])
        /// Instruction-seeking ("how do I…"). Never executed.
        case explanation
        /// Understood as a Mac request but missing a target; the string is the
        /// clarification to ask.
        case clarification(String)
        /// Not a Mac-control request.
        case notMacControl
    }

    private let applications: [String]

    /// `applications` are the installed names, so "open Numbers" can be
    /// recognised even though Numbers is not in a hard-coded vocabulary.
    init(applications: [String] = []) {
        self.applications = applications
    }

    // MARK: - Entry point

    func parse(_ raw: String) -> Outcome {
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return .notMacControl }
        let lowered = text.lowercased()

        // 1. Instruction-seeking wins over everything. "How do I set the
        //    volume", "what's the command to…", "explain how to close a window"
        //    are questions, and acting on them would be the worst kind of
        //    over-eagerness.
        if Self.isInstructionSeeking(lowered) {
            return Self.mentionsMacSubject(lowered) ? .explanation : .notMacControl
        }

        // 2. A sequence ("open Safari, go to example.com, and lower the volume
        //    to 30 percent") is split on the conjunctions people actually say,
        //    and each clause is parsed on its own. If every clause is a clear
        //    action the plan is returned; if any is not, this is not something
        //    to guess at.
        let clauses = Self.splitSequence(text)
        if clauses.count > 1 {
            var steps: [Step] = []
            for clause in clauses {
                if case .action(let kind, let args) = parseSingle(clause) {
                    steps.append(Step(kind: kind, arguments: args))
                } else {
                    return .notMacControl
                }
            }
            return steps.isEmpty ? .notMacControl : .plan(steps)
        }

        return parseSingle(text)
    }

    // MARK: - One clause

    private func parseSingle(_ raw: String) -> Outcome {
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        let lowered = text.lowercased()
        var args = MacActionArguments()

        // --- volume -------------------------------------------------------
        if let percent = Self.volumePercent(in: lowered) {
            args.percent = percent
            return .action(.setVolume, args)
        }
        if Self.matches(lowered, #"\b(mute|silence)\b"#) && !Self.matches(lowered, #"\bunmute"#) {
            args.flag = true
            return .action(.setMuted, args)
        }
        if Self.matches(lowered, #"\bunmute\b"#) {
            args.flag = false
            return .action(.setMuted, args)
        }
        if Self.matches(lowered, #"\b(what('s| is)? (the )?volume|volume level|how loud)\b"#) {
            return .action(.getVolume, args)
        }

        // --- clipboard -----------------------------------------------------
        if Self.matches(lowered, #"\b(what('s| is)? (on )?(in )?(the )?clipboard|read (the )?clipboard|paste (it|that) here)\b"#) {
            return .action(.readClipboard, args)
        }
        if let text = Self.quoted(in: raw),
           Self.matches(lowered, #"\b(copy|put)\b"#),
           Self.matches(lowered, #"\b(clipboard|pasteboard)\b"#) {
            args.text = text
            return .action(.writeClipboard, args)
        }

        // --- notifications --------------------------------------------------
        if let text = Self.quoted(in: raw),
           Self.matches(lowered, #"\b(notify|notification|alert me)\b"#) {
            args.text = text
            return .action(.postNotification, args)
        }

        // --- date and time ---------------------------------------------------
        if Self.matches(lowered, #"\b(what('s| is)? the (date|time)|what time is it|current (date|time)|today'?s date)\b"#) {
            return .action(.currentTime, args)
        }
        if Self.matches(lowered, #"\b(what|which) (displays?|monitors?|screens?)\b"#) {
            return .action(.displays, args)
        }
        if Self.matches(lowered, #"\b(audio (devices?|output)|sound output|what('s| is) (my )?(audio|sound) (output|device))\b"#) {
            return .action(.audioDevices, args)
        }

        // --- files: move / rename / trash -----------------------------------
        // These are deliberately the last file rules: "open the file" and
        // "find the file" must win over them, and a sentence that names an
        // action on a path must still resolve that path through the guard.
        if let pair = Self.movePair(in: raw),
           Self.matches(lowered, #"\b(move|relocate)\b"#) {
            args.path = pair.from
            args.destination = pair.to
            return .action(.moveFile, args)
        }
        if let pair = Self.renamePair(in: raw) {
            args.path = pair.from
            args.newName = pair.to
            return .action(.renameFile, args)
        }
        if let path = Self.quoted(in: raw), Self.matches(lowered, #"\b(delete|remove|trash)\b"#) {
            args.path = path
            return .action(.deleteFile, args)
        }

        // --- read-only system questions ------------------------------------
        if Self.matches(lowered, #"\b(which|what) app (is|was) (in the )?(foreground|front)\b"#)
            || Self.matches(lowered, #"\b(frontmost|current) app(lication)?\b"#) {
            return .action(.frontmostApp, args)
        }
        if Self.matches(lowered, #"\b(what('s| is)? running|running apps|applications? (are )?running|show (me )?(the )?running|what('s| is) open|applications currently running|apps currently running)\b"#) {
            return .action(.listRunningApps, args)
        }
        if Self.matches(lowered, #"\b(screen (size|resolution)|display (size|information)|system info|how much battery|battery level)\b"#) {
            return .action(.systemInfo, args)
        }

        // --- windows --------------------------------------------------------
        let windowVerb = Self.matches(lowered, #"\b(minimi[sz]e|minimi[sz]ed)\b"#)
        let closeVerb = Self.matches(lowered, #"\b(close|shut)\b"#) && Self.matches(lowered, #"\bwindow\b"#)
        let arrangeVerb = Self.matches(lowered, #"\b(move|snap|put)\b.*\bwindow\b"#)
            || Self.matches(lowered, #"\bwindow\b.*\b(left|right)( half| side)?\b"#)
        let listVerb = Self.matches(lowered, #"\b(list|show)\b.*\bwindows\b"#)
        if windowVerb || closeVerb || arrangeVerb || listVerb {
            args.application = Self.applicationMention(in: text, known: applications)
            if let side = Self.side(in: lowered) { args.side = side }
            if let title = Self.windowTitle(in: text) { args.windowTitle = title }
            if listVerb { return .action(.listWindows, args) }
            if windowVerb { return .action(.minimizeWindow, args) }
            if closeVerb { return .action(.closeWindow, args) }
            return .action(.arrangeWindow, args)
        }

        // --- URLs -----------------------------------------------------------
        if let url = Self.url(in: text) {
            // "open X in Safari" names a browser; keep it as the target of the
            // open rather than silently reopening in the default browser.
            args.url = url
            return .action(.openURL, args)
        }

        // --- files and folders ----------------------------------------------
        if Self.matches(lowered, #"\bopen\b.*\b(downloads|documents|desktop|pictures|music|movies|folder)\b"#) {
            args.path = Self.folderName(in: lowered)
            if args.path.isEmpty {
                return .clarification("Which folder should I open?")
            }
            return .action(.openFolder, args)
        }

        // --- applications ----------------------------------------------------
        let mentioned = Self.applicationMention(in: text, known: applications)
        if !mentioned.isEmpty {
            let name = mentioned
            if Self.matches(lowered, #"\b(quit|close|exit)\b"#) {
                args.application = name
                return .action(.quitApplication, args)
            }
            if Self.matches(lowered, #"\b(switch|go|bring|focus|activate|jump)\b"#) {
                args.application = name
                return .action(.activateApplication, args)
            }
            if Self.matches(lowered, #"^(please\s+)?(open|launch|start|run|fire up)\b"#) {
                args.application = name
                return .action(.openApplication, args)
            }
            // "Safari" alone after an imperative verb was handled above; a bare
            // name is ambiguous between launch and switch, so ask.
            if Self.matches(lowered, #"^(please\s+)?(open|launch|start|run)?\s*(the\s+)?"#
                             + NSRegularExpression.escapedPattern(for: name.lowercased()) + "$") {
                args.application = name
                return .action(.openApplication, args)
            }
        }

        // An imperative open/launch/switch with a *name* that is not installed
        // is still a Mac request: say so rather than handing it to the model to
        // explain a failure. The executor will report "not installed".
        if let target = Self.imperativeTarget(in: text) {
            args.application = target
            if Self.matches(lowered, #"\b(quit|exit)\b"#) { return .action(.quitApplication, args) }
            if Self.matches(lowered, #"\b(switch|bring|focus|activate)\b"#) {
                return .action(.activateApplication, args)
            }
            return .action(.openApplication, args)
        }

        return .notMacControl
    }

    // MARK: - Recognisers

    private static func matches(_ text: String, _ pattern: String) -> Bool {
        text.range(of: pattern, options: [.regularExpression, .caseInsensitive]) != nil
    }

    static func isInstructionSeeking(_ lowered: String) -> Bool {
        // "tell me how to …" puts the verb first, so it needs its own branch;
        // anchoring only on `^(how|what|explain)` missed it and a question was
        // then treated as a command to switch apps.
        matches(lowered, #"^(how (do|can|would|should) (i|you)|how to\b|what('s| is) the (command|way|best way)|explain\b|tell me how|where (do|can) i\b|is there a way)"#)
            || matches(lowered, #"\btell me how\b|\bshow me how\b|\bteach me how\b"#)
            || matches(lowered, #"^(what does|why does|what happens if)\b"#)
    }

    private static func mentionsMacSubject(_ lowered: String) -> Bool {
        // The subject list must cover the verbs a Mac question is about ("switch
        // apps", "close my windows", "change the volume"), or a real question
        // falls through to the conversation and reads as "I can't".
        matches(lowered, #"\b(window|windows|volume|safari|finder|app|apps|application|applications|folder|file|mac|screen|minimi[sz]e|maximi[sz]e|open|switch|close|quit|launch|mute|desktop)\b"#)
    }

    /// Split on commas, semicolons and "and"/"then" when each side has a verb.
    ///
    /// Deliberately conservative: a sentence is only treated as a sequence when
    /// every resulting clause parses as its own action, so a false split cannot
    /// produce a half-executed plan.
    static func splitSequence(_ text: String) -> [String] {
        let parts = text
            .replacingOccurrences(of: #"\band then\b"#, with: "|", options: [.regularExpression, .caseInsensitive])
            .replacingOccurrences(of: #"[,;]"#, with: "|", options: .regularExpression)
            .components(separatedBy: "|")
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
        return parts.count > 1 ? parts : [text]
    }

    static func volumePercent(in lowered: String) -> Int? {
        // "set the volume to 30 percent" / "lower the volume to 30%" / "volume 20"
        let patterns = [
            #"volume\D{0,20}?(\d{1,3})\s*(%|percent)"#,
            #"(\d{1,3})\s*(%|percent)\s*(volume|sound)"#,
            #"volume\b[^\d]{0,20}(\d{1,3})\b"#,
        ]
        for pattern in patterns {
            if let regex = try? NSRegularExpression(pattern: pattern, options: [.caseInsensitive]),
               let match = regex.firstMatch(in: lowered, range: NSRange(lowered.startIndex..., in: lowered)),
               match.numberOfRanges > 1,
               let range = Range(match.range(at: 1), in: lowered),
               let value = Int(lowered[range]), (0...100).contains(value) {
                return value
            }
        }
        return nil
    }

    static func side(in lowered: String) -> MacWindowSide? {
        if matches(lowered, #"\b(maximi[sz]e|full screen|fill the screen)\b"#) { return .maximise }
        if matches(lowered, #"\bleft\b"#) { return .left }
        if matches(lowered, #"\bright\b"#) { return .right }
        return nil
    }

    static func windowTitle(in text: String) -> String? {
        // `titled "X"` / `called X` is how a person names one window.
        let pattern = #"(?:titled|called|named)\s+["“]([^"”]+)["”]|(?:titled|called|named)\s+([A-Za-z0-9 ._\-]{2,40})$"#
        guard let regex = try? NSRegularExpression(pattern: pattern, options: [.caseInsensitive]),
              let match = regex.firstMatch(in: text, range: NSRange(text.startIndex..., in: text))
        else { return nil }
        for index in 1..<match.numberOfRanges {
            if let range = Range(match.range(at: index), in: text) {
                let value = text[range].trimmingCharacters(in: .whitespaces)
                if !value.isEmpty { return value }
            }
        }
        return nil
    }

    static func url(in text: String) -> String? {
        let pattern = #"(?:https?://)?(?:www\.)?([a-z0-9\-]+(?:\.[a-z0-9\-]+)+)(/[^\s]*)?(?=\s|$|\.)"#
        guard let regex = try? NSRegularExpression(pattern: pattern, options: [.caseInsensitive]),
              let match = regex.firstMatch(in: text, range: NSRange(text.startIndex..., in: text)),
              let hostRange = Range(match.range(at: 1), in: text)
        else { return nil }
        let host = String(text[hostRange]).lowercased()
        // A bare word with a dot is only a URL inside an open/go-to verb, so
        // "open project.notes" does not become a website.
        let looksImperative = matches(text.lowercased(),
                                      #"\b(open|go to|visit|navigate|browse|load)\b"#)
        guard looksImperative else { return nil }
        // Known TLD-ish guard: require a real domain suffix so "invoice.pdf" in
        // a sentence never turns into a browse.
        let suffixes = [".com", ".org", ".net", ".io", ".dev", ".co", ".ai", ".app",
                        ".edu", ".gov", ".uk", ".in", ".me", ".info"]
        guard suffixes.contains(where: { host.hasSuffix($0) }) else { return nil }
        var value = "https://" + host
        if let pathRange = Range(match.range(at: 2), in: text) {
            value += text[pathRange]
        }
        return value
    }

    static func folderName(in lowered: String) -> String {
        for name in ["downloads", "documents", "desktop", "pictures", "music", "movies"] {
            if lowered.contains(name) { return name.capitalized }
        }
        return ""
    }

    /// The first installed application the sentence names.
    static func applicationMention(in text: String, known: [String]) -> String {
        let lowered = text.lowercased()
        // Longest first, so "Google Chrome" wins over a hypothetical "Chrome".
        for name in known.sorted(by: { $0.count > $1.count }) {
            let pattern = "\\b" + NSRegularExpression.escapedPattern(for: name.lowercased()) + "\\b"
            if lowered.range(of: pattern, options: .regularExpression) != nil {
                return name
            }
        }
        return ""
    }

    /// "open Foo" / "launch Foo" when Foo is not a known installed app.
    static func imperativeTarget(in text: String) -> String? {
        let pattern = #"^(?:please\s+)?(?:open|launch|start|switch to|bring up|fire up)\s+(?:the\s+)?(.{2,40})$"#
        guard let regex = try? NSRegularExpression(pattern: pattern, options: [.caseInsensitive]),
              let match = regex.firstMatch(in: text, range: NSRange(text.startIndex..., in: text)),
              let range = Range(match.range(at: 1), in: text)
        else { return nil }
        let target = String(text[range]).trimmingCharacters(in: .whitespaces)
        // Do not treat a folder or a web address as an application.
        if target.lowercased().contains("folder") || target.contains(".") && !target.contains(" ") {
            return nil
        }
        return target.isEmpty ? nil : target
    }
    // MARK: - Quoted values and file-operation pairs

    /// The first quoted or backticked span in a sentence, or nil.
    static func quoted(in text: String) -> String? {
        for pattern in [#""([^"]{1,300})""#, #"'([^']{1,300})'"#, #"`([^`]{1,300})`"#,
                        #"“([^”]{1,300})”"#] {
            if let regex = try? NSRegularExpression(pattern: pattern),
               let found = regex.firstMatch(in: text,
                                            range: NSRange(text.startIndex..., in: text)),
               found.numberOfRanges > 1,
               let r = Range(found.range(at: 1), in: text) {
                let match = String(text[r])
                if !match.isEmpty { return match }
            }
        }
        return nil
    }

    /// "move “A” to “B”" — two quoted values, from → to.
    static func movePair(in text: String) -> (from: String, to: String)? {
        let values = allQuoted(in: text)
        guard values.count >= 2 else { return nil }
        return (values[0], values[1])
    }

    /// "rename “A” to “B”" — the same shape, a name rather than a path.
    static func renamePair(in text: String) -> (from: String, to: String)? {
        guard matches(text.lowercased(), #"\brenam(e|ing)\b"#) else { return nil }
        return movePair(in: text)
    }

    static func allQuoted(in text: String) -> [String] {
        var found: [String] = []
        let regex = try? NSRegularExpression(
            pattern: #""([^"]{1,300})"|'([^']{1,300})'|`([^`]{1,300})`|“([^”]{1,300})”"#)
        let range = NSRange(text.startIndex..., in: text)
        regex?.enumerateMatches(in: text, range: range) { match, _, _ in
            guard let match else { return }
            for index in 1..<match.numberOfRanges {
                if let r = Range(match.range(at: index), in: text) {
                    found.append(String(text[r]))
                    break
                }
            }
        }
        return found
    }

}