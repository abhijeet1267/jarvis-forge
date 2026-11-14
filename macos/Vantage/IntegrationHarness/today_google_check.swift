import Foundation

// The Today / Google verification harness.
//
// XCTest cannot run in this environment (Command Line Tools only, no
// Xcode.app), so this stands in for the unit tests: it compiles against the
// real Sources/ tree and exercises the rules that silently lie when they are
// wrong.
//
// What this proves: the *logic* — mapping, day boundaries, proposal safety,
// availability, health classification, flow approval and dependent-step
// failure.
// What it does NOT prove: that Google was called. The live calls are exercised
// separately against the running backend and are reported as such.

var failures = 0
func check(_ name: String, _ ok: Bool, _ detail: @autoclosure () -> String = "") {
    print("\(ok ? "PASS" : "FAIL")  \(name)\(ok || detail().isEmpty ? "" : "  [\(detail())]")")
    if !ok { failures += 1 }
}

var cal = Calendar(identifier: .gregorian)
cal.timeZone = TimeZone(identifier: "Asia/Kolkata")!
func date(_ s: String) -> Date {
    let f = DateFormatter()
    f.locale = Locale(identifier: "en_US_POSIX")
    f.timeZone = cal.timeZone
    f.dateFormat = "yyyy-MM-dd HH:mm"
    return f.date(from: s)!
}
func ymdhm(_ d: Date) -> String {
    let f = DateFormatter()
    f.locale = Locale(identifier: "en_US_POSIX")
    f.timeZone = cal.timeZone
    f.dateFormat = "MM-dd HH:mm"
    return f.string(from: d)
}

// MARK: - Today mapping: empty vs failed

do {
    let empty = APIModels.GoogleEventsResponse(provider: "google", status: "real_empty",
                                               read_succeeded: true, detail: "", count: 0,
                                               events: [])
    let s = TodayMapping.calendarSection(empty, error: nil)
    check("empty calendar read is .empty, not .failed", s.state == .empty, "\(s.state)")

    let failed = APIModels.GoogleEventsResponse(
        provider: "google", status: "provider_error", read_succeeded: false,
        detail: "Calendar API disabled for project 1030985631332.", count: 0, events: [])
    let f = TodayMapping.calendarSection(failed, error: nil)
    var failedReason = ""
    if case .failed(let r) = f.state { failedReason = r }
    check("failed calendar read is .failed", f.state.isFailure, "\(f.state)")
    check("failed calendar read keeps the provider's reason",
          failedReason.contains("1030985631332"), failedReason)

    let transport = TodayMapping.calendarSection(nil, error: "The request timed out.")
    check("transport failure is .failed", transport.state.isFailure, "\(transport.state)")
    check("no response yet is .loading, not empty",
          TodayMapping.calendarSection(nil, error: nil).state == .loading)

    let one = APIModels.GoogleEventsResponse(
        provider: "google", status: "has_results", read_succeeded: true, detail: "", count: 1,
        events: [APIModels.GoogleEvent(uid: "e1", title: "Standup",
                                       start: "2026-10-29T09:30:00+05:30",
                                       end: "2026-10-29T10:00:00+05:30",
                                       location: "Room 4", description: nil,
                                       calendar: "work", all_day: false,
                                       duration_minutes: 30)])
    let r = TodayMapping.calendarSection(one, error: nil)
    check("event row keeps its Google identity", r.rows.first?.origin == .googleEvent(uid: "e1"),
          "\(String(describing: r.rows.first?.origin))")
    check("event row shows local clock time", r.rows.first?.trailing == "09:30",
          r.rows.first?.trailing ?? "nil")
}

// MARK: - Google Tasks day boundaries

func gtask(_ id: String, _ title: String, due: String?, done: Bool = false) -> APIModels.GoogleTask {
    APIModels.GoogleTask(id: id, title: title, notes: nil,
                         status: done ? "completed" : "needsAction", done: done,
                         due: due, completed: nil, list_id: "@default", parent: nil, source: nil)
}
func gtasks(_ items: [APIModels.GoogleTask]) -> APIModels.GoogleTasksResponse {
    APIModels.GoogleTasksResponse(provider: "google_tasks", status: "has_results",
                                  read_succeeded: true, detail: "", count: items.count,
                                  tasks: items)
}

do {
    let now = date("2026-10-29 23:59")
    let (_, split) = TodayMapping.googleTaskSplit(gtasks([gtask("1", "File", due: "2026-10-29")]),
                                                  error: nil, now: now, calendar: cal)
    check("a task due today is due today at 23:59", split.dueToday.count == 1 && split.overdue.isEmpty,
          "today=\(split.dueToday.count) overdue=\(split.overdue.count)")
    check("a task due today is labelled today", split.dueToday.first?.trailing == "today",
          split.dueToday.first?.trailing ?? "nil")

    let (_, overdue) = TodayMapping.googleTaskSplit(gtasks([gtask("2", "Old", due: "2026-10-28")]),
                                                    error: nil, now: now, calendar: cal)
    check("a task due yesterday is overdue", overdue.overdue.count == 1)
    check("overdue label counts days", overdue.overdue.first?.trailing == "overdue by a day",
          overdue.overdue.first?.trailing ?? "nil")

    let (_, undated) = TodayMapping.googleTaskSplit(gtasks([gtask("3", "Someday", due: nil)]),
                                                    error: nil, now: now, calendar: cal)
    check("an undated task is outstanding, not overdue",
          undated.overdue.isEmpty && undated.upcoming.count == 1)
    check("an undated task is not given a date", undated.upcoming.first?.trailing == nil)

    let (_, mixed) = TodayMapping.googleTaskSplit(
        gtasks([gtask("4", "Done", due: "2026-10-29", done: true),
                gtask("5", "Open", due: "2026-10-29")]),
        error: nil, now: now, calendar: cal)
    check("completed tasks are not on Today", mixed.outstanding == 1, "\(mixed.outstanding)")

    let tasksFailed = APIModels.GoogleTasksResponse(
        provider: "google_tasks", status: "auth_error", read_succeeded: false,
        detail: "Google rejected the access token.", count: 0, tasks: [])
    let (ts, _) = TodayMapping.googleTaskSplit(tasksFailed, error: nil, now: now, calendar: cal)
    check("failed tasks read is .failed, never empty", ts.state.isFailure, "\(ts.state)")

    let tasksEmpty = APIModels.GoogleTasksResponse(
        provider: "google_tasks", status: "real_empty", read_succeeded: true,
        detail: "", count: 0, tasks: [])
    let (te, _) = TodayMapping.googleTaskSplit(tasksEmpty, error: nil, now: now, calendar: cal)
    check("empty tasks read is .empty", te.state == .empty, "\(te.state)")
}

// MARK: - Board honesty

do {
    var board = TodayModel.Board.initial()
    board.calendar.state = .failed("Calendar disabled")
    board.googleTasks.state = .ready
    board.planner.state = .empty
    board.capture.state = .empty
    board.attention.state = .empty
    check("one failed source is named in the summary",
          board.failureSummary == "Calendar could not be read.", board.failureSummary ?? "nil")
    check("a partially loaded board still reports loading",
          { var b = TodayModel.Board.initial(); b.calendar.state = .ready; return b.isLoading }())
}

// MARK: - Calendar proposals: never guess

let monday = date("2026-10-26 09:00")
func parts(_ d: Date) -> DateComponents {
    cal.dateComponents([.day, .hour, .minute], from: d)
}

do {
    switch CalendarProposal.interpret("schedule a review meeting tomorrow at 10 AM",
                                      now: monday, calendar: cal) {
    case .ready(let p):
        let c = parts(p.start)
        check("a complete request produces the stated day and hour",
              c.day == 27 && c.hour == 10 && c.minute == 0, "\(c)")
        check("the title drops the verb and the date words", p.title == "Review", p.title)
        check("the summary names the day and both times",
              p.summary(calendar: cal).contains("Tuesday 27 October")
              && p.summary(calendar: cal).contains("10:00")
              && p.summary(calendar: cal).contains("11:00"), p.summary(calendar: cal))
    default:
        check("a complete request produces a proposal", false)
    }

    switch CalendarProposal.interpret("schedule a review meeting tomorrow", now: monday, calendar: cal) {
    case .needsDetail(let q): check("a date with no hour asks instead of guessing", true, q)
    default: check("a date with no hour asks instead of guessing", false)
    }

    switch CalendarProposal.interpret("schedule a review meeting tomorrow at 10", now: monday, calendar: cal) {
    case .needsDetail: check("an ambiguous bare hour asks", true)
    default: check("an ambiguous bare hour asks", false)
    }

    switch CalendarProposal.interpret("schedule a review meeting tomorrow at 17", now: monday, calendar: cal) {
    case .ready(let p): check("17:00 is unambiguous and accepted", parts(p.start).hour == 17)
    default: check("17:00 is unambiguous and accepted", false)
    }

    switch CalendarProposal.interpret("schedule a review meeting at 10 AM", now: monday, calendar: cal) {
    case .needsDetail(let q): check("a time with no day asks", q.lowercased().contains("which day"), q)
    default: check("a time with no day asks", false)
    }

    switch CalendarProposal.interpret("schedule a review meeting yesterday at 10 AM", now: monday, calendar: cal) {
    case .needsDetail(let q): check("a past date is refused", q.lowercased().contains("past"), q)
    default: check("a past date is refused", false)
    }

    check("a task sentence is not a calendar write",
          CalendarProposal.interpret("create a task to submit the report tomorrow",
                                     now: monday, calendar: cal) == .notACalendarRequest)
    check("a read question is not a calendar write",
          CalendarProposal.interpret("what is on my calendar today?",
                                     now: monday, calendar: cal) == .notACalendarRequest)

    switch CalendarProposal.interpret("schedule a review meeting tomorrow at 10 AM for 45 minutes",
                                      now: monday, calendar: cal) {
    case .ready(let p): check("a stated duration is read", p.durationMinutes == 45, "\(p.durationMinutes)")
    default: check("a stated duration is read", false)
    }
}

// MARK: - Availability

do {
    let window = DateInterval(start: date("2026-10-29 09:00"), end: date("2026-10-29 18:00"))
    func ev(_ s: String, _ e: String) -> APIModels.GoogleEvent {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX"); f.timeZone = cal.timeZone
        f.dateFormat = "yyyy-MM-dd'T'HH:mm:ssXXX"
        return APIModels.GoogleEvent(uid: UUID().uuidString, title: "x",
                                     start: f.string(from: date(s)),
                                     end: f.string(from: date(e)),
                                     location: nil, description: nil, calendar: nil,
                                     all_day: false, duration_minutes: nil)
    }

    let bad = Availability.within(window, readSucceeded: false,
                                  readFailureReason: "Calendar disabled", events: [])
    check("a failed read is unknown, never free", !bad.isKnown, "\(bad)")

    if case .known(let free, _) = Availability.within(window, readSucceeded: true, events: []) {
        check("a successful empty read is a whole free window",
              free.count == 1 && free[0].minutes == 540, "\(free.map(\.minutes))")
    } else { check("a successful empty read is a whole free window", false) }

    if case .known(let free, let busy) = Availability.within(
        window, readSucceeded: true,
        events: [ev("2026-10-29 09:30", "2026-10-29 10:00"),
                 ev("2026-10-29 13:00", "2026-10-29 14:00")]) {
        check("free blocks are the gaps between events", free.count == 3 && busy.count == 2,
              "free=\(free.count) busy=\(busy.count)")
        check("the first free block ends at the first event",
              ymdhm(free[0].end) == ymdhm(date("2026-10-29 09:30")), ymdhm(free[0].end))
    } else { check("free blocks are the gaps between events", false) }

    if case .known(_, let busy) = Availability.within(
        window, readSucceeded: true,
        events: [ev("2026-10-29 10:00", "2026-10-29 11:00"),
                 ev("2026-10-29 10:30", "2026-10-29 12:00")]) {
        check("overlapping events merge into one busy block", busy.count == 1, "\(busy.count)")
    } else { check("overlapping events merge into one busy block", false) }

    check("no slot is offered when the read failed",
          Availability.firstSlot(atLeast: 30, in: window, readSucceeded: false, events: []) == nil)
}

// MARK: - Integration health

do {
    let disabled = APIModels.GoogleTasksResponse(
        provider: "google_tasks", status: "provider_error", read_succeeded: false,
        detail: "Google Tasks is not enabled for this OAuth project. Enable the Google "
            + "Tasks API in the Google Cloud console for that project.",
        count: 0, tasks: [])
    let state = IntegrationHealth.tasks(disabled)
    var isPermission = false
    if case .permissionDenied = state { isPermission = true }
    check("a disabled API is a permission problem, not a token problem", isPermission, state.label)
    check("a disabled API is never drawn healthy", !state.isHealthy)

    let calendarOK = IntegrationHealth.calendar(
        APIModels.GoogleEventsResponse(provider: "google", status: "real_empty",
                                       read_succeeded: true, detail: "", count: 0, events: []))
    check("a successful empty read is healthy", calendarOK.isHealthy, calendarOK.label)
    check("Calendar and Tasks can disagree in one account",
          calendarOK.isHealthy && !state.isHealthy)
    check("an unknown state is not healthy",
          !IntegrationHealth.State.unknown("connected").isHealthy)
}

// MARK: - Google flow actions

@MainActor
final class HarnessExecutor: MacActionExecutor {
    var results: [String: MacActionResult] = [:]
    var executed: [String] = []
    func execute(_ req: MacActionRequest,
                 cancellation: MacControlService.Cancellation?) async -> MacActionResult {
        executed.append(req.kind.id)
        return results[req.kind.id] ?? .verified(req.kind, "ok")
    }
}

let task = Task { @MainActor in
    check("Google reads are class A", MacActionPolicy.forKind(.googleAgenda)?.risk == .read)
    check("Google writes are class C",
          MacActionPolicy.forKind(.googleCreateTask)?.risk == .mutating
          && MacActionPolicy.forKind(.googleCreateEvent)?.risk == .mutating)
    check("Google writes require confirmation",
          MacActionPolicy.forKind(.googleCreateTask)!.risk.requiresConfirmation)

    let router = MacActionRouter(permissions: { MacPermissionStatus(accessibilityGranted: true) })
    var blank = MacActionArguments(); blank.text = "   "
    do {
        _ = try router.prepare(kind: .googleCreateTask, arguments: blank, origin: .local)
        check("a Google task with no title is refused", false)
    } catch { check("a Google task with no title is refused", true) }

    var badTime = MacActionArguments(); badTime.title = "Review"; badTime.startDate = "whenever"
    do {
        _ = try router.prepare(kind: .googleCreateEvent, arguments: badTime, origin: .local)
        check("an unreadable event time is refused before approval", false)
    } catch { check("an unreadable event time is refused before approval", true) }

    var write = MacActionArguments(); write.text = "Submit the report"
    do {
        _ = try router.prepare(kind: .googleCreateTask, arguments: write, origin: .backend)
        check("an unattended caller cannot run a Google write unconfirmed", false)
    } catch { check("an unattended caller cannot run a Google write unconfirmed", true) }

    let draft = Flow(name: "g", steps: [FlowStep(actionID: MacActionKind.googleCreateTask.id,
                                                 arguments: StoredArguments(write))])
    check("a Google write step can be saved while Google is disconnected",
          draft.validate().isEmpty, "\(draft.validate().map(\.message))")

    // Dependent-step failure: a failed Google read must stop the run.
    let dir = URL(fileURLWithPath: NSTemporaryDirectory())
        .appendingPathComponent("today-harness-\(UUID().uuidString)")
    try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: dir) }
    let store = FlowStore(directory: dir)
    let ex = HarnessExecutor()
    ex.results[MacActionKind.googleAgenda.id] = .failed(.googleAgenda, "Calendar could not be read.")
    var appArgs = StoredArguments(); appArgs.application = "Safari"
    let engine = FlowEngine(store: store,
                            router: MacActionRouter(permissions: {
                                MacPermissionStatus(accessibilityGranted: true) }),
                            executor: ex)
    let run = await engine.run(Flow(name: "agenda then safari", steps: [
        FlowStep(actionID: MacActionKind.googleAgenda.id, arguments: StoredArguments()),
        FlowStep(actionID: MacActionKind.openApplication.id, arguments: appArgs)
    ]))
    check("a failed Google read stops the flow", run?.state == .failed, "\(String(describing: run?.state))")
    check("a dependent step does not run after the Google step failed",
          ex.executed == [MacActionKind.googleAgenda.id], "\(ex.executed)")

    // A Google write pauses for approval.
    let ex2 = HarnessExecutor()
    let engine2 = FlowEngine(store: FlowStore(directory: dir),
                             router: MacActionRouter(permissions: {
                                 MacPermissionStatus(accessibilityGranted: true) }),
                             executor: ex2)
    let run2 = await engine2.run(Flow(name: "write", steps: [
        FlowStep(actionID: MacActionKind.googleCreateTask.id, arguments: StoredArguments(write))
    ]))
    check("a Google write step pauses for approval", run2?.state == .running
          && engine2.pendingApproval != nil, "\(String(describing: run2?.state))")
    check("the approval names the task", engine2.pendingApproval?.summary.contains("Submit the report") == true,
          engine2.pendingApproval?.summary ?? "nil")

    // Result mapping: a failed Google outcome is never verified.
    let service = MacControlService()
    let agenda = try! router.prepare(kind: .googleAgenda, arguments: MacActionArguments(),
                                     origin: .local)
    check("a failed Google outcome maps to .failed",
          service.execute(agenda, google: GoogleOutcome(status: .failed, message: "boom")).status == .failed)
    check("a refused Google outcome maps to .refused",
          service.execute(agenda, google: GoogleOutcome(status: .refused, message: "no")).status == .refused)
    check("an unconfirmed Google write maps to .unverified",
          service.execute(agenda, google: GoogleOutcome(status: .unverified, message: "?")).status == .unverified)
    check("a Google step with no outcome fails rather than guessing",
          service.execute(agenda).status == .failed)

    print(failures == 0 ? "\nALL CHECKS PASSED" : "\n\(failures) CHECK(S) FAILED")
    exit(failures == 0 ? 0 : 1)
}
_ = task
RunLoop.main.run()