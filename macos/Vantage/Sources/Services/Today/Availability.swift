import Foundation

/// Free/busy from events that were actually read.
///
/// The one rule this file exists to enforce: **a failed calendar read is never
/// a free slot.** Every entry point takes the read's outcome alongside the
/// events, and returns `.unknown` when the read failed. There is no code path
/// here that can turn an unreachable Google into "you're free at 3".
///
/// It also refuses to invent working hours. Vantage has no setting for them,
/// so the only honest claim is about the events it can see inside the window
/// the person asked about — stated with its assumptions, clearly.
enum Availability {

    /// The open interval between two events.
    struct Slot: Equatable {
        let start: Date
        let end: Date
        var minutes: Int { Int(end.timeIntervalSince(start) / 60) }
    }

    enum Outcome: Equatable {
        /// The window was read and the answer is known.
        case known(free: [Slot], busy: [Slot])
        /// The read failed. Nothing may be claimed about the person's time.
        case unknown(reason: String)
        /// The window is not something this can answer (bad range).
        case invalid(reason: String)

        var isKnown: Bool { if case .known = self { return true }; return false }
    }

    /// Assumptions stated with every answer, because each one bounds the claim.
    static let assumptions = [
        "Only events on the connected calendar are considered.",
        "The calendar holds no working-hours setting, so this is about free time "
            + "in the window you asked about, not about when you work.",
        "An event with no end time is treated as one hour long."
    ]

    /// Compute free and busy intervals inside `window`.
    ///
    /// `readSucceeded` is the *calendar's* verdict, not this function's. It is
    /// a parameter rather than something inferred from an empty event list,
    /// because an empty list is exactly what a failed read also produces.
    static func within(_ window: DateInterval,
                       readSucceeded: Bool,
                       readFailureReason: String? = nil,
                       events: [APIModels.GoogleEvent]) -> Outcome {
        guard window.duration > 0 else {
            return .invalid(reason: "The end of the window is not after its start.")
        }
        guard readSucceeded else {
            return .unknown(reason: readFailureReason
                ?? "Your calendar could not be read, so I cannot say when you are free.")
        }

        // Convert only events that overlap the window, and clip them to it.
        var busy: [Slot] = []
        for event in events {
            guard let start = TodayMapping.parseInstant(event.start) else { continue }
            let end = TodayMapping.parseInstant(event.end)
                ?? start.addingTimeInterval(3600)
            let clippedStart = max(start, window.start)
            let clippedEnd = min(end, window.end)
            if clippedEnd > clippedStart {
                busy.append(Slot(start: clippedStart, end: clippedEnd))
            }
        }

        // Merge overlaps so a double-booked hour is one busy block, not two
        // that make the free time between them look real.
        busy.sort { $0.start < $1.start }
        var merged: [Slot] = []
        for slot in busy {
            if let last = merged.last, slot.start <= last.end {
                merged[merged.count - 1] = Slot(start: last.start,
                                                end: max(last.end, slot.end))
            } else {
                merged.append(slot)
            }
        }

        var free: [Slot] = []
        var cursor = window.start
        for block in merged {
            if block.start > cursor {
                free.append(Slot(start: cursor, end: block.start))
            }
            cursor = max(cursor, block.end)
        }
        if cursor < window.end {
            free.append(Slot(start: cursor, end: window.end))
        }
        return .known(free: free, busy: merged)
    }

    /// The first free interval of at least `minutes`, or nil when there is none.
    ///
    /// Returns nil for both "no gap that long" and "the read failed" — so the
    /// caller must use `within(...)` when the difference matters. This is the
    /// helper for a sentence like "when am I free for an hour tomorrow".
    static func firstSlot(atLeast minutes: Int, in window: DateInterval,
                          readSucceeded: Bool,
                          events: [APIModels.GoogleEvent]) -> Slot? {
        guard case .known(let free, _) = within(window, readSucceeded: readSucceeded,
                                                events: events) else { return nil }
        return free.first { $0.minutes >= minutes }
    }

    /// A day's window in the person's calendar.
    static func day(_ date: Date, calendar: Calendar) -> DateInterval {
        let start = calendar.startOfDay(for: date)
        return DateInterval(start: start, end: start.addingTimeInterval(86_400))
    }

    /// A sentence that states the answer and its assumptions together.
    static func describe(_ outcome: Outcome, window: DateInterval,
                         calendar: Calendar = .current) -> String {
        switch outcome {
        case .unknown(let reason):
            return reason
        case .invalid(let reason):
            return reason
        case .known(let free, let busy):
            let formatter = DateFormatter()
            formatter.calendar = calendar
            formatter.locale = Locale.current
            formatter.dateFormat = "HH:mm"
            let day = DateFormatter()
            day.calendar = calendar
            day.locale = Locale.current
            day.dateFormat = "EEEE d MMMM"

            var lines: [String] = []
            if busy.isEmpty && free.count == 1 {
                lines.append("You have nothing on \(day.string(from: window.start)) "
                             + "between \(formatter.string(from: window.start)) and "
                             + "\(formatter.string(from: window.end)).")
            } else {
                let blocks = free
                    .filter { $0.minutes >= 15 }
                    .map { "\(formatter.string(from: $0.start))–\(formatter.string(from: $0.end))" }
                if blocks.isEmpty {
                    lines.append("No gap of 15 minutes or more is free in that window.")
                } else {
                    lines.append("Free: " + blocks.joined(separator: ", ") + ".")
                }
                let busyText = busy
                    .map { "\(formatter.string(from: $0.start))–\(formatter.string(from: $0.end))" }
                    .joined(separator: ", ")
                if !busyText.isEmpty { lines.append("Busy: \(busyText).") }
            }
            return lines.joined(separator: " ")
        }
    }
}