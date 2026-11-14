import Foundation

/// Pure functions that turn backend payloads into Today rows.
///
/// Split out of the view for one reason: the rules that matter here are the
/// ones that silently lie when they are wrong — "is this task due today",
/// "is it overdue", "did this read succeed" — and those need to be testable
/// without a running app or a network. `now` and `calendar` are parameters for
/// the same reason; production passes the person's own clock and calendar, and
/// the tests pass a fixed one.
enum TodayMapping {

    // MARK: - Calendar

    /// Today's events, in the order they happen.
    ///
    /// The distinction this function must preserve: `read_succeeded == true`
    /// with zero events is `.empty` (a real, clear day); anything else is
    /// `.failed` with Google's own sentence. A calendar that could not be read
    /// rendering as a clear day is the specific defect this project keeps
    /// finding, and it is decided here, once.
    static func calendarSection(_ response: APIModels.GoogleEventsResponse?,
                                error: String?) -> TodayModel.Section {
        var section = TodayModel.Section(
            source: .calendar,
            emptyText: "Nothing scheduled today.")

        if let error {
            section.state = .failed(error)
            return section
        }
        guard let response else {
            section.state = .loading
            return section
        }
        guard response.read_succeeded == true else {
            let reason = nonEmpty(response.detail) ?? "Google Calendar could not be read."
            section.state = .failed(reason)
            return section
        }
        let events = response.events ?? []
        section.rows = events.enumerated().map { index, event in
            TodayModel.Row(
                // A Google event's uid is unique; index guards the rare
                // malformed row rather than making two rows share an identity.
                id: "gcal|" + (event.uid.isEmpty ? "\(index)" : event.uid),
                origin: .googleEvent(uid: event.uid),
                title: event.title,
                detail: nonEmpty(event.location),
                trailing: event.all_day == true ? "all day" : clock(event.start),
                emphasis: .normal)
        }
        section.state = section.rows.isEmpty ? .empty : .ready
        return section
    }

    // MARK: - Google Tasks

    struct GoogleTaskSplit: Equatable {
        var dueToday: [TodayModel.Row]
        var overdue: [TodayModel.Row]
        var upcoming: [TodayModel.Row]
        var outstanding: Int { dueToday.count + overdue.count + upcoming.count }
    }

    /// Split open Google tasks into today / overdue / upcoming.
    ///
    /// Google Tasks stores a *date*, no time. That has two consequences this
    /// function takes seriously:
    ///
    /// * a task due today is not "late" at 09:00 — it is due *today*, and the
    ///   comparison is by calendar day, never by instant;
    /// * a task with no due date is not overdue. It is outstanding, and it is
    ///   listed under "upcoming" without a date, because inventing one would
    ///   make the screen say something Google does not.
    static func googleTaskSplit(_ response: APIModels.GoogleTasksResponse?,
                                error: String?,
                                now: Date, calendar: Calendar) -> (section: TodayModel.Section,
                                                                   split: GoogleTaskSplit) {
        var section = TodayModel.Section(
            source: .googleTasks,
            emptyText: "No Google tasks are open.")
        var split = GoogleTaskSplit(dueToday: [], overdue: [], upcoming: [])

        if let error {
            section.state = .failed(error)
            return (section, split)
        }
        guard let response else {
            section.state = .loading
            return (section, split)
        }
        guard response.read_succeeded == true else {
            let reason = nonEmpty(response.detail) ?? "Google Tasks could not be read."
            section.state = .failed(reason)
            return (section, split)
        }

        let today = calendar.startOfDay(for: now)
        let tasks = (response.tasks ?? []).filter { $0.done != true }
        for task in tasks {
            let due = parseDay(task.due, calendar: calendar)
            let dueDay = due.map { calendar.startOfDay(for: $0) }
            let trailing: String?
            var emphasis: TodayModel.Row.Emphasis = .normal
            if let dueDay {
                if dueDay < today {
                    trailing = overdueLabel(from: dueDay, to: today, calendar: calendar)
                    emphasis = .urgent
                } else if dueDay == today {
                    trailing = "today"
                    emphasis = .urgent
                } else {
                    trailing = dayLabel(dueDay, now: today, calendar: calendar)
                }
            } else {
                trailing = nil
            }
            let row = TodayModel.Row(
                id: "gtask|" + task.id,
                origin: .googleTask(id: task.id, listID: task.list_id ?? ""),
                title: task.title,
                detail: nonEmpty(task.notes),
                trailing: trailing,
                emphasis: emphasis)
            if let dueDay {
                if dueDay < today { split.overdue.append(row) }
                else if dueDay == today { split.dueToday.append(row) }
                else { split.upcoming.append(row) }
            } else {
                split.upcoming.append(row)
            }
        }
        // Google returns tasks in list order; the screen reads best by date.
        split.overdue.sort { $0.trailing ?? "" < $1.trailing ?? "" }
        section.rows = split.dueToday + split.overdue + split.upcoming
        section.emptyText = split.outstanding == 0
            ? "No open Google tasks." : ""
        section.state = section.rows.isEmpty ? .empty : .ready
        return (section, split)
    }

    // MARK: - Planner

    static func plannerSection(_ tasks: [APIModels.PlannerTask]?, error: String?,
                               now: Date) -> TodayModel.Section {
        var section = TodayModel.Section(
            source: .planner,
            emptyText: "No planner tasks are open.")
        if let error { section.state = .failed(error); return section }
        guard let tasks else { section.state = .loading; return section }
        let open = tasks.filter { ($0.status ?? "open") == "open" }
        section.rows = open.map { task in
            let overdue = task.overdue == true
            return TodayModel.Row(
                id: "plan|" + task.task_ref,
                origin: .plannerTask(ref: task.task_ref),
                title: task.title,
                detail: nonEmpty(task.project),
                trailing: overdue ? "overdue" : nonEmpty(task.priority),
                emphasis: overdue ? .urgent : .normal)
        }
        // Overdue first, then the rest, so the screen's most actionable rows
        // are at the top of its section.
        section.rows.sort { lhs, rhs in
            (lhs.emphasis == .urgent ? 0 : 1) < (rhs.emphasis == .urgent ? 0 : 1)
        }
        section.state = section.rows.isEmpty ? .empty : .ready
        return section
    }

    // MARK: - Capture

    static func captureSection(_ items: [CaptureItem]?) -> TodayModel.Section {
        var section = TodayModel.Section(
            source: .capture,
            emptyText: "Nothing waiting to be reviewed.")
        guard let items else { section.state = .loading; return section }
        // Only what is still waiting for a person. A converted or archived
        // item has been dealt with, and listing it here would make the inbox
        // look like a second task system.
        section.rows = items.filter(\.isOpen).map { item in
            TodayModel.Row(
                id: "cap|" + item.id.uuidString,
                origin: .capture(id: item.id),
                title: item.headline,
                detail: item.kind.displayName,
                trailing: nil,
                emphasis: .normal)
        }
        section.state = section.rows.isEmpty ? .empty : .ready
        return section
    }

    // MARK: - Attention

    static func attentionSection(_ items: [APIModels.AttentionItem]?, error: String?) -> TodayModel.Section {
        var section = TodayModel.Section(
            source: .attention,
            emptyText: "Nothing needs your attention.")
        if let error { section.state = .failed(error); return section }
        guard let items else { section.state = .loading; return section }
        section.rows = items.enumerated().map { index, item in
            TodayModel.Row(
                id: "att|" + (item.ref ?? "\(index)") + "|" + (item.kind ?? ""),
                origin: .attention(surface: item.surface ?? "", ref: item.ref ?? ""),
                title: item.title,
                detail: nonEmpty(item.detail),
                trailing: nil,
                emphasis: .urgent)
        }
        section.state = section.rows.isEmpty ? .empty : .ready
        return section
    }

    // MARK: - Dates

    /// Google's `due` is a date (sometimes with a timezone suffix). Read the
    /// day, in the person's calendar, or nothing.
    static func parseDay(_ raw: String?, calendar: Calendar) -> Date? {
        guard let raw, !raw.isEmpty else { return nil }
        let dayPart = String(raw.prefix(10))
        let formatter = DateFormatter()
        formatter.calendar = calendar
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = calendar.timeZone
        formatter.dateFormat = "yyyy-MM-dd"
        return formatter.date(from: dayPart)
    }

    private static func dayLabel(_ day: Date, now: Date, calendar: Calendar) -> String {
        let days = calendar.dateComponents([.day], from: now, to: day).day ?? 0
        if days == 1 { return "tomorrow" }
        let formatter = DateFormatter()
        formatter.calendar = calendar
        formatter.locale = Locale.current
        formatter.dateFormat = days < 7 ? "EEE" : "d MMM"
        return formatter.string(from: day)
    }

    static func overdueLabel(from due: Date, to today: Date, calendar: Calendar) -> String {
        let days = calendar.dateComponents([.day], from: due, to: today).day ?? 0
        if days <= 1 { return "overdue by a day" }
        return "overdue by \(days) days"
    }

    /// "HH:mm" in the device's own zone. An unparseable timestamp is shown as
    /// "—" rather than echoed raw: a raw RFC3339 string in a time column reads
    /// as a rendering bug.
    static func clock(_ iso: String?) -> String {
        guard let date = parseInstant(iso) else { return "—" }
        let out = DateFormatter()
        out.dateFormat = "HH:mm"
        return out.string(from: date)
    }

    static func parseInstant(_ iso: String?) -> Date? {
        guard let iso, !iso.isEmpty else { return nil }
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = formatter.date(from: iso) { return date }
        formatter.formatOptions = [.withInternetDateTime]
        if let date = formatter.date(from: iso) { return date }
        // A bare date ("2026-10-30") is midnight local, not an instant error.
        let day = DateFormatter()
        day.locale = Locale(identifier: "en_US_POSIX")
        day.dateFormat = "yyyy-MM-dd"
        return day.date(from: String(iso.prefix(10)))
    }

    static func nonEmpty(_ value: String?) -> String? {
        guard let value else { return nil }
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }
}