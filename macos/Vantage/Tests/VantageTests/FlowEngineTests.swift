import XCTest
@testable import Vantage

/// An executor that answers from a script and records what it was asked to do.
///
/// This is a *logic* harness: it proves ordering, failure and cancellation rules.
/// It is never evidence that a real Mac action happened — the native harness is
/// what proves that.
private final class RecordingExecutor: MacActionExecutor {
    enum Reply {
        case verified
        case unverified
        case refused
        case failed
        case cancelledIfTokenIsSet
    }
    var replies: [String: Reply]      // keyed by action id
    private(set) var executed: [String] = []
    var onExecute: ((MacActionRequest) -> Void)?

    init(replies: [String: Reply]) { self.replies = replies }

    func execute(_ request: MacActionRequest,
                 cancellation: MacControlService.Cancellation?) async -> MacActionResult {
        executed.append(request.kind.id)
        onExecute?(request)
        let reply = replies[request.kind.id] ?? .verified
        switch reply {
        case .verified: return .verified(request.kind, "ok")
        case .unverified: return .unverified(request.kind, "asked")
        case .refused: return .refused(request.kind, .unsupportedTarget("no"))
        case .failed: return .failed(request.kind, "boom")
        case .cancelledIfTokenIsSet:
            return cancellation?.isCancelled == true
                ? .cancelled(request.kind)
                : .verified(request.kind, "ok")
        }
    }
}

@MainActor
final class FlowEngineTests: XCTestCase {

    private var tempDir: URL!
    private var store: FlowStore!

    override func setUp() async throws {
        tempDir = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("vantage-flows-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: tempDir, withIntermediateDirectories: true)
        store = FlowStore(directory: tempDir)
    }

    override func tearDown() async throws {
        try? FileManager.default.removeItem(at: tempDir)
    }

    private func flow(_ steps: [FlowStep], policy: FlowFailurePolicy = .stop) -> Flow {
        Flow(name: "Test Flow", steps: steps, failurePolicy: policy)
    }

    private func step(_ kind: MacActionKind, _ configure: (inout StoredArguments) -> Void = { _ in },
                      continueOnFailure: Bool = false) -> FlowStep {
        var args = StoredArguments()
        configure(&args)
        return FlowStep(actionID: kind.id, arguments: args,
                        continueOnFailure: continueOnFailure)
    }

    /// Permissions granted, so a window step reaches the executor in tests that
    /// are about ordering rather than permission.
    private func engine(_ executor: MacActionExecutor) -> FlowEngine {
        FlowEngine(store: store,
                   router: MacActionRouter(permissions: {
                       MacPermissionStatus(accessibilityGranted: true)
                   }),
                   executor: executor)
    }

    // MARK: - Ordering

    func test_steps_run_in_order() async {
        let executor = RecordingExecutor(replies: [:])
        let engine = engine(executor)
        let f = flow([step(.frontmostApp), step(.systemInfo), step(.listRunningApps)])
        let run = await engine.run(f, permissions: MacPermissionStatus(accessibilityGranted: true))
        XCTAssertEqual(executor.executed, ["frontmost_app", "system_info", "list_running_apps"])
        XCTAssertEqual(run?.state, .completed)
        XCTAssertEqual(run?.verifiedCount, 3)
    }

    func test_a_required_failure_stops_dependent_steps() async {
        let executor = RecordingExecutor(replies: ["open_application": .failed])
        let engine = engine(executor)
        let f = flow([step(.openApplication) { $0.application = "Safari" },
                      step(.setVolume) { $0.percent = 30 }])
        let run = await engine.run(f, permissions: MacPermissionStatus(accessibilityGranted: true))
        XCTAssertEqual(executor.executed, ["open_application"],
                       "a step after a required failure must not run")
        XCTAssertEqual(run?.state, .failed)
        XCTAssertEqual(run?.outcomes.count, 1)
    }

    func test_continue_policy_only_continues_marked_safe_steps() async {
        let executor = RecordingExecutor(replies: ["frontmost_app": .failed])
        let engine = engine(executor)
        let f = flow([step(.frontmostApp, continueOnFailure: true),
                      step(.systemInfo)],
                     policy: .continueIndependent)
        let run = await engine.run(f, permissions: MacPermissionStatus(accessibilityGranted: true))
        XCTAssertEqual(executor.executed, ["frontmost_app", "system_info"])
        XCTAssertEqual(run?.state, .completedWithErrors)
    }

    func test_continue_policy_does_not_continue_an_unmarked_failure() async {
        let executor = RecordingExecutor(replies: ["frontmost_app": .failed])
        let engine = engine(executor)
        let f = flow([step(.frontmostApp), step(.systemInfo)], policy: .continueIndependent)
        let run = await engine.run(f, permissions: MacPermissionStatus(accessibilityGranted: true))
        XCTAssertEqual(executor.executed, ["frontmost_app"])
        XCTAssertEqual(run?.state, .failed)
    }

    // MARK: - Validation and permissions

    func test_an_unknown_action_never_reaches_the_executor() async {
        let executor = RecordingExecutor(replies: [:])
        let engine = engine(executor)
        let bad = FlowStep(actionID: "run_shell", arguments: StoredArguments())
        let run = await engine.run(flow([bad]), permissions: MacPermissionStatus(accessibilityGranted: true))
        XCTAssertTrue(executor.executed.isEmpty, "an unknown action id reached the executor")
        XCTAssertEqual(run, nil, "a flow that cannot be validated must not record a success")
    }

    func test_a_missing_permission_blocks_before_any_step_runs() async {
        let executor = RecordingExecutor(replies: [:])
        let engine = engine(executor)
        let f = flow([step(.minimizeWindow)])
        let run = await engine.run(f, permissions: MacPermissionStatus(accessibilityGranted: false))
        XCTAssertTrue(executor.executed.isEmpty)
        XCTAssertNil(run)
        XCTAssertEqual(engine.live?.phase, .failed)
        XCTAssertTrue((engine.live?.note ?? "").contains("Accessibility"))
    }

    func test_invalid_arguments_are_refused_before_execution() async {
        let executor = RecordingExecutor(replies: [:])
        let engine = engine(executor)
        let f = flow([step(.openURL) { $0.url = "file:///etc/passwd" }])
        _ = await engine.run(f, permissions: MacPermissionStatus(accessibilityGranted: true))
        XCTAssertTrue(executor.executed.isEmpty)
    }

    // MARK: - Approval

    func test_a_protected_step_pauses_for_approval_and_does_not_run() async {
        let executor = RecordingExecutor(replies: [:])
        let engine = engine(executor)
        let f = flow([step(.openApplication) { $0.application = "Safari" },
                      step(.closeWindow)])
        _ = await engine.run(f, permissions: MacPermissionStatus(accessibilityGranted: true))
        XCTAssertEqual(executor.executed, ["open_application"],
                       "only the unprotected step may run before approval")
        XCTAssertNotNil(engine.pendingApproval)
        XCTAssertEqual(engine.pendingApproval?.nextIndex, 1)
    }

    func test_approving_runs_exactly_the_pending_step() async {
        let executor = RecordingExecutor(replies: [:])
        let engine = engine(executor)
        let f = flow([step(.closeWindow)])
        _ = await engine.run(f, permissions: MacPermissionStatus(accessibilityGranted: true))
        XCTAssertTrue(executor.executed.isEmpty)
        let run = await engine.approvePending()
        XCTAssertEqual(executor.executed, ["close_window"])
        XCTAssertEqual(run?.state, .completed)
    }

    func test_declining_approval_runs_nothing() async {
        let executor = RecordingExecutor(replies: [:])
        let engine = engine(executor)
        _ = await engine.run(flow([step(.closeWindow)]),
                             permissions: MacPermissionStatus(accessibilityGranted: true))
        engine.cancelPending()
        XCTAssertTrue(executor.executed.isEmpty)
        XCTAssertNil(engine.pendingApproval)
    }

    // MARK: - Cancellation

    func test_cancellation_stops_unstarted_steps() async {
        let executor = RecordingExecutor(replies: [:])
        let engine = engine(executor)
        // Cancel from inside the first step's execution, as a person pressing
        // Stop would while step one is running.
        var engineRef: FlowEngine?
        executor.onExecute = { _ in engineRef?.cancel() }
        let f = flow([step(.frontmostApp), step(.systemInfo), step(.listRunningApps)])
        engineRef = engine
        let run = await engine.run(f, permissions: MacPermissionStatus(accessibilityGranted: true))
        XCTAssertEqual(executor.executed, ["frontmost_app"], "a cancelled flow kept running")
        XCTAssertEqual(run?.state, .cancelled)
    }

    func test_cancellation_is_never_reported_as_completed() async {
        let executor = RecordingExecutor(replies: [:])
        let engine = engine(executor)
        var engineRef: FlowEngine?
        executor.onExecute = { _ in engineRef?.cancel() }
        engineRef = engine
        let run = await engine.run(flow([step(.frontmostApp), step(.systemInfo)]),
                                   permissions: MacPermissionStatus(accessibilityGranted: true))
        XCTAssertNotEqual(run?.state, .completed)
        XCTAssertEqual(run?.state, .cancelled)
    }

    // MARK: - Concurrency and retry safety

    func test_a_second_run_is_refused_while_one_is_in_flight() async {
        let executor = RecordingExecutor(replies: [:])
        let engine = engine(executor)
        let f = flow([step(.frontmostApp)])
        async let first = engine.run(f, permissions: MacPermissionStatus(accessibilityGranted: true))
        async let second = engine.run(f, permissions: MacPermissionStatus(accessibilityGranted: true))
        let results = await [first, second]
        XCTAssertEqual(executor.executed.count, 1,
                       "two concurrent runs must not both execute; got \(executor.executed)")
        XCTAssertTrue(results.contains { $0 == nil })
    }

    func test_retry_of_read_only_steps_is_safe() {
        let run = FlowRun(id: UUID(), flowID: UUID(), flowName: "r", stepCount: 1,
                          startedAt: Date(), finishedAt: Date(), state: .completed,
                          outcomes: [], stopReason: "")
        XCTAssertTrue(engine(RecordingExecutor(replies: [:])).retrySafety(for: run).safe)
    }

    func test_retry_after_a_protected_step_asks_first() {
        let step = self.step(.closeWindow)
        let outcome = FlowStepOutcome(step: step, result: .verified(.closeWindow, "ok"),
                                      startedAt: Date())
        let run = FlowRun(id: UUID(), flowID: UUID(), flowName: "r", stepCount: 1,
                          startedAt: Date(), finishedAt: Date(), state: .completed,
                          outcomes: [outcome], stopReason: "")
        let safety = engine(RecordingExecutor(replies: [:])).retrySafety(for: run)
        XCTAssertFalse(safety.safe)
        XCTAssertTrue(safety.reason.contains("Close window"))
    }

    // MARK: - Persistence

    func test_a_flow_survives_a_new_store_on_the_same_directory() throws {
        let f = flow([step(.openFolder) { $0.path = "Downloads" }])
        _ = store.save(f)
        let reopened = FlowStore(directory: tempDir)
        XCTAssertEqual(reopened.flows.count, 1)
        XCTAssertEqual(reopened.flows.first?.name, "Test Flow")
        XCTAssertEqual(reopened.flows.first?.steps.first?.arguments.path, "Downloads")
    }

    func test_editing_reordering_and_duplicating() throws {
        var f = flow([step(.frontmostApp), step(.systemInfo)])
        _ = store.save(f)
        f.steps.swapAt(0, 1)
        _ = store.save(f)
        XCTAssertEqual(store.flows.first?.steps.first?.actionID, "system_info")

        let copy = store.duplicate(f)
        XCTAssertNotEqual(copy.id, f.id)
        XCTAssertEqual(copy.steps.count, f.steps.count)
        XCTAssertEqual(store.flows.count, 2)
    }

    func test_deleting_a_flow_keeps_its_run_history() async {
        let executor = RecordingExecutor(replies: [:])
        let engine = engine(executor)
        let f = flow([step(.frontmostApp)])
        _ = store.save(f)
        _ = await engine.run(f, permissions: MacPermissionStatus(accessibilityGranted: true))
        XCTAssertEqual(store.history.count, 1)

        _ = store.delete(f)
        let reopened = FlowStore(directory: tempDir)
        XCTAssertTrue(reopened.flows.isEmpty)
        XCTAssertEqual(reopened.history.count, 1, "history must outlive the flow definition")
        XCTAssertEqual(reopened.history.first?.flowName, "Test Flow")
    }

    func test_a_corrupt_file_is_moved_aside_not_overwritten() throws {
        let url = tempDir.appendingPathComponent("flows.json")
        try "{ not json".write(to: url, atomically: true, encoding: .utf8)
        let reopened = FlowStore(directory: tempDir)
        XCTAssertNotNil(reopened.loadError)
        XCTAssertFalse(FileManager.default.fileExists(atPath: url.path),
                       "the unreadable file must be moved, not replaced in place")
        let moved = try FileManager.default.contentsOfDirectory(atPath: tempDir.path)
            .filter { $0.contains("unreadable") }
        XCTAssertEqual(moved.count, 1)
    }

    func test_history_is_capped_and_clearable() async {
        let executor = RecordingExecutor(replies: [:])
        let engine = engine(executor)
        let f = flow([step(.frontmostApp)])
        for _ in 0..<45 {
            _ = await engine.run(f, permissions: MacPermissionStatus(accessibilityGranted: true))
        }
        XCTAssertLessThanOrEqual(store.history.count, 40)
        store.clearHistory()
        XCTAssertTrue(store.history.isEmpty)
    }
}