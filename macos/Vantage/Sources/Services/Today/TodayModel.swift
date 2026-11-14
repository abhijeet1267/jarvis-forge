import Foundation

/// The Today screen's data, assembled from the sources that already exist.
///
/// Nothing here owns a database. Every row is a *view* of a record that lives
/// somewhere else — Google Calendar, Google Tasks, the local planner, the
/// capture inbox — and keeps the identity it has there (`sourceID`), so an
/// action taken here can be sent back to the system that owns the record.
///
/// The rule the rest of this app keeps re-learning is enforced by the types:
/// **a failed source is not an empty source.** Each source carries its own
/// `SourceState`, so a calendar that could not be read cannot render as
/// "nothing scheduled", and one source failing cannot blank the others.
enum TodayModel {

    // MARK: - Sources

    enum Source: String, CaseIterable, Identifiable, Equatable {
        case calendar, googleTasks, planner, capture, attention

        var id: String { rawValue }

        var title: String {
            switch self {
            case .calendar: return "Calendar"
            case .googleTasks: return "Google Tasks"
            case .planner: return "Planner"
            case .capture: return "Quick Capture"
            case .attention: return "Needs attention"
            }
        }

        var symbol: String {
            switch self {
            case .calendar: return "calendar"
            case .googleTasks: return "checklist"
            case .planner: return "list.bullet.rectangle"
            case .capture: return "tray.and.arrow.down"
            case .attention: return "exclamationmark.triangle"
            }
        }
    }

    /// What one source actually returned. `empty` and `unavailable` are
    /// different values because they are different facts, and the screen has
    /// different things to say about each.
    enum SourceState: Equatable {
        case loading
        case ready
        case empty
        /// The read failed. Carries the reason a person can act on.
        case failed(String)
        /// The source is not connected / not configured. Not an error.
        case unavailable(String)

        var isFailure: Bool {
            if case .failed = self { return true }
            return false
        }

        var isUnavailable: Bool {
            if case .unavailable = self { return true }
            return false
        }

        var isSettled: Bool { self != .loading }

        /// The sentence to show in the source's header, or nil when the state
        /// needs no explanation (`ready` shows its items, `empty` shows its own
        /// short line).
        var explanation: String? {
            switch self {
            case .loading: return nil
            case .ready, .empty: return nil
            case .failed(let reason): return reason
            case .unavailable(let reason): return reason
            }
        }
    }

    // MARK: - Rows

    /// Where a row came from, in enough detail to send an action back.
    enum Origin: Equatable {
        case googleEvent(uid: String)
        case googleTask(id: String, listID: String)
        case plannerTask(ref: String)
        case capture(id: UUID)
        /// An attention item names its own surface; the screen navigates to it
        /// rather than acting on the record itself.
        case attention(surface: String, ref: String)
    }

    struct Row: Identifiable, Equatable {
        let id: String
        let origin: Origin
        let title: String
        let detail: String?
        /// A short right-aligned label: a time, a due date, a state word.
        /// Deliberately a string, because the sources count in different units
        /// (clock time, a date, "overdue by 2 days") and pretending they share
        /// one type would invent precision none of them has.
        let trailing: String?
        /// Tone for the trailing label. `nil` is the ordinary secondary text.
        let emphasis: Emphasis

        enum Emphasis: Equatable { case normal, urgent, done }
    }

    /// One section of the screen: a source, its state, and the rows it produced.
    struct Section: Identifiable, Equatable {
        let source: Source
        var state: SourceState
        var rows: [Row]
        /// The line shown when the source answered successfully with nothing.
        var emptyText: String

        var id: String { source.rawValue }

        init(source: Source, state: SourceState = .loading, rows: [Row] = [],
             emptyText: String) {
            self.source = source
            self.state = state
            self.rows = rows
            self.emptyText = emptyText
        }
    }

    /// Everything the screen draws, in its fixed order. A section is present
    /// even while loading or failed — its absence would be indistinguishable
    /// from "this source does not exist".
    struct Board: Equatable {
        var calendar: Section
        var googleTasks: Section
        var planner: Section
        var capture: Section
        var attention: Section

        static func initial() -> Board {
            Board(
                calendar: Section(source: .calendar,
                                  emptyText: "Nothing scheduled today."),
                googleTasks: Section(source: .googleTasks,
                                     emptyText: "No Google tasks are open."),
                planner: Section(source: .planner,
                                 emptyText: "No planner tasks are open."),
                capture: Section(source: .capture,
                                 emptyText: "Nothing waiting to be reviewed."),
                attention: Section(source: .attention,
                                   emptyText: "Nothing needs your attention."))
        }

        var sections: [Section] { [calendar, googleTasks, planner, capture, attention] }

        /// Sources whose read failed. The screen uses this for one honest
        /// summary line and to decide whether a refresh is the useful action.
        var failedSources: [Source] {
            sections.filter { $0.state.isFailure }.map(\.source)
        }

        var unavailableSources: [Source] {
            sections.filter { $0.state.isUnavailable }.map(\.source)
        }

        var isLoading: Bool { sections.contains { !$0.state.isSettled } }

        /// A single line that names what could not be read — never a claim that
        /// there is nothing to do. Nil when every source answered.
        var failureSummary: String? {
            let failed = failedSources
            guard !failed.isEmpty else { return nil }
            let names = failed.map(\.title)
            if names.count == 1 { return "\(names[0]) could not be read." }
            return "Could not read " + names.dropLast().joined(separator: ", ")
                + " and " + (names.last ?? "") + "."
        }
    }
}