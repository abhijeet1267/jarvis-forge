import XCTest
@testable import Vantage

/// R3.3 — a section that FAILED to load must be a distinct presentation, never
/// collapsed into "loaded empty." Before the fix, `HomeView`'s per-section loaders
/// swallowed their errors (`catch { }`), so a failing `/devices` fetch rendered
/// exactly like a backend that genuinely has zero devices. These tests pin the
/// mapping that separates the three states, so the regression cannot return.
final class SectionPresentationTests: XCTestCase {

    // MARK: - The core invariant: failure is never "empty"

    func test_failed_fetch_is_never_presented_as_empty() {
        // itemCount is irrelevant when the fetch failed — the section must be `.failed`,
        // not `.empty` (which is what a confident "0 devices" is). This is the exact
        // bug R3.3 fixes: erroring fetches read as successful empties.
        let failedWithNothing = sectionPresentation(error: "Couldn't load devices.", itemCount: 0)
        let failedWithStale = sectionPresentation(error: "Couldn't load tasks.", itemCount: 5)

        XCTAssertEqual(failedWithNothing, .failed("Couldn't load devices."))
        XCTAssertEqual(failedWithStale, .failed("Couldn't load tasks."))
        // The regression would make these equal to `.empty`; assert they are not.
        XCTAssertNotEqual(failedWithNothing, .empty, "a failed fetch must not read as empty")
    }

    func test_failed_is_distinct_from_both_empty_and_populated() {
        let failed = sectionPresentation(error: "nope", itemCount: 0)
        XCTAssertNotEqual(failed, .empty)
        XCTAssertNotEqual(failed, .hasItems)
    }

    // MARK: - The genuine states are preserved (no over-correction)

    func test_clean_fetch_with_no_items_is_genuinely_empty() {
        XCTAssertEqual(sectionPresentation(error: nil, itemCount: 0), .empty)
    }

    func test_clean_fetch_with_items_is_has_items() {
        XCTAssertEqual(sectionPresentation(error: nil, itemCount: 1), .hasItems)
        XCTAssertEqual(sectionPresentation(error: nil, itemCount: 42), .hasItems)
    }

    // MARK: - Boundary: zero items is empty, one item is populated

    func test_one_item_is_the_boundary_between_empty_and_populated() {
        XCTAssertEqual(sectionPresentation(error: nil, itemCount: 0), .empty)
        XCTAssertEqual(sectionPresentation(error: nil, itemCount: 1), .hasItems)
    }
}
