import Foundation

/// The daily briefing, assembled from sources Vantage can actually read.
///
/// The design rule that matters most: a section that failed to load is a
/// *failure*, never an empty one. "No events today" and "I could not read your
/// calendar" look identical on a busy person's screen and mean opposite things,
/// so `BriefingSection.state` carries three values and the UI draws all three
/// differently.
struct Briefing {

    /// One section's fetch state, so failure cannot be rendered as emptiness.
    enum State: Equatable {
        case loaded
        case failed(String)
        /// The source is not configured at all (no calendar connected). Distinct
        /// from both: there is nothing to read, and nothing went wrong.
        case unavailable(String)
    }

    struct Section<Item>: Identifiable {
        let id: String
        let title: String
        let symbol: String
        var state: State
        var items: [Item]
        var emptyMessage: String
        /// When this section's data was last successfully read.
        var refreshedAt: Date?

        var isLoaded: Bool { state == .loaded }
    }

    /// A briefing card. Every action is optional and points at a real handler;
    /// a card with no action is read-only rather than a disguised button.
    struct Card: Identifiable {
        enum Kind: String {
            case event, taskDue, taskOverdue, taskUpcoming, captureAwaitingReview
            case flowFailure, scheduleFailure, approval, providerIssue
        }
        enum Action: Equatable {
            case openTask(String)            // task_ref
            case completeTask(String)        // task_ref (confirms first)
            case openCapture(UUID)
            case openFlow(UUID)              // flow id, to its history
            case openFlowHistory
            case openScreen(NavItem)
        }

        let id: String
        let kind: Kind
        let title: String
        let detail: String
        var dateLabel: String = ""
        var action: Action?
        var isSuggestion = false
    }

    let generatedAt: Date
    let day: String
    var sections: [Section<Card>]
    /// Sources that could not be read at all, for the honest footer.
    var failedSources: [String]
    /// Sources that are not configured, for the same footer.
    var unavailableSources: [String]
    /// The short paragraph at the top; suggestions are labelled as suggestions.
    var headline: String
    var suggestions: [String]
    /// True when nothing at all could be read — the screen must say so rather
    /// than show a serene empty day.
    var allSourcesFailed: Bool

    var totalCards: Int { sections.reduce(0) { $0 + $1.items.count } }
}

/// Builds a briefing from the pieces the app already fetches.
///
/// Deliberately not a network aggregator of its own: the view passes in what it
/// read (often reusing Home/Planner's data), and this turns it into sections.
/// That keeps one fetch per source and makes the whole thing testable with plain
/// values — no server, no clock, no time zone beyond the one passed in.
struct BriefingBuilder {

    /// Everything the briefing was able to read, plus what failed.
    struct Inputs {
        var agenda: APIModels.PlannerAgenda?
        var agendaError: String?
        var tasks: [APIModels.PlannerTask]?
        var tasksError: String?
        var captureItems: [CaptureItem]?
        var flowHistory: [FlowRun]?
        var schedules: [APIModels.ScheduledJob]?
        var schedulesError: String?
        var approvals: [APIModels.Confirmation]?
        var approvalsError: String?
        var providerIssue: String?
        var calendarUnavailable: String?

        init() {}
    }

    /// Aggregate.
    ///
    /// `now` and `calendar` are parameters so date boundaries are testable: the
    /// same inputs at 23:59 and 00:01 must produce different "today" sections.
    func build(_ inputs: Inputs, now: Date = Date(),
               calendar: Calendar = .current,
               staleAfter: TimeInterval = 15 * 60) -> Briefing {
        var sections: [Briefing.Section<Briefing.Card>] = []
        var failed: [String] = []
        var unavailable: [String] = []

        let startOfToday = calendar.startOfDay(for: now)
        let endOfToday = calendar.date(byAdding: .day, value: 1, to: startOfToday)

        // --- Today's schedule ------------------------------------------------
        // `/planner/today` carries the calendar events and its own
        // `calendar_read_failed` flag. That flag is the difference between
        // "nothing scheduled" and "the calendar could not be read", and it is
        // honoured rather than folded into an empty list.
        var eventCards: [Briefing.Card] = []
        var eventState: Briefing.State = .loaded
        if let agenda = inputs.agenda {
            if agenda.calendar_read_failed == true {
                eventState = .failed("Your calendar could not be read, so this is not an empty day.")
                failed.append("Calendar")
            } else {
                for raw in agenda.events ?? [] {
                    guard let event = Self.event(from: raw) else { continue }
                    eventCards.append(Briefing.Card(
                        id: "event-\(event.uid)",
                        kind: .event,
                        title: event.title.isEmpty ? "(untitled event)" : event.title,
                        detail: [event.start ?? "", event.location ?? ""]
                            .filter { !$0.isEmpty }.joined(separator: " · "),
                        dateLabel: Self.timeLabel(event.start, calendar: calendar),
                        action: nil))
                }
            }
        } else if let error = inputs.agendaError {
            eventState = .failed(error)
            failed.append("Today's agenda")
        } else if let reason = inputs.calendarUnavailable {
            eventState = .unavailable(reason)
            unavailable.append("Calendar")
        } else {
            eventState = .failed("The agenda was not read.")
            failed.append("Today's agenda")
        }
        sections.append(.init(id: "schedule", title: "Today", symbol: "calendar",
                              state: eventState, items: eventCards,
                              emptyMessage: "Nothing on your calendar today.",
                              refreshedAt: inputs.agenda == nil ? nil : now))

        // --- Tasks -----------------------------------------------------------
        // Due today and overdue come from the agenda when it has them (the
        // backend computes `overdue` against its own clock and open set); the
        // flat list is the fallback and supplies the upcoming section.
        var dueCards: [Briefing.Card] = []
        var overdueCards: [Briefing.Card] = []
        var upcomingCards: [Briefing.Card] = []
        var taskState: Briefing.State = .loaded

        if let agenda = inputs.agenda {
            for task in agenda.tasks ?? [] {
                dueCards.append(Self.card(for: task, kind: .taskDue, calendar: calendar))
            }
            for task in agenda.overdue ?? [] {
                overdueCards.append(Self.card(for: task, kind: .taskOverdue, calendar: calendar))
            }
            // `due_today` when the backend sends it, since `tasks` may include
            // more than today's.
            if let due = agenda.due_today {
                dueCards = due.map { Self.card(for: $0, kind: .taskDue, calendar: calendar) }
            }
        } else if let error = inputs.tasksError {
            taskState = .failed(error)
            failed.append("Tasks")
        } else if inputs.tasks == nil, inputs.agenda == nil {
            taskState = .failed("Your tasks were not read.")
            failed.append("Tasks")
        }

        if let tasks = inputs.tasks {
            // Overdue/due-today from the flat list, de-duplicated against the
            // agenda so one task is not listed twice under two headings.
            let known = Set(dueCards.map(\.id) + overdueCards.map(\.id))
            for task in tasks where !known.contains("task-\(task.task_ref)") {
                guard task.status == "open" || task.status == nil else { continue }
                if task.blocked == true { continue }
                if let due = Self.date(task.due_at, calendar: calendar) {
                    if due < startOfToday {
                        overdueCards.append(Self.card(for: task, kind: .taskOverdue,
                                                      calendar: calendar))
                    } else if let end = endOfToday, due < end {
                        dueCards.append(Self.card(for: task, kind: .taskDue,
                                                  calendar: calendar))
                    }
                }
            }
            upcomingCards = Self.upcoming(from: tasks, excluding: Set(dueCards.map(\.id)
                                                                     + overdueCards.map(\.id)),
                                          now: now, calendar: calendar)
                .map { Self.card(for: $0, kind: .taskUpcoming, calendar: calendar) }
        }

        sections.append(.init(id: "due", title: "Due today", symbol: "clock",
                              state: taskState, items: dueCards,
                              emptyMessage: "Nothing is due today.", refreshedAt: now))
        sections.append(.init(id: "overdue", title: "Overdue", symbol: "exclamationmark.circle",
                              state: taskState, items: overdueCards,
                              emptyMessage: "Nothing is overdue.", refreshedAt: now))
        sections.append(.init(id: "upcoming", title: "Coming up", symbol: "calendar.badge.clock",
                              state: inputs.tasks == nil ? taskState : .loaded,
                              items: upcomingCards,
                              emptyMessage: "Nothing scheduled ahead.",
                              refreshedAt: inputs.tasks == nil ? nil : now))

        // --- Captured items waiting for review -------------------------------
        var captureCards: [Briefing.Card] = []
        var captureState: Briefing.State = .loaded
        if let items = inputs.captureItems {
            for item in items where item.status == .inbox {
                captureCards.append(Briefing.Card(
                    id: "capture-\(item.id.uuidString)",
                    kind: .captureAwaitingReview,
                    title: item.headline,
                    detail: item.kind.displayName + (item.tags.isEmpty ? ""
                                                     : " · " + item.tags.joined(separator: ", ")),
                    action: .openCapture(item.id)))
            }
        } else {
            // The inbox is local and always available; nil means it genuinely
            // was not read, which is a failure rather than an empty inbox.
            captureState = .failed("The capture inbox was not read.")
            failed.append("Capture inbox")
        }
        // Only the most recent few: a briefing is a highlight, not the inbox.
        sections.append(.init(id: "capture", title: "Waiting for review",
                              symbol: "tray", state: captureState,
                              items: Array(captureCards.prefix(5)),
                              emptyMessage: "Your inbox is clear.",
                              refreshedAt: inputs.captureItems == nil ? nil : now))

        // --- Failures needing attention -------------------------------------
        var failureCards: [Briefing.Card] = []
        var flowState: Briefing.State = .loaded
        if let history = inputs.flowHistory {
            for run in history.prefix(12) where run.state == .failed {
                failureCards.append(Briefing.Card(
                    id: "flowrun-\(run.id.uuidString)",
                    kind: .flowFailure,
                    title: run.flowName,
                    detail: run.stopReason.isEmpty
                        ? "A saved flow stopped before finishing." : run.stopReason,
                    dateLabel: Self.relative(run.startedAt, now: now),
                    action: .openFlow(run.flowID)))
            }
        } else {
            flowState = .unavailable("The flow run history has not been read.")
        }
        if let schedules = inputs.schedules {
            for job in schedules where (job.failure_count ?? 0) > 0 || job.last_error != nil {
                failureCards.append(Briefing.Card(
                    id: "schedule-\(job.schedule_id)",
                    kind: .scheduleFailure,
                    title: job.name,
                    detail: job.last_error ?? "This scheduled job has failed before.",
                    dateLabel: job.last_run ?? "",
                    action: .openScreen(.planner)))
            }
        } else if let error = inputs.schedulesError {
            // A failed read makes the section failed whatever the flow half said:
            // leaving it `.unavailable` would let a dead backend read as "not
            // configured" instead of "could not be read".
            flowState = .failed(error)
            failed.append("Scheduled jobs")
        }
        sections.append(.init(id: "failures", title: "Needs attention",
                              symbol: "exclamationmark.triangle", state: flowState,
                              items: failureCards,
                              emptyMessage: "No failed runs or scheduled jobs.",
                              refreshedAt: now))

        // --- Approvals and provider health -----------------------------------
        var attentionCards: [Briefing.Card] = []
        var attentionState: Briefing.State = .loaded
        if let approvals = inputs.approvals {
            for confirmation in approvals {
                attentionCards.append(Briefing.Card(
                    id: "approval-\(confirmation.id)",
                    kind: .approval,
                    title: confirmation.displayTitle,
                    detail: confirmation.displayDetail,
                    action: .openScreen(.approvals)))
            }
        } else if let error = inputs.approvalsError {
            attentionState = .failed(error)
            failed.append("Approvals")
        }
        if let issue = inputs.providerIssue, !issue.isEmpty {
            attentionCards.append(Briefing.Card(
                id: "provider-issue", kind: .providerIssue,
                title: "AI provider", detail: issue, action: .openScreen(.providers)))
        }
        sections.append(.init(id: "approvals", title: "Waiting on you", symbol: "checkmark.shield",
                              state: attentionState, items: attentionCards,
                              emptyMessage: "Nothing is waiting for your approval.",
                              refreshedAt: now))

        // --- Staleness --------------------------------------------------------
        // A section whose data is old says so; the screen shows this rather than
        // presenting yesterday's read as this morning's.
        for index in sections.indices {
            guard sections[index].isLoaded else { continue }
            if let stamp = sections[index].refreshedAt,
               now.timeIntervalSince(stamp) > staleAfter {
                sections[index].state = .failed("Last read \(Self.relative(stamp, now: now)) — "
                                                + "this may be out of date.")
            }
        }

        // "All sources failed" means every section is in a failed state. A
        // section that is merely *unavailable* (not configured) is a different
        // fact and must not make a real day look dead — but a section that
        // genuinely failed everywhere does. The check is over the sections that
        // represent a source read, and a section that is neither failed nor
        // unavailable counts as a success.
        let sourceSections = sections.filter { section in
            // A section with no data and no failure is a genuine empty read.
            return true
        }
        let allFailed = !sourceSections.isEmpty && sourceSections.allSatisfy {
            if case .failed = $0.state { return true }
            return false
        }
        let suggestions = Self.buildSuggestions(overdue: overdueCards.count,
                                                due: dueCards.count,
                                                failures: failureCards.count,
                                                approvals: attentionCards.filter {
                                                    $0.kind == .approval }.count)

        return Briefing(generatedAt: now,
                        day: Self.dayLabel(now, calendar: calendar),
                        sections: sections,
                        failedSources: failed,
                        unavailableSources: unavailable,
                        headline: Self.headline(overdue: overdueCards.count,
                                                due: dueCards.count,
                                                events: eventCards.count,
                                                failures: failureCards.count,
                                                allFailed: allFailed),
                        suggestions: suggestions,
                        allSourcesFailed: allFailed)
    }

    // MARK: - Cards

    private static func card(for task: APIModels.PlannerTask, kind: Briefing.Card.Kind,
                             calendar: Calendar) -> Briefing.Card {
        var detailParts: [String] = []
        if let priority = task.priority, priority != "normal" { detailParts.append(priority) }
        if let project = task.project, !project.isEmpty { detailParts.append(project) }
        if let estimate = task.estimate_minutes, estimate > 0 {
            detailParts.append("\(estimate) min")
        }
        if task.blocked == true { detailParts.append("blocked") }
        return Briefing.Card(
            id: "task-\(task.task_ref)",
            kind: kind,
            title: task.title,
            detail: detailParts.joined(separator: " · "),
            dateLabel: dateLabel(task.due_at, calendar: calendar),
            action: .openTask(task.task_ref))
    }

    private static func upcoming(from tasks: [APIModels.PlannerTask], excluding: Set<String>,
                                 now: Date, calendar: Calendar) -> [APIModels.PlannerTask] {
        let horizon = calendar.date(byAdding: .day, value: 7, to: now) ?? now
        return tasks
            .filter { task in
                (task.status == "open" || task.status == nil)
                    && !excluding.contains("task-\(task.task_ref)")
                    && task.blocked != true
            }
            .filter { task in
                guard let due = date(task.due_at, calendar: calendar) else {
                    return false
                }
                return due >= now && due <= horizon
            }
            .sorted { (date($0.due_at, calendar: calendar) ?? .distantFuture)
                < (date($1.due_at, calendar: calendar) ?? .distantFuture) }
            .prefix(5)
            .map { $0 }
    }

    // MARK: - Text

    private static func headline(overdue: Int, due: Int, events: Int,
                                 failures: Int, allFailed: Bool) -> String {
        if allFailed {
            return "I could not read any of your sources, so this is not an empty day."
        }
        var parts: [String] = []
        if overdue > 0 {
            parts.append("\(overdue) overdue task\(overdue == 1 ? "" : "s")")
        }
        if due > 0 { parts.append("\(due) due today") }
        if events > 0 { parts.append("\(events) appointment\(events == 1 ? "" : "s")") }
        if failures > 0 { parts.append("\(failures) thing\(failures == 1 ? "" : "s") needing attention") }
        if parts.isEmpty { return "A clear day — nothing due and nothing scheduled." }
        return parts.joined(separator: ", ") + "."
    }

    /// Suggestions are *suggestions*. Each is derived from a real count in the
    /// data above, never from an inferred priority.
    private static func buildSuggestions(overdue: Int, due: Int,
                                         failures: Int, approvals: Int) -> [String] {
        var out: [String] = []
        if approvals > 0 {
            out.append("Answer the \(approvals) action\(approvals == 1 ? "" : "s") waiting for "
                       + "your approval first — something is paused until you do.")
        }
        if overdue > 0 {
            out.append("Clear or reschedule the \(overdue) overdue task"
                       + "\(overdue == 1 ? "" : "s") before starting anything new.")
        }
        if failures > 0 {
            out.append("Review the \(failures) failed run\(failures == 1 ? "" : "s") — a flow that "
                       + "stopped may need one edited step.")
        }
        if due > 0 {
            out.append("Work through today's \(due) task\(due == 1 ? "" : "s") while the day is "
                       + "still open.")
        }
        return out
    }

    private static func dayLabel(_ date: Date, calendar: Calendar) -> String {
        let formatter = DateFormatter()
        formatter.calendar = calendar
        formatter.dateStyle = .full
        formatter.timeStyle = .none
        formatter.timeZone = calendar.timeZone
        return formatter.string(from: date)
    }

    private static func timeLabel(_ iso: String?, calendar: Calendar) -> String {
        guard let date = self.date(iso, calendar: calendar) else { return "" }
        let formatter = DateFormatter()
        formatter.calendar = calendar
        formatter.dateStyle = .none
        formatter.timeStyle = .short
        formatter.timeZone = calendar.timeZone
        return formatter.string(from: date)
    }

    private static func dateLabel(_ iso: String?, calendar: Calendar) -> String {
        guard let date = self.date(iso, calendar: calendar) else { return "" }
        let formatter = DateFormatter()
        formatter.calendar = calendar
        formatter.dateStyle = .medium
        formatter.timeStyle = .short
        formatter.timeZone = calendar.timeZone
        return formatter.string(from: date)
    }

    private static func relative(_ date: Date, now: Date) -> String {
        let formatter = RelativeDateTimeFormatter()
        formatter.unitsStyle = .abbreviated
        return formatter.localizedString(for: date, relativeTo: now)
    }

    /// Parse an ISO-8601 date the backend sent, with or without a zone.
    ///
    /// The backend writes offsets in most fields but not all; a value with no
    /// zone is read in the person's own calendar rather than being dropped,
    /// because a task with a due date is not the same as a task without one.
    static func date(_ iso: String?, calendar: Calendar) -> Date? {
        guard let iso, !iso.isEmpty else { return nil }
        let withFraction = ISO8601DateFormatter()
        withFraction.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = withFraction.date(from: iso) { return date }
        let plain = ISO8601DateFormatter()
        plain.formatOptions = [.withInternetDateTime]
        if let date = plain.date(from: iso) { return date }
        let local = DateFormatter()
        local.calendar = calendar
        local.timeZone = calendar.timeZone
        local.locale = Locale(identifier: "en_US_POSIX")
        for format in ["yyyy-MM-dd'T'HH:mm:ss", "yyyy-MM-dd HH:mm:ss", "yyyy-MM-dd"] {
            local.dateFormat = format
            if let date = local.date(from: iso) { return date }
        }
        return nil
    }

    /// `PlannerAgenda.events` is `[AnyCodable]` because the backend's event shape
    /// is provider-specific. This reads the common fields, returning nil rather
    /// than a half-filled event when the object is not one.
    private static func event(from raw: AnyCodable) -> APIModels.GoogleEvent? {
        guard let dict = raw.value as? [String: Any] else { return nil }
        let uid = (dict["uid"] as? String) ?? (dict["id"] as? String) ?? UUID().uuidString
        return APIModels.GoogleEvent(
            uid: uid,
            title: (dict["title"] as? String) ?? (dict["summary"] as? String) ?? "",
            start: dict["start"] as? String,
            end: dict["end"] as? String,
            location: dict["location"] as? String,
            description: dict["description"] as? String,
            calendar: dict["calendar"] as? String,
            all_day: dict["all_day"] as? Bool,
            duration_minutes: dict["duration_minutes"] as? Int)
    }
}