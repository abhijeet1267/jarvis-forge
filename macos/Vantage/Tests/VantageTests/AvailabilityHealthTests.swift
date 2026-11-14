import XCTest
@testable import Vantage

/// Availability from real events, and the refusal to answer when the calendar
/// could not be read.
final class AvailabilityTests: XCTestCase {

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

    private func event(_ title: String, _ start: String, _ end: String) -> APIModels.GoogleEvent {
        APIModels.GoogleEvent(uid: title, title: title,
                              start: iso(start), end: iso(end),
                              location: nil, description: nil, calendar: nil,
                              all_day: false, duration_minutes: nil)
    }

    private func iso(_ local: String) -> String {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = calendar.timeZone
        f.dateFormat = "yyyy-MM-dd'T'HH:mm:ssXXX"
        return f.string(from: date(local))
    }

    private var window: DateInterval {
        DateInterval(start: date("2026-10-29 09:00"), end: date("2026-10-29 18:00"))
    }

    // MARK: - The rule

    func testFailedReadIsUnknownNeverFree() {
        let outcome = Availability.within(window, readSucceeded: false,
                                          readFailureReason: "Calendar disabled",
                                          events: [])
        guard case .unknown(let reason) = outcome else {
            return XCTFail("a failed read must be .unknown, got \(outcome)")
        }
        XCTAssertEqual(reason, "Calendar disabled")
        XCTAssertFalse(outcome.isKnown)
    }

    func testEmptySuccessIsGenuinelyFree() {
        let outcome = Availability.within(window, readSucceeded: true, events: [])
        guard case .known(let free, let busy) = outcome else {
            return XCTFail("expected .known, got \(outcome)")
        }
        XCTAssertEqual(free.count, 1)
        XCTAssertEqual(free.first?.minutes, 540)
        XCTAssertTrue(busy.isEmpty)
    }

    func testFreeBlocksAreTheGapsBetweenEvents() {
        let events = [
            event("Standup", "2026-10-29 09:30", "2026-10-29 10:00"),
            event("Review", "2026-10-29 13:00", "2026-10-29 14:00")
        ]
        let outcome = Availability.within(window, readSucceeded: true, events: events)
        guard case .known(let free, let busy) = outcome else {
            return XCTFail("expected .known")
        }
        XCTAssertEqual(busy.count, 2)
        XCTAssertEqual(free.count, 3)
        XCTAssertEqual(free[0].start, date("2026-10-29 09:00"))
        XCTAssertEqual(free[0].end, date("2026-10-29 09:30"))
        XCTAssertEqual(free[1].start, date("2026-10-29 10:00"))
        XCTAssertEqual(free[2].end, date("2026-10-29 18:00"))
    }

    func testOverlappingEventsMergeIntoOneBusyBlock() {
        let events = [
            event("A", "2026-10-29 10:00", "2026-10-29 11:00"),
            event("B", "2026-10-29 10:30", "2026-10-29 12:00")
        ]
        let outcome = Availability.within(window, readSucceeded: true, events: events)
        guard case .known(_, let busy) = outcome else { return XCTFail("expected .known") }
        XCTAssertEqual(busy.count, 1, "a double-booked hour is one busy block")
        XCTAssertEqual(busy.first?.end, date("2026-10-29 12:00"))
    }

    func testEventsOutsideTheWindowAreIgnored() {
        let events = [event("Early", "2026-10-29 07:00", "2026-10-29 08:00")]
        let outcome = Availability.within(window, readSucceeded: true, events: events)
        guard case .known(let free, let busy) = outcome else { return XCTFail("expected .known") }
        XCTAssertTrue(busy.isEmpty)
        XCTAssertEqual(free.count, 1)
    }

    func testEventSpanningTheWindowStartIsClipped() {
        let events = [event("Overnight", "2026-10-28 20:00", "2026-10-29 10:00")]
        let outcome = Availability.within(window, readSucceeded: true, events: events)
        guard case .known(_, let busy) = outcome else { return XCTFail("expected .known") }
        XCTAssertEqual(busy.first?.start, window.start, "clipped to the window being asked about")
    }

    func testFirstSlotRequiresEnoughMinutes() {
        let events = [event("Standup", "2026-10-29 09:30", "2026-10-29 10:00")]
        let short = Availability.firstSlot(atLeast: 30, in: window,
                                           readSucceeded: true, events: events)
        XCTAssertEqual(short?.start, date("2026-10-29 10:00"))
        let long = Availability.firstSlot(atLeast: 600, in: window,
                                          readSucceeded: true, events: events)
        XCTAssertNil(long, "no gap is ten hours long inside a nine-hour window")
    }

    func testFirstSlotIsNilWhenTheReadFailed() {
        XCTAssertNil(Availability.firstSlot(atLeast: 30, in: window,
                                            readSucceeded: false, events: []))
    }

    func testBackwardsWindowIsInvalid() {
        let bad = DateInterval(start: date("2026-10-29 18:00"), end: date("2026-10-29 09:00"))
        let outcome = Availability.within(bad, readSucceeded: true, events: [])
        guard case .invalid = outcome else { return XCTFail("expected .invalid") }
    }

    // MARK: - Explanation

    func testDescriptionStatesAssumptionsBoundedClaim() {
        let outcome = Availability.within(window, readSucceeded: true, events: [])
        let text = Availability.describe(outcome, window: window, calendar: calendar)
        XCTAssertTrue(text.contains("nothing on"))
        XCTAssertTrue(text.contains("09:00"))
    }

    func testDescriptionOfUnknownIsTheReason() {
        let outcome = Availability.Outcome.unknown(reason: "Calendar could not be read.")
        XCTAssertEqual(Availability.describe(outcome, window: window, calendar: calendar),
                       "Calendar could not be read.")
    }
}

/// Integration health: the same payload must not be drawn as healthy when it is
/// a failure, and the four failure kinds must stay distinguishable.
final class IntegrationHealthTests: XCTestCase {

    func testSuccessfulEmptyReadIsHealthy() {
        let response = APIModels.GoogleTasksResponse(
            provider: "google_tasks", status: "real_empty", read_succeeded: true,
            detail: "", count: 0, tasks: [])
        let state = IntegrationHealth.tasks(response)
        XCTAssertEqual(state, .workingEmpty)
        XCTAssertTrue(state.isHealthy)
    }

    func testDisabledApiIsPermissionNotAuth() {
        let response = APIModels.GoogleTasksResponse(
            provider: "google_tasks", status: "provider_error", read_succeeded: false,
            detail: "Google Tasks is not enabled for this OAuth project. Enable the "
                + "Google Tasks API in the Google Cloud console for that project.",
            count: 0, tasks: [])
        let state = IntegrationHealth.tasks(response)
        guard case .permissionDenied = state else {
            return XCTFail("a disabled API is a console problem, got \(state)")
        }
        XCTAssertFalse(state.isHealthy)
    }

    func testRejectedTokenIsAuthRequired() {
        let response = APIModels.GoogleEventsResponse(
            provider: "google", status: "auth_error", read_succeeded: false,
            detail: "Google rejected the access token. Reconnect the account in Settings.",
            count: 0, events: [])
        let state = IntegrationHealth.calendar(response)
        guard case .authRequired = state else {
            return XCTFail("expected .authRequired, got \(state)")
        }
    }

    func testReadOnlyIsItsOwnState() {
        let state = IntegrationHealth.classify(
            detail: "This Google connection is read-only. Editing events needs the "
                + "calendar write permission, which Vantage does not request.",
            status: "auth_error")
        guard case .readOnly = state else { return XCTFail("expected .readOnly, got \(state)") }
    }

    func testTimeoutIsRecognisedFromTheTransportError() {
        struct Timeout: LocalizedError {
            var errorDescription: String? { "The request timed out." }
        }
        let state = IntegrationHealth.transport(Timeout())
        guard case .timedOut = state else { return XCTFail("expected .timedOut, got \(state)") }
    }

    func testUnreachableBackendIsUnavailable() {
        struct Down: LocalizedError {
            var errorDescription: String? { "Could not connect to the server." }
        }
        let state = IntegrationHealth.transport(Down())
        guard case .unavailable = state else {
            return XCTFail("expected .unavailable, got \(state)")
        }
    }

    func testCalendarAndTasksCanDisagree() {
        // The measured situation: one readable, one disabled in the project.
        let calendar = IntegrationHealth.calendar(
            APIModels.GoogleEventsResponse(provider: "google", status: "real_empty",
                                           read_succeeded: true, detail: "",
                                           count: 0, events: []))
        let tasks = IntegrationHealth.tasks(
            APIModels.GoogleTasksResponse(provider: "google_tasks", status: "provider_error",
                                          read_succeeded: false,
                                          detail: "Google Tasks is not enabled for this OAuth project.",
                                          count: 0, tasks: []))
        XCTAssertTrue(calendar.isHealthy)
        XCTAssertFalse(tasks.isHealthy)
        XCTAssertNotEqual(calendar.label, tasks.label)
    }

    func testHealthyCannotComeFromAStatusAlone() {
        // `unknown` is the default and is never healthy, whatever the state string.
        XCTAssertFalse(IntegrationHealth.State.unknown("connected").isHealthy)
    }
}