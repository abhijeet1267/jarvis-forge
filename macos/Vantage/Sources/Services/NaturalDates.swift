import Foundation

/// The small, closed set of relative dates Vantage will read out of a sentence.
///
/// Deliberately narrow. The brief is explicit that an exact deadline must not be
/// inferred from vague text — "soon", "sometime", "before the review" produce no
/// date at all, and the caller shows "no date" rather than guessing. Only words
/// that name a *day* are accepted, and the result is always a start-of-day
/// anchor a person can see and change before anything is created.
struct NaturalDates {

    /// A parsed date and how sure the parse is.
    struct Match: Equatable {
        let date: Date
        /// True when the sentence named the day outright ("tomorrow", "Friday").
        /// False would mean a guess, and this parser never returns one.
        let explicit: Bool
        /// The words that produced it, so the UI can show "from “tomorrow”".
        let phrase: String
    }

    /// Parse the first recognised day reference in `text`.
    ///
    /// `now` is a parameter so the tests are deterministic; production passes
    /// the current instant. Time zone comes from `calendar` (the person's own),
    /// which is what makes "tomorrow" mean their tomorrow rather than UTC's.
    static func firstMatch(in text: String, now: Date = Date(),
                           calendar: Calendar = .current) -> Match? {
        let lowered = text.lowercased()
        let today = calendar.startOfDay(for: now)

        // Ordered longest-first so "tomorrow morning" beats "tomorrow".
        let relative: [(String, Int, Int)] = [
            ("day after tomorrow", 2, 9),
            ("yesterday morning", -1, 9),
            ("yesterday afternoon", -1, 14),
            ("yesterday evening", -1, 18),
            ("yesterday", -1, 9),
            ("tomorrow morning", 1, 9),
            ("tomorrow afternoon", 1, 14),
            ("tomorrow evening", 1, 18),
            ("tomorrow night", 1, 20),
            ("tomorrow", 1, 9),
            ("tonight", 0, 20),
            ("this evening", 0, 18),
            ("this afternoon", 0, 14),
            ("today", 0, 9),
            ("next week", 7, 9),
            ("in a week", 7, 9),
            ("next month", 30, 9),
        ]
        for (phrase, dayOffset, hour) in relative where lowered.contains(phrase) {
            guard let day = calendar.date(byAdding: .day, value: dayOffset, to: today),
                  let dated = calendar.date(bySettingHour: hour, minute: 0,
                                            second: 0, of: day) else { continue }
            return Match(date: dated, explicit: true, phrase: phrase)
        }

        // "in 3 days"
        if let match = regexFirst(lowered, #"\bin (\d{1,2}) days?\b"#),
           let days = Int(match.value), days > 0, days <= 365,
           let day = calendar.date(byAdding: .day, value: days, to: today),
           let dated = calendar.date(bySettingHour: 9, minute: 0, second: 0, of: day) {
            return Match(date: dated, explicit: true, phrase: match.whole)
        }

        // Weekday names mean the *next* one, never today: "for Friday" said on a
        // Friday is read as the following week, which is the reading that does
        // not put a task due in the past.
        let weekdays = ["sunday", "monday", "tuesday", "wednesday",
                        "thursday", "friday", "saturday"]
        for (index, name) in weekdays.enumerated() {
            let patterns = ["\\bnext \(name)\\b", "\\bon \(name)\\b", "\\b\(name)\\b"]
            for pattern in patterns {
                guard let match = regexFirst(lowered, pattern) else { continue }
                // `Calendar` numbers weekdays from 1 (Sunday); `weekdays` is
                // 0-based, so the +1 is what makes Friday land on Friday rather
                // than Thursday. A weekday named *today* means the next one: a
                // task due today-and-already-past is worse than one due next week.
                let todayNumber = calendar.component(.weekday, from: today)
                var delta = (index + 1 - todayNumber + 7) % 7
                if delta == 0 { delta = 7 }
                guard let day = calendar.date(byAdding: .day, value: delta, to: today),
                      let dated = calendar.date(bySettingHour: 9, minute: 0,
                                                second: 0, of: day) else { continue }
                return Match(date: dated, explicit: true, phrase: match.whole)
            }
        }

        // An explicit calendar date: "on 2026-11-04" or "by 4 November".
        if let match = regexFirst(lowered, #"\b(\d{4}-\d{2}-\d{2})\b"#) {
            let formatter = DateFormatter()
            formatter.dateFormat = "yyyy-MM-dd"
            formatter.calendar = calendar
            formatter.timeZone = calendar.timeZone
            if let day = formatter.date(from: match.value) {
                return Match(date: day, explicit: true, phrase: match.whole)
            }
        }
        let months = ["january", "february", "march", "april", "may", "june", "july",
                      "august", "september", "october", "november", "december"]
        for (index, month) in months.enumerated() {
            let pattern = "\\b(\\d{1,2})\\s+(?:of\\s+)?\(month)\\b|\\b\(month)\\s+(\\d{1,2})\\b"
            guard let match = regexFirst(lowered, pattern) else { continue }
            let numberText = match.groups.compactMap { $0 }.first ?? ""
            guard let dayNumber = Int(numberText), (1...31).contains(dayNumber) else { continue }
            var components = calendar.dateComponents([.year], from: today)
            components.month = index + 1
            components.day = dayNumber
            components.hour = 9
            if let date = calendar.date(from: components) {
                // A month/day already behind us means next year; a date in the
                // past would otherwise be created and immediately read as overdue.
                if date < today,
                   let nextYear = calendar.date(byAdding: .year, value: 1, to: date) {
                    return Match(date: nextYear, explicit: true, phrase: match.whole)
                }
                return Match(date: date, explicit: true, phrase: match.whole)
            }
        }
        return nil
    }

    /// The sentence with the date phrase removed, so the stored title does not
    /// read "Review proposal for tomorrow" when the date is its own field.
    static func withoutDatePhrase(_ text: String, phrase: String) -> String {
        var result = text
        for pattern in ["\\bfor\\s+\(NSRegularExpression.escapedPattern(for: phrase))\\b",
                        "\\bby\\s+\(NSRegularExpression.escapedPattern(for: phrase))\\b",
                        "\\bon\\s+\(NSRegularExpression.escapedPattern(for: phrase))\\b",
                        "\\b\(NSRegularExpression.escapedPattern(for: phrase))\\b"] {
            result = result.replacingOccurrences(of: pattern, with: "",
                                                 options: [.regularExpression, .caseInsensitive])
        }
        return result.trimmingCharacters(in: CharacterSet(charactersIn: " ,.;:"))
    }

    private struct RegexMatch {
        let whole: String
        let groups: [String?]
        var value: String { groups.compactMap { $0 }.first ?? whole }
    }

    private static func regexFirst(_ text: String, _ pattern: String) -> RegexMatch? {
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
        return RegexMatch(whole: String(text[whole]), groups: groups)
    }
}