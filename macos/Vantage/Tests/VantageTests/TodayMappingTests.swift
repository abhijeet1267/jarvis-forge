import XCTest
@testable import Vantage

/// Today's mapping rules. The cases here are the ones that lie when they are
/// wrong: an empty read drawn as a failure, a failed read drawn as empty, a
/// task due today called overdue, a task with no date given one.
final class TodayMappingTests: XCTestCase {

    private var calendar: Calendar {
        var c = Calendar(identifier: .gregorian)
        c.timeZone = TimeZone(identifier: "Asia/Kolkata")!
        return c
    }

    private func date(_ string: String) -> Date {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = calendar.timeZone
        f.dateFormat = "yyyy-MM-dd HH:mm"
        return f.date(from: string)!
    }

    // MARK: - Calendar: empty vs failed

    func testEmptyCalendarReadIsEmptyNotFailed() {
        let response = APIModels.GoogleEventsResponse(
            provider: "google", status: "real_empty", read_succeeded: true,
            detail: "", count: 0, events: [])
        let section = TodayMapping.calendarSection(response, error: nil)
        XCTAssertEqual(section.state, .empty)
        XCTAssertEqual(section.rows.count, 0)
        XCTAssertTrue(section.rows.isEmpty)
    }

    func testFailedCalendarReadIsNeverEmpty() {
        let response = APIModels.GoogleEventsResponse(
            provider: "google", status: "provider_error", read_succeeded: false,
            detail: "Google Calendar API has not been used in project 1030985631332.",
            count: 0, events: [])
        let section = TodayMapping.calendarSection(response, error: nil)
        guard case .failed(let reason) = section.state else {
            return XCTFail("a failed read must be .failed, got \(section.state)")
        }
        XCTAssertTrue(reason.contains("1030985631332"),
                      "the provider's own reason must survive into the UI")
    }

    func testTransportFailureIsFailed() {
        let section = TodayMapping.calendarSection(nil, error: "The request timed out.")
        XCTAssertEqual(section.state, .failed("The request timed out."))
    }

    func testNilResponseIsLoadingNotEmpty() {
        let section = TodayMapping.calendarSection(nil, error: nil)
        XCTAssertEqual(section.state, .loading)
    }

    func testEventsMapToRowsWithIdentityAndTime() {
        let response = APIModels.GoogleEventsResponse(
            provider: "google", status: "has_results", read_succeeded: true,
            detail: "", count: 1,
            events: [APIModels.GoogleEvent(
                uid: "abc123", title: "Standup",
                start: "2026-10-29T09:30:00+05:30",
                end: "2026-10-29T10:00:00+05:30",
                location: "Room 4", description: nil, calendar: "work",
                all_day: false, duration_minutes: 30)])
        let section = TodayMapping.calendarSection(response, error: nil)
        XCTAssertEqual(section.state, .ready)
        XCTAssertEqual(section.rows.first?.origin, .googleEvent(uid: "abc123"))
        XCTAssertEqual(section.rows.first?.trailing, "09:30")
        XCTAssertEqual(section.rows.first?.detail, "Room 4")
    }

    func testAllDayEventSaysAllDayRatherThanMidnight() {
        let response = APIModels.GoogleEventsResponse(
            provider: "google", status: "has_results", read_succeeded: true,
            detail: "", count: 1,
            events: [APIModels.GoogleEvent(
                uid: "holiday", title: "Holiday", start: "2026-10-30",
                end: "2026-10-31", location: nil, description: nil, calendar: nil,
                all_day: true, duration_minutes: 1440)])
        let section = TodayMapping.calendarSection(response, error: nil)
        XCTAssertEqual(section.rows.first?.trailing, "all day")
    }

    // MARK: - Google Tasks: day boundaries

    private func tasks(_ items: [APIModels.GoogleTask]) -> APIModels.GoogleTasksResponse {
        APIModels.GoogleTasksResponse(provider: "google_tasks", status: "has_results",
                                      read_succeeded: true, detail: "",
                                      count: items.count, tasks: items)
    }

    private func task(_ id: String, _ title: String, due: String?,
                      done: Bool = false) -> APIModels.GoogleTask {
        APIModels.GoogleTask(id: id, title: title, notes: nil,
                             status: done ? "completed" : "needsAction", done: done,
                             due: due, completed: nil, list_id: "@default",
                             parent: nil, source: nil)
    }

    func testTaskDueTodayIsDueTodayNotOverdue() {
        // 23:59 on the due day is still that day, not the next one.
        let now = date("2026-10-29 23:59")
        let response = tasks([task("1", "File report", due: "2026-10-29")])
        let (section, split) = TodayMapping.googleTaskSplit(response, error: nil,
                                                            now: now, calendar: calendar)
        XCTAssertEqual(split.dueToday.count, 1)
        XCTAssertEqual(split.overdue.count, 0)
        XCTAssertEqual(split.dueToday.first?.trailing, "today")
        XCTAssertEqual(section.state, .ready)
    }

    func testTaskDueYesterdayIsOverdueByADay() {
        let now = date("2026-10-29 08:00")
        let response = tasks([task("1", "Old", due: "2026-10-28")])
        let (_, split) = TodayMapping.googleTaskSplit(response, error: nil,
                                                     now: now, calendar: calendar)
        XCTAssertEqual(split.overdue.count, 1)
        XCTAssertEqual(split.overdue.first?.trailing, "overdue by a day")
        XCTAssertEqual(split.overdue.first?.emphasis, .urgent)
    }

    func testTaskWithNoDueDateIsNeitherTodayNorOverdue() {
        let now = date("2026-10-29 08:00")
        let response = tasks([task("1", "Someday", due: nil)])
        let (_, split) = TodayMapping.googleTaskSplit(response, error: nil,
                                                      now: now, calendar: calendar)
        XCTAssertEqual(split.dueToday.count, 0)
        XCTAssertEqual(split.overdue.count, 0)
        XCTAssertEqual(split.upcoming.count, 1, "an undated task is outstanding, not dated")
        XCTAssertNil(split.upcoming.first?.trailing, "and it must not be given a date")
    }

    func testCompletedTasksAreNotShown() {
        let now = date("2026-10-29 08:00")
        let response = tasks([
            task("1", "Done", due: "2026-10-29", done: true),
            task("2", "Open", due: "2026-10-29")
        ])
        let (section, split) = TodayMapping.googleTaskSplit(response, error: nil,
                                                            now: now, calendar: calendar)
        XCTAssertEqual(split.outstanding, 1)
        XCTAssertEqual(section.rows.map(\.title), ["Open"])
    }

    func testFailedTasksReadIsFailedNotEmpty() {
        let response = APIModels.GoogleTasksResponse(
            provider: "google_tasks", status: "auth_error", read_succeeded: false,
            detail: "Google rejected the access token.", count: 0, tasks: [])
        let (section, split) = TodayMapping.googleTaskSplit(response, error: nil,
                                                            now: Date(), calendar: calendar)
        guard case .failed = section.state else {
            return XCTFail("a failed tasks read must be .failed, got \(section.state)")
        }
        XCTAssertEqual(split.outstanding, 0)
    }

    func testEmptyTasksReadIsEmpty() {
        let response = APIModels.GoogleTasksResponse(
            provider: "google_tasks", status: "real_empty", read_succeeded: true,
            detail: "", count: 0, tasks: [])
        let (section, _) = TodayMapping.googleTaskSplit(response, error: nil,
                                                        now: Date(), calendar: calendar)
        XCTAssertEqual(section.state, .empty)
        XCTAssertEqual(section.emptyText, "No open Google tasks.")
    }

    // MARK: - Board honesty

    func testBoardSummarisesFailedSourcesBySectionOnly() {
        var board = TodayModel.Board.initial()
        board.calendar.state = .failed("Calendar disabled")
        board.googleTasks.state = .ready
        board.planner.state = .empty
        board.capture.state = .empty
        board.attention.state = .empty
        XCTAssertEqual(board.failedSources, [.calendar])
        XCTAssertEqual(board.failureSummary, "Calendar could not be read.")
    }

    func testBoardWithNoFailuresHasNoSummary() {
        var board = TodayModel.Board.initial()
        board.calendar.state = .empty
        board.googleTasks.state = .empty
        board.planner.state = .empty
        board.capture.state = .empty
        board.attention.state = .empty
        XCTAssertNil(board.failureSummary)
    }

    func testPartiallyLoadedBoardStillReportsLoading() {
        var board = TodayModel.Board.initial()
        board.calendar.state = .ready
        XCTAssertTrue(board.isLoading, "one unsettled source means the board is still loading")
    }

    // MARK: - Planner

    func testPlannerOverdueSortsFirst() {
        let tasks = [
            APIModels.PlannerTask(task_ref: "A", title: "Later", detail: nil,
                                  status: "open", priority: "normal", due_at: nil,
                                  start_at: nil, estimate_minutes: nil,
                                  depends_on: nil, project: nil, done_at: nil,
                                  evidence: nil, overdue: false, blocked: false),
            APIModels.PlannerTask(task_ref: "B", title: "Late", detail: nil,
                                  status: "open", priority: "high", due_at: nil,
                                  start_at: nil, estimate_minutes: nil,
                                  depends_on: nil, project: nil, done_at: nil,
                                  evidence: nil, overdue: true, blocked: false)
        ]
        let section = TodayMapping.plannerSection(tasks, error: nil, now: Date())
        XCTAssertEqual(section.rows.first?.title, "Late")
        XCTAssertEqual(section.rows.first?.emphasis, .urgent)
    }

    func testPlannerFailureIsDistinctFromEmpty() {
        let failed = TodayMapping.plannerSection(nil, error: "Could not load.", now: Date())
        XCTAssertEqual(failed.state, .failed("Could not load."))
        let empty = TodayMapping.plannerSection([], error: nil, now: Date())
        XCTAssertEqual(empty.state, .empty)
    }
}