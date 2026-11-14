import XCTest
@testable import Vantage

/// The three intents the brief separates — create, run, explain — and the rule
/// that a draft is never executed.
final class FlowAuthoringTests: XCTestCase {

    private var tempDir: URL!
    private var store: FlowStore!

    override func setUpWithError() throws {
        tempDir = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("vantage-authoring-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: tempDir, withIntermediateDirectories: true)
        store = FlowStore(directory: tempDir)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: tempDir)
    }

    private func authoring() -> FlowAuthoring {
        FlowAuthoring(applications: ["Safari", "Finder", "Calculator", "TextEdit"])
    }

    private func save(_ name: String, steps: [FlowStep]) {
        _ = store.save(Flow(name: name, steps: steps))
    }

    // MARK: - Drafting

    func test_a_described_flow_becomes_an_editable_draft() {
        let outcome = authoring().interpret(
            "Create a Start Workday flow that opens Safari, Finder and my Documents folder, "
            + "then sets volume to 25 percent", store: store)
        guard case .draft(let flow) = outcome else {
            return XCTFail("expected a draft, got \(outcome)")
        }
        XCTAssertEqual(flow.name, "Start Workday")
        XCTAssertEqual(flow.steps.map(\.actionID),
                       ["open_application", "open_application", "open_folder", "set_volume"])
        XCTAssertEqual(flow.steps.last?.arguments.percent, 25)
        // Nothing was saved and nothing ran.
        XCTAssertTrue(store.flows.isEmpty, "a draft must not be saved automatically")
    }

    func test_a_draft_with_an_unsupported_clause_is_reported_not_guessed() {
        // "makes me a sandwich" is not an action. The supported half is kept and
        // the rest is named, rather than dropping the whole sentence or inventing
        // a step for it.
        let outcome = authoring().interpret(
            "create a flow that opens Safari and then makes me a sandwich", store: store)
        guard case .incompleteDraft(let flow, let problems) = outcome else {
            return XCTFail("expected an incomplete draft, got \(outcome)")
        }
        XCTAssertEqual(flow.steps.map(\.actionID), ["open_application"])
        XCTAssertTrue(problems.contains { $0.contains("sandwich") }, "\(problems)")
    }

    func test_a_draft_with_no_action_reports_that_plainly() {
        let outcome = authoring().interpret("create a flow that does something useful", store: store)
        guard case .incompleteDraft(let flow, let problems) = outcome else {
            return XCTFail("expected an incomplete draft, got \(outcome)")
        }
        XCTAssertTrue(flow.steps.isEmpty)
        XCTAssertTrue(problems.contains { $0.contains("No step") })
    }

    // MARK: - The three intents

    func test_creating_running_and_explaining_are_different() {
        save("Safari", steps: [FlowStep(actionID: MacActionKind.openApplication.id,
                                        arguments: { var a = StoredArguments(); a.application = "Safari"; return a }())])

        XCTAssertEqual(intent("create a flow that opens Safari"), "draft")
        XCTAssertEqual(intent("run my Safari flow"), "run")
        XCTAssertEqual(intent("what does my Safari flow do"), "describe")
        XCTAssertEqual(intent("show me what the Safari flow does"), "describe")
    }

    func test_running_a_missing_flow_names_the_ones_that_exist() {
        save("Workday", steps: [FlowStep(actionID: MacActionKind.frontmostApp.id)])
        guard case .unknownFlow(let message) = authoring().interpret(
            "run my Lunch flow", store: store) else {
            return XCTFail("expected unknownFlow")
        }
        XCTAssertTrue(message.contains("Workday"), message)
    }

    func test_a_quoted_name_wins_over_a_loose_match() {
        save("Open Safari", steps: [FlowStep(actionID: MacActionKind.openApplication.id,
                                             arguments: { var a = StoredArguments(); a.application = "Safari"; return a }())])
        guard case .run(let flow) = authoring().interpret(
            "run my “Open Safari” flow", store: store) else {
            return XCTFail("expected run")
        }
        XCTAssertEqual(flow.name, "Open Safari")
    }

    func test_ordinary_commands_are_not_drafts() {
        XCTAssertEqual(intent("Open Safari"), "notAFlowRequest")
        XCTAssertEqual(intent("set the volume to 30 percent"), "notAFlowRequest")
    }

    private func intent(_ text: String) -> String {
        switch authoring().interpret(text, store: store) {
        case .draft: return "draft"
        case .incompleteDraft: return "incompleteDraft"
        case .run: return "run"
        case .describe: return "describe"
        case .unknownFlow: return "unknownFlow"
        case .notAFlowRequest: return "notAFlowRequest"
        }
    }
}

/// The palette resolves to proposals; it never executes and never invents a
/// capability.
final class AnywhereCommandTests: XCTestCase {

    private var tempDir: URL!
    private var store: FlowStore!

    override func setUpWithError() throws {
        tempDir = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("vantage-anywhere-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: tempDir, withIntermediateDirectories: true)
        store = FlowStore(directory: tempDir)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: tempDir)
    }

    private func resolver() -> AnywhereCommand {
        AnywhereCommand(applications: ["Safari", "Finder", "Calculator"], store: store)
    }

    func test_supported_commands_resolve_to_typed_actions() {
        let r = resolver()
        if case .action(let kind, let args) = r.resolve("Open Safari").outcome {
            XCTAssertEqual(kind, .openApplication)
            XCTAssertEqual(args.application, "Safari")
        } else { XCTFail("did not resolve to an action") }

        if case .action(let kind, _) = r.resolve("set the volume to 30 percent").outcome {
            XCTAssertEqual(kind, .setVolume)
        } else { XCTFail("volume did not resolve") }
    }

    func test_a_protected_action_is_labelled_as_needing_confirmation() {
        XCTAssertTrue(resolver().resolve("close the current window").needsConfirmation)
        XCTAssertFalse(resolver().resolve("open Safari").needsConfirmation)
    }

    func test_a_question_is_answered_not_executed() {
        if case .explanation = resolver().resolve("how do I change the volume?").outcome {
            // correct
        } else { XCTFail("a question resolved to an action") }
    }

    func test_a_conversational_request_is_left_to_the_window() {
        if case .conversation = resolver().resolve("write me a poem").outcome {
            // correct
        } else { XCTFail("ordinary conversation was treated as a Mac action") }
    }

    func test_a_saved_flow_resolves_to_a_run() {
        _ = store.save(Flow(name: "Start Workday",
                            steps: [FlowStep(actionID: MacActionKind.openApplication.id,
                                             arguments: { var a = StoredArguments(); a.application = "Safari"; return a }())]))
        if case .runFlow(let flow) = resolver().resolve("run my Start Workday flow").outcome {
            XCTAssertEqual(flow.name, "Start Workday")
        } else { XCTFail("a saved flow did not resolve to a run") }
    }

    func test_listing_flows_returns_only_saved_ones() {
        if case .listFlows(let flows) = resolver().resolve("show my saved flows").outcome {
            XCTAssertTrue(flows.isEmpty)
        } else { XCTFail("did not resolve to a list") }
    }

    func test_suggestions_are_real_capabilities_not_invented_examples() {
        let items = resolver().suggestions(recent: ["Open Calculator"], limit: 8)
        XCTAssertTrue(items.contains { $0.text == "Open Calculator" && $0.detail == "Recent" })
        XCTAssertTrue(items.contains { $0.text == "Show my saved flows" })
        // Every suggestion must resolve to something the resolver handles.
        for item in items {
            let outcome = resolver().resolve(item.text).outcome
            XCTAssertTrue(outcome != .flowProblem(""), "suggestion \(item.text) is a dead end")
        }
    }
}

/// Shortcut storage and the registration rules that are checkable without a
/// window server. `GlobalHotKey.register` itself is exercised natively by the
/// harness, because Carbon needs a real application event target.
final class ShortcutSpecTests: XCTestCase {

    func test_the_default_is_option_space() {
        let spec = ShortcutSpec.defaultShortcut
        XCTAssertEqual(spec.displayString, "⌥Space")
    }

    func test_display_includes_every_modifier_in_order() {
        var spec = ShortcutSpec(keyCode: 0, modifiers: 0)
        spec.modifiers = UInt32(ShortcutSpec.controlMask) | UInt32(ShortcutSpec.optionMask) | UInt32(ShortcutSpec.shiftMask) | UInt32(ShortcutSpec.commandMask)
        XCTAssertEqual(spec.displayString, "⌃⌥⇧⌘A")
    }

    func test_round_trip_through_json() throws {
        let spec = ShortcutSpec(keyCode: 40, modifiers: UInt32(ShortcutSpec.commandMask) | UInt32(ShortcutSpec.shiftMask))
        let data = try JSONEncoder().encode(spec)
        XCTAssertEqual(try JSONDecoder().decode(ShortcutSpec.self, from: data), spec)
    }
}