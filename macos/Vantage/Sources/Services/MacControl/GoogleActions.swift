import Foundation

/// The Google operations a Mac action can perform, as one small protocol.
///
/// Why a protocol instead of the executor calling `APIClient` directly:
///
/// * the flow tests need to prove **dependent-step failure** — a Google read
///   that failed must stop the run rather than continue as if it succeeded —
///   and that is only testable with a substitutable callee;
/// * `MacControlService` is synchronous and runs under a lock. It must never
///   block a thread on a network round trip, so the Google work is done by the
///   caller (async) and handed in as a result.
///
/// The production conformance is `BackendGoogleActions`, which turns every
/// response into a `GoogleOutcome` that keeps the provider's own status. It
/// never invents success: a failed read is `.failed`, and a write is `.verified`
/// only when the backend reports it wrote *and* the record came back.
enum GoogleActionKind: String, Equatable, Sendable {
    case agenda
    case overdueTasks
    case createTask
    case createEvent
}

/// What a Google call produced, in the terms the executor reports.
struct GoogleOutcome: Equatable, Sendable {
    enum Status: String, Equatable, Sendable {
        /// Read succeeded with data.
        case hasResults
        /// Read succeeded and there is genuinely nothing.
        case empty
        /// Read failed. A distinct state from `empty`, always.
        case failed
        /// Write succeeded and was confirmed by the provider's own response.
        case verified
        /// Write was refused (e.g. a read-only calendar). Nothing changed.
        case refused
        /// The call happened but the outcome is not established.
        case unverified
    }

    let status: Status
    let message: String
    var details: [String: String] = [:]

    var isFailure: Bool {
        status == .failed || status == .refused
    }
}

/// Runs one Google operation. Async, because every one is a network call.
protocol GoogleActions: Sendable {
    func perform(_ kind: GoogleActionKind, arguments: MacActionArguments) async -> GoogleOutcome
}

/// The real implementation: the local backend, which owns the OAuth token.
///
/// It speaks the same `/google/*` routes the Google screen uses, so a flow step
/// and a button press cannot mean different things — there is one adapter, and
/// this is a caller of it.
struct BackendGoogleActions: GoogleActions {

    private let api: APIClient

    init(api: APIClient = .shared) { self.api = api }

    func perform(_ kind: GoogleActionKind, arguments: MacActionArguments) async -> GoogleOutcome {
        switch kind {
        case .agenda:
            return await agenda()
        case .overdueTasks:
            return await overdueTasks()
        case .createTask:
            return await createTask(arguments)
        case .createEvent:
            return await createEvent(arguments)
        }
    }

    // MARK: - Reads

    private func agenda() async -> GoogleOutcome {
        do {
            // Both services, because "today's agenda" is both. A Tasks failure
            // must not hide a readable calendar, so the calendar's own result
            // decides the status and the task failure is stated alongside it.
            async let calendarCall = api.googleEvents()
            async let tasksCall = api.googleTasks()
            let (calendar, tasks) = try await (calendarCall, tasksCall)

            let events = calendar.events ?? []
            let dueToday = TodayMapping.googleTaskSplit(tasks, error: nil, now: Date(),
                                                        calendar: .current).split.dueToday
            var lines: [String] = []
            if calendar.read_succeeded == true {
                if events.isEmpty {
                    lines.append("No calendar events today.")
                } else {
                    let clocked = events.map { event -> String in
                        let time = event.all_day == true ? "all day"
                            : TodayMapping.clock(event.start)
                        return "\(time) \(event.title)"
                    }
                    lines.append("\(events.count) event(s) today: "
                                 + clocked.prefix(8).joined(separator: " · "))
                }
            } else {
                // The calendar failed: say so. It is not a clear day.
                return GoogleOutcome(
                    status: .failed,
                    message: calendar.detail ?? "Google Calendar could not be read.")
            }
            if tasks.read_succeeded == true {
                lines.append(dueToday.isEmpty
                             ? "No Google tasks are due today."
                             : "\(dueToday.count) Google task(s) due today: "
                                + dueToday.map(\.title).joined(separator: ", ") + ".")
            } else {
                lines.append(tasks.detail ?? "Google Tasks could not be read.")
            }
            return GoogleOutcome(
                status: events.isEmpty && dueToday.isEmpty ? .empty : .hasResults,
                message: lines.joined(separator: " "),
                details: ["events": "\(events.count)", "tasksDueToday": "\(dueToday.count)"])
        } catch {
            return GoogleOutcome(status: .failed,
                                 message: "Could not reach the backend for Google Calendar. "
                                    + error.localizedDescription)
        }
    }

    private func overdueTasks() async -> GoogleOutcome {
        do {
            let response = try await api.googleTasks()
            guard response.read_succeeded == true else {
                return GoogleOutcome(status: .failed,
                                     message: response.detail ?? "Google Tasks could not be read.")
            }
            let split = TodayMapping.googleTaskSplit(response, error: nil, now: Date(),
                                                     calendar: .current).split
            if split.overdue.isEmpty {
                let message = split.outstanding == 0
                    ? "No open Google tasks, so nothing is overdue."
                    : "Nothing is overdue. \(split.outstanding) Google task(s) are still open."
                return GoogleOutcome(status: .empty, message: message,
                                     details: ["overdue": "0",
                                               "open": "\(split.outstanding)"])
            }
            let named = split.overdue.prefix(8)
                .map { "\($0.title) (\($0.trailing ?? "overdue"))" }
                .joined(separator: ", ")
            return GoogleOutcome(
                status: .hasResults,
                message: "\(split.overdue.count) overdue Google task(s): \(named).",
                details: ["overdue": "\(split.overdue.count)",
                          "open": "\(split.outstanding)"])
        } catch {
            return GoogleOutcome(status: .failed,
                                 message: "Could not reach the backend for Google Tasks. "
                                    + error.localizedDescription)
        }
    }

    // MARK: - Writes

    private func createTask(_ arguments: MacActionArguments) async -> GoogleOutcome {
        let title = arguments.googleTitle.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !title.isEmpty else {
            return GoogleOutcome(status: .refused, message: "A Google task needs a title.")
        }
        do {
            let result = try await api.googleTaskCreate(
                APIModels.GoogleTaskCreateRequest(
                    title: title, notes: nil, due: nil,
                    list_id: arguments.taskListID.isEmpty ? nil : arguments.taskListID,
                    source: "Vantage flow"))
            // Verified only when Google returned the record it created: an
            // `ok` with no task id is a request that was accepted, not a task
            // that exists.
            guard result.ok == true, let task = result.task, !task.id.isEmpty else {
                return GoogleOutcome(status: .refused,
                                     message: result.error
                                        ?? "Google did not return the task it was asked to create.")
            }
            return GoogleOutcome(status: .verified,
                                 message: "Created “\(task.title)” in Google Tasks.",
                                 details: ["taskID": task.id, "listID": task.list_id ?? ""])
        } catch {
            return GoogleOutcome(status: .failed,
                                 message: "Google Tasks could not be written: "
                                    + error.localizedDescription)
        }
    }

    private func createEvent(_ arguments: MacActionArguments) async -> GoogleOutcome {
        let title = arguments.googleTitle.trimmingCharacters(in: .whitespacesAndNewlines)
        let start = arguments.startDate.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !title.isEmpty else {
            return GoogleOutcome(status: .refused, message: "A calendar event needs a title.")
        }
        guard !start.isEmpty else {
            return GoogleOutcome(status: .refused, message: "A calendar event needs a start time.")
        }
        do {
            let result = try await api.createCalendarEvent(
                title: title, start: start,
                durationMinutes: arguments.durationMinutes ?? 60,
                location: arguments.location)
            if result.ok != true {
                // A read-only calendar answers here, and the tool's sentence
                // explains which kind of refusal it was. Reporting it as done
                // would be the invented success this integration avoids.
                return GoogleOutcome(
                    status: .refused,
                    message: result.message ?? "The event was not created.")
            }
            return GoogleOutcome(
                status: result.verified == true ? .verified : .unverified,
                message: result.message ?? "Created the event.",
                details: result.event.map { ["uid": $0.uid, "title": $0.title] } ?? [:])
        } catch {
            return GoogleOutcome(status: .failed,
                                 message: "The calendar could not be written: "
                                    + error.localizedDescription)
        }
    }
}