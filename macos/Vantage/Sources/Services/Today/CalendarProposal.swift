import Foundation

/// Turns a calendar sentence into a *typed proposal* — never directly into a
/// write.
///
/// The brief's order is: parse, validate, show a readable summary, require the
/// applicable confirmation, execute, verify, refresh. This file owns the first
/// three steps for calendar writes. It deliberately cannot execute anything:
/// the value it returns is a `CalendarProposal`, and the only thing that can
/// send one is `TodayView` after a person pressed a button that named the
/// exact event.
///
/// Two failure modes it exists to prevent, both measured in this project:
///
/// * **A guessed time.** "Schedule a review tomorrow" has a date and no hour.
///   Booking 09:00 because something has to be chosen puts a real meeting at a
///   time the person never said. That is a `.needsDetail` answer, not a write.
/// * **A lost date.** "Schedule a review at 10 AM" has an hour and no day in a
///   sentence spoken on Monday could mean tomorrow, or today. Ambiguity is
///   resolved by asking, not by picking the reading that happens to be easier.
enum CalendarProposal {

    /// The outcome of understanding a sentence.
    enum Result: Equatable {
        /// Everything needed to create the event is present and unambiguous.
        case ready(CalendarProposal)
        /// A calendar write was requested but a fact is missing or ambiguous.
        /// The question says exactly what is missing; it is never a generic
        /// "I didn't understand".
        case needsDetail(question: String)
        /// Not a calendar-write request.
        case notACalendarRequest
    }

    // MARK: - The proposal

    /// What the person will see before anything is written.
    struct CalendarProposal: Equatable {
        let title: String
        let start: Date
        let durationMinutes: Int
        let location: String
        /// The words the date and time came from, so the summary can show its
        /// own reasoning instead of asking to be trusted.
        let datePhrase: String
        let timePhrase: String

        var end: Date { start.addingTimeInterval(TimeInterval(durationMinutes * 60)) }

        /// A sentence a person can check against what they meant.
        func summary(calendar: Calendar = .current) -> String {
            let day = DateFormatter()
            day.calendar = calendar
            day.locale = Locale.current
            day.dateFormat = "EEEE d MMMM"
            let clock = DateFormatter()
            clock.calendar = calendar
            clock.locale = Locale.current
            clock.dateFormat = "HH:mm"
            let until = DateFormatter()
            until.calendar = calendar
            until.locale = Locale.current
            until.dateFormat = "HH:mm"
            var text = "\(title) — \(day.string(from: start)), "
                + "\(clock.string(from: start))–\(until.string(from: end))"
            if !location.isEmpty { text += " at \(location)" }
            text += ". (\(datePhrase) + \(timePhrase))"
            return text
        }
    }

    // MARK: - Reading

    /// Whether the sentence is asking to put something *in* the calendar.
    ///
    /// Narrow on purpose. "What is on my calendar" is a read; "add a task" is
    /// not a calendar write even though it has a time in it.
    static func isScheduleRequest(_ text: String) -> Bool {
        let lowered = text.lowercased()
        if lowered.contains("task") { return false }
        let verbs = #"\b(schedule|book|add|create|set up|arrange|put|plan)\b"#
        let nouns = #"\b(meeting|event|call|appointment|invite|calendar|1:1|one-on-one|sync)\b"#
        return matches(lowered, verbs) && matches(lowered, nouns)
    }

    /// Parse a schedule sentence.
    ///
    /// `now` and `calendar` are parameters so the tests can fix a Monday at a
    /// known hour and assert on the exact instants produced.
    static func interpret(_ text: String, now: Date = Date(),
                          calendar: Calendar = .current) -> Result {
        guard isScheduleRequest(text) else { return .notACalendarRequest }

        let time = parseTime(text)
        let date = NaturalDates.firstMatch(in: text, now: now, calendar: calendar)

        guard let time else {
            return .needsDetail(question:
                "What time should it start? Say the hour too — for example "
                + "“schedule a review tomorrow at 10 AM”.")
        }
        guard let date else {
            return .needsDetail(question:
                "Which day? I heard \(time.phrase) but no date — for example "
                + "“schedule a review tomorrow at \(time.displayPhrase)”.")
        }

        // A bare hour with no day is already covered by the date requirement.
        // What remains is a date in the past: creating it would quietly put an
        // event where the person cannot see it.
        let start = combine(day: date.date, hour: time.hour, minute: time.minute,
                            calendar: calendar)
        guard start > now else {
            return .needsDetail(question:
                "\(date.phrase.capitalized) is already past. Which upcoming day did you mean?")
        }

        let title = title(from: text, datePhrase: date.phrase, timePhrase: time.phrase)
        guard !title.isEmpty else {
            return .needsDetail(question:
                "What should I call it? For example: “schedule a project review "
                + "\(date.phrase) at \(time.displayPhrase)”.")
        }
        return .ready(CalendarProposal(
            title: title,
            start: start,
            durationMinutes: duration(from: text),
            location: location(from: text),
            datePhrase: date.phrase,
            timePhrase: time.phrase))
    }

    /// The title, with the date and time phrases and the scheduling verb
    /// removed: "schedule a review meeting tomorrow at 10 AM" -> "review".
    ///
    /// "meeting" is dropped only when something else remains. "Schedule a
    /// meeting tomorrow at 10" should become "Meeting" — an event called
    /// "a" would be worse than one called "Meeting".
    static func title(from text: String, datePhrase: String, timePhrase: String) -> String {
        var value = text
        for phrase in [datePhrase, timePhrase] {
            let escaped = NSRegularExpression.escapedPattern(for: phrase)
            for prefix in ["\\bfor\\s+", "\\bby\\s+", "\\bon\\s+", "\\bat\\s+", "\\b"] {
                value = value.replacingOccurrences(
                    of: prefix + escaped, with: "",
                    options: [.regularExpression, .caseInsensitive])
            }
        }
        value = value.replacingOccurrences(
            of: #"^\s*(please\s+)?(schedule|book|add|create|set up|arrange|put|plan)\b"#,
            with: "", options: [.regularExpression, .caseInsensitive])
        value = value.replacingOccurrences(
            of: #"\b(to|in|on)\s+(my|the)\s+calendar\b"#,
            with: "", options: [.regularExpression, .caseInsensitive])
        value = value.replacingOccurrences(of: #"\s{2,}"#,
                                           with: " ", options: [.regularExpression])
        var trimmed = value.trimmingCharacters(in: CharacterSet(charactersIn: " ,.;:-"))
        // Drop a leading article and the generic noun only if a real name is
        // left behind.
        let stripped = trimmed.replacingOccurrences(
            of: #"^(a|an|the)\s+"#, with: "", options: [.regularExpression, .caseInsensitive])
        let withoutNoun = stripped.replacingOccurrences(
            of: #"\b(meeting|event|appointment)\s*$"#,
            with: "", options: [.regularExpression, .caseInsensitive])
            .trimmingCharacters(in: CharacterSet(charactersIn: " ,.;:-"))
        if !withoutNoun.isEmpty { trimmed = withoutNoun }
        else if !stripped.isEmpty { trimmed = stripped }
        // Capitalise the first letter for display; the stored title is the
        // person's own words, so nothing else is rewritten.
        guard let first = trimmed.first else { return "" }
        return String(first).uppercased() + trimmed.dropFirst()
    }

    // MARK: - Pieces

    struct TimeMatch: Equatable {
        let hour: Int       // 24-hour
        let minute: Int
        let phrase: String
        /// The clock time as the person would write it, for suggestion text.
        var displayPhrase: String {
            let formatter = DateFormatter()
            formatter.locale = Locale(identifier: "en_US_POSIX")
            formatter.dateFormat = "h:mm a"
            var components = DateComponents()
            components.hour = hour; components.minute = minute
            let calendar = Calendar(identifier: .gregorian)
            let date = calendar.date(from: components) ?? Date()
            return formatter.string(from: date)
        }
    }

    /// A clock time from ordinary English: "at 10 AM", "3pm", "at 09:30",
    /// "at 17:00", "half past two" is deliberately *not* supported — an
    /// unsupported phrasing asks, which is safer than a guess.
    static func parseTime(_ text: String) -> TimeMatch? {
        let lowered = text.lowercased()

        // "3:30 pm", "17:45"
        if let match = firstMatch(lowered,
            #"\b(\d{1,2}):(\d{2})\s*(am|pm)?\b"#),
           let hour = Int(match.groups[0] ?? ""),
           let minute = Int(match.groups[1] ?? ""),
           (0...23).contains(hour), (0...59).contains(minute) {
            // A 12-hour clock with no meridiem is ambiguous above 12 only;
            // below it, "3:30" at 15:30 and 03:30 are different events, so an
            // hour under 8 with no meridiem is read as ambiguous rather than
            // guessed at.
            let meridiem = (match.groups[2] ?? "").lowercased()
            let resolved = applyMeridiem(hour: hour, meridiem: meridiem)
            guard let resolved else { return nil }
            return TimeMatch(hour: resolved, minute: minute, phrase: match.whole)
        }

        // "10 am", "3pm", "at 9 o'clock"
        if let match = firstMatch(lowered, #"\b(\d{1,2})\s*(am|pm)\b"#),
           let hour = Int(match.groups[0] ?? "") {
            let meridiem = (match.groups[1] ?? "").lowercased()
            guard (1...12).contains(hour),
                  let resolved = applyMeridiem(hour: hour, meridiem: meridiem) else { return nil }
            return TimeMatch(hour: resolved, minute: 0, phrase: match.whole)
        }

        // "at 10" with no meridiem: only a 24-hour reading is safe when the
        // hour is 13 or above; anything lower is a guess between two very
        // different times of day, so it is refused and the caller asks.
        if let match = firstMatch(lowered, #"\bat\s+(\d{1,2})\b"#),
           let hour = Int(match.groups[0] ?? ""), (13...23).contains(hour) {
            return TimeMatch(hour: hour, minute: 0, phrase: match.whole)
        }
        return nil
    }

    private static func applyMeridiem(hour: Int, meridiem: String) -> Int? {
        switch meridiem {
        case "am":
            guard (1...12).contains(hour) else { return nil }
            return hour == 12 ? 0 : hour
        case "pm":
            guard (1...12).contains(hour) else { return nil }
            return hour == 12 ? 12 : hour + 12
        default:
            // No meridiem. Below 13 this is genuinely ambiguous ("at 10" could
            // be 10:00 or 22:00), so refuse; 13–23 is unambiguous.
            return (13...23).contains(hour) ? hour : nil
        }
    }

    /// A duration named in the sentence, else one hour. "for 30 minutes",
    /// "for 2 hours", "a 45-minute review".
    static func duration(from text: String) -> Int {
        let lowered = text.lowercased()
        if let match = firstMatch(lowered, #"\b(?:for|lasts?|duration)\s*(\d{1,3})\s*(min|mins|minutes?)\b"#),
           let minutes = Int(match.groups[0] ?? ""), (5...600).contains(minutes) {
            return minutes
        }
        if let match = firstMatch(lowered, #"\b(\d{1,3})[- ]?minute\b"#),
           let minutes = Int(match.groups[0] ?? ""), (5...600).contains(minutes) {
            return minutes
        }
        if let match = firstMatch(lowered, #"\b(?:for|lasts?|duration)\s*(\d{1,2})\s*(h|hrs?|hours?)\b"#),
           let hours = Int(match.groups[0] ?? ""), (1...10).contains(hours) {
            return hours * 60
        }
        return 60
    }

    /// "in the Design room", "at CUBE84" — the words after a locative, when
    /// they are not a clock time.
    static func location(from text: String) -> String {
        let lowered = text.lowercased()
        guard let match = firstMatch(lowered, #"\b(?:at|in)\s+((?:the\s+)?[a-z0-9][a-z0-9 ’'\-&.]{2,40})"#) else {
            return ""
        }
        let candidate = (match.groups[0] ?? "").trimmingCharacters(in: .whitespaces)
        // A clock time is a time, not a place; and a common scheduling word is
        // not a room.
        let stop = ["am", "pm", "today", "tomorrow", "tonight", "the morning",
                    "the afternoon", "the evening", "my calendar", "the calendar",
                    "least", "all", "once", "the same time"]
        if stop.contains(candidate.lowercased()) { return "" }
        if candidate.first?.isNumber == true { return "" }
        if parseTime("at " + candidate) != nil { return "" }
        return candidate
    }

    private static func combine(day: Date, hour: Int, minute: Int,
                                calendar: Calendar) -> Date {
        let components = calendar.dateComponents([.year, .month, .day], from: day)
        var combined = DateComponents()
        combined.year = components.year
        combined.month = components.month
        combined.day = components.day
        combined.hour = hour
        combined.minute = minute
        return calendar.date(from: combined) ?? day
    }

    // MARK: - Regex

    static func matches(_ text: String, _ pattern: String) -> Bool {
        text.range(of: pattern, options: [.regularExpression, .caseInsensitive]) != nil
    }

    private struct Match {
        let whole: String
        let groups: [String?]
    }

    private static func firstMatch(_ text: String, _ pattern: String) -> Match? {
        guard let regex = try? NSRegularExpression(pattern: pattern, options: [.caseInsensitive]),
              let match = regex.firstMatch(in: text, range: NSRange(text.startIndex..., in: text)),
              let whole = Range(match.range, in: text) else { return nil }
        var groups: [String?] = []
        if match.numberOfRanges > 1 {
            for index in 1..<match.numberOfRanges {
                if let range = Range(match.range(at: index), in: text) {
                    groups.append(String(text[range]))
                } else {
                    groups.append(nil)
                }
            }
        }
        return Match(whole: String(text[whole]), groups: groups)
    }
}