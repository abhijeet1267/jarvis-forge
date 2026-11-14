import XCTest
@testable import Vantage

/// Natural-language routing into the typed Mac actions.
///
/// The two rules that matter here are opposite failures: acting on a question
/// ("how do I open Safari") and refusing an unambiguous imperative ("open
/// Safari"), and both are tested by name.
final class MacCommandParserTests: XCTestCase {

    private let applications = ["Safari", "Finder", "Calculator", "TextEdit",
                                "Google Chrome", "Terminal", "Numbers"]

    private func parser() -> MacCommandParser { MacCommandParser(applications: applications) }

    private func action(_ text: String) -> (MacActionKind, MacActionArguments)? {
        if case .action(let kind, let args) = parser().parse(text) { return (kind, args) }
        return nil
    }

    // MARK: - Applications

    func test_open_application() {
        let parsed = action("Open Safari")
        XCTAssertEqual(parsed?.0, .openApplication)
        XCTAssertEqual(parsed?.1.application, "Safari")
    }

    func test_switch_to_application_is_activation_not_a_launch() {
        let parsed = action("Switch to Finder")
        XCTAssertEqual(parsed?.0, .activateApplication)
        XCTAssertEqual(parsed?.1.application, "Finder")
    }

    func test_quit_application() {
        XCTAssertEqual(action("quit TextEdit")?.0, .quitApplication)
    }

    func test_longer_application_names_win() {
        let parsed = action("open Google Chrome")
        XCTAssertEqual(parsed?.1.application, "Google Chrome")
    }

    func test_a_named_but_uninstalled_app_is_still_an_open_request() {
        // The executor reports "not installed"; the router must not silently
        // hand this to the model as prose.
        let parsed = action("open Zorblax")
        XCTAssertEqual(parsed?.0, .openApplication)
        XCTAssertEqual(parsed?.1.application, "Zorblax")
    }

    // MARK: - Windows

    func test_minimize_current_window() {
        let parsed = action("Minimize the current window")
        XCTAssertEqual(parsed?.0, .minimizeWindow)
    }

    func test_move_window_left_names_the_side() {
        let parsed = action("move this window to the left side of the screen")
        XCTAssertEqual(parsed?.0, .arrangeWindow)
        XCTAssertEqual(parsed?.1.side, .left)
    }

    func test_close_window_is_a_mutating_action() {
        let parsed = action("close the current window")
        XCTAssertEqual(parsed?.0, .closeWindow)
    }

    func test_list_windows() {
        XCTAssertEqual(action("show me the windows of Safari")?.0, .listWindows)
    }

    // MARK: - System

    func test_volume_with_percent() {
        let parsed = action("Set the volume to 30 percent")
        XCTAssertEqual(parsed?.0, .setVolume)
        XCTAssertEqual(parsed?.1.percent, 30)
    }

    func test_lower_the_volume_to_a_percentage() {
        XCTAssertEqual(action("lower the volume to 15%")?.1.percent, 15)
    }

    func test_mute_and_unmute_are_distinct() {
        XCTAssertEqual(action("mute the sound")?.1.flag, true)
        XCTAssertEqual(action("unmute")?.1.flag, false)
    }

    func test_foreground_question() {
        XCTAssertEqual(action("which app is in the foreground?")?.0, .frontmostApp)
    }

    func test_running_applications() {
        XCTAssertEqual(action("show me the applications currently running")?.0, .listRunningApps)
    }

    // MARK: - URLs

    func test_open_a_website() {
        let parsed = action("open example.com in Safari")
        XCTAssertEqual(parsed?.0, .openURL)
        XCTAssertEqual(parsed?.1.url, "https://example.com")
    }

    func test_a_file_name_is_not_mistaken_for_a_website() {
        // "invoice.pdf" has a dot but is not a domain, and launching a browser
        // for it would be a wrong action taken confidently.
        XCTAssertNil(action("open invoice.pdf"))
    }

    // MARK: - Folders

    func test_open_downloads_folder() {
        let parsed = action("Open my Downloads folder")
        XCTAssertEqual(parsed?.0, .openFolder)
        XCTAssertEqual(parsed?.1.path, "Downloads")
    }

    // MARK: - Questions must never act

    func test_instruction_seeking_is_never_executed() {
        for question in ["how do I open Safari", "how to minimize a window",
                         "what's the command to set the volume",
                         "explain how to close a window",
                         "tell me how to switch apps"] {
            XCTAssertEqual(parser().parse(question), .explanation, "\(question) was executed")
        }
    }

    func test_ordinary_conversation_is_left_alone() {
        for text in ["what is the capital of France", "hello", "write me a poem"] {
            XCTAssertEqual(parser().parse(text), .notMacControl, "\(text) was treated as control")
        }
    }

    // MARK: - Sequences

    func test_a_three_step_sequence_keeps_order() {
        guard case .plan(let steps) = parser().parse(
            "Open Safari, go to example.com, and set the volume to 30 percent") else {
            return XCTFail("a clear sequence was not parsed as one")
        }
        XCTAssertEqual(steps.map(\.kind), [.openApplication, .openURL, .setVolume])
        XCTAssertEqual(steps[1].arguments.url, "https://example.com")
        XCTAssertEqual(steps[2].arguments.percent, 30)
    }

    func test_a_partly_understood_sequence_is_not_executed() {
        // Half a plan is worse than none: nothing should run from this.
        XCTAssertEqual(parser().parse("open Safari and then make me a sandwich"),
                       .notMacControl)
    }
}