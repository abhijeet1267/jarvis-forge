import XCTest
@testable import Vantage

/// The calendar proposal layer: what a sentence becomes before anything is
/// written, and — more importantly — what it refuses to become.
final class CalendarProposalTests: XCTestCase {

    private var calendar: Calendar {
        var c = Calendar(identifier: .gregorian)
        c.timeZone = TimeZone(identifier: "Asia/Kolkata")!
        return c
    }

    /// Monday 26 October 2026, 09:00 local. Fixed so "tomorrow" is checkable.
    private var monday: Date {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = calendar.timeZone
        f.dateFormat = "yyyy-MM-dd HH:mm"
        return f.date(from: "2026-10-26 09:00")!
    }

    private func components(_ date: Date) -> DateComponents {
        calendar.dateComponents([.year, .month, .day, .hour, .minute], from: date)
    }

    // MARK: - Complete requests

    func testFullRequestBecomesAProposal() {
        let result = CalendarProposal.interpret("schedule a review meeting tomorrow at 10 AM",
                                                now: monday, calendar: calendar)
        guard case .ready(let proposal) = result else {
            return XCTFail("expected a proposal, got \(result)")
        }
        let parts = components(proposal.start)
        XCTAssertEqual(parts.day, 27)
        XCTAssertEqual(parts.hour, 10)
        XCTAssertEqual(parts.minute, 0)
        XCTAssertEqual(proposal.title, "Review")
        XCTAssertEqual(proposal.durationMinutes, 60)
    }

    func testPMIsHonoured() {
        let result = CalendarProposal.interpret("book a call tomorrow at 3pm",
                                                now: monday, calendar: calendar)
        guard case .ready(let proposal) = result else {
            return XCTFail("expected a proposal, got \(result)")
        }
        XCTAssertEqual(components(proposal.start).hour, 15)
    }

    func testDurationIsRead() {
        let result = CalendarProposal.interpret(
            "schedule a review meeting tomorrow at 10 AM for 45 minutes",
            now: monday, calendar: calendar)
        guard case .ready(let proposal) = result else {
            return XCTFail("expected a proposal, got \(result)")
        }
        XCTAssertEqual(proposal.durationMinutes, 45)
    }

    func testLocationIsReadWhenItIsNotATime() {
        let result = CalendarProposal.interpret(
            "schedule a review meeting tomorrow at 10 AM in the Design room",
            now: monday, calendar: calendar)
        guard case .ready(let proposal) = result else {
            return XCTFail("expected a proposal, got \(result)")
        }
        XCTAssertEqual(proposal.location.lowercased(), "design room")
    }

    // MARK: - What it refuses

    func testMissingTimeAsksRatherThanGuessing() {
        // The whole point: a date with no hour must not become 09:00.
        let result = CalendarProposal.interpret("schedule a review meeting tomorrow",
                                                now: monday, calendar: calendar)
        guard case .needsDetail(let question) = result else {
            return XCTFail("a missing time must ask, got \(result)")
        }
        XCTAssertTrue(question.lowercased().contains("what time"))
    }

    func testBareAmbiguousHourIsRefused() {
        // "at 10" is 10:00 or 22:00; both are plausible, so neither is assumed.
        let result = CalendarProposal.interpret("schedule a review meeting tomorrow at 10",
                                                now: monday, calendar: calendar)
        guard case .needsDetail = result else {
            return XCTFail("an ambiguous hour must ask, got \(result)")
        }
    }

    func testUnambiguousTwentyFourHourIsAccepted() {
        let result = CalendarProposal.interpret("schedule a review meeting tomorrow at 17",
                                                now: monday, calendar: calendar)
        guard case .ready(let proposal) = result else {
            return XCTFail("17:00 is unambiguous, got \(result)")
        }
        XCTAssertEqual(components(proposal.start).hour, 17)
    }

    func testTimeWithNoDateAsks() {
        let result = CalendarProposal.interpret("schedule a review meeting at 10 AM",
                                                now: monday, calendar: calendar)
        guard case .needsDetail(let question) = result else {
            return XCTFail("a missing date must ask, got \(result)")
        }
        XCTAssertTrue(question.lowercased().contains("which day"))
    }

    func testPastDateIsRefused() {
        let result = CalendarProposal.interpret("schedule a review meeting yesterday at 10 AM",
                                                now: monday, calendar: calendar)
        guard case .needsDetail(let question) = result else {
            return XCTFail("a past date must ask, got \(result)")
        }
        XCTAssertTrue(question.lowercased().contains("past"))
    }

    func testTaskSentenceIsNotACalendarWrite() {
        let result = CalendarProposal.interpret("create a task to submit the report tomorrow",
                                                now: monday, calendar: calendar)
        XCTAssertEqual(result, .notACalendarRequest)
    }

    func testReadQuestionIsNotACalendarWrite() {
        let result = CalendarProposal.interpret("what is on my calendar today?",
                                                now: monday, calendar: calendar)
        XCTAssertEqual(result, .notACalendarRequest)
    }

    // MARK: - Summary

    func testSummaryNamesTheDayTheTimesAndTheTitle() {
        let result = CalendarProposal.interpret("schedule a review meeting tomorrow at 10 AM",
                                                now: monday, calendar: calendar)
        guard case .ready(let proposal) = result else {
            return XCTFail("expected a proposal")
        }
        let summary = proposal.summary(calendar: calendar)
        XCTAssertTrue(summary.contains("Review"))
        XCTAssertTrue(summary.contains("Tuesday 27 October"))
        XCTAssertTrue(summary.contains("10:00"))
        XCTAssertTrue(summary.contains("11:00"), "the end time is part of what is being confirmed")
    }

    // MARK: - Title extraction

    func testTitleKeepsARealName() {
        XCTAssertEqual(
            CalendarProposal.title(from: "schedule a project review tomorrow at 10 AM",
                                   datePhrase: "tomorrow", timePhrase: "10 am"),
            "Project review")
    }

    func testTitleFallsBackToTheNounWhenNothingElseRemains() {
        // "Schedule a meeting tomorrow at 10" must not create an event called
        // "a" — with no other name, the noun is the title.
        XCTAssertEqual(
            CalendarProposal.title(from: "schedule a meeting tomorrow at 10 AM",
                                   datePhrase: "tomorrow", timePhrase: "10 am"),
            "Meeting")
    }
}