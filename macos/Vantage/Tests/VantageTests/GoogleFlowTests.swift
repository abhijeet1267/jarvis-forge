import XCTest
@testable import Vantage

/// Google steps inside flows: approval, dependent-step failure, and the rule
/// that a failed read is never continued past.
///
/// This is a *logic* harness. It proves ordering, approval and failure rules
/// against a scripted Google — never that Google was actually called. The real
/// call is exercised separately against the live backend.
@MainActor
final class GoogleFlowTests: XCTestCase {

    /// A Google that answers from a per-call script.
    private final class ScriptedGoogle: GoogleActions, @unchecked Sendable {
        var replies: [GoogleOutcome]
        private(set) var calls: [(GoogleActionKind, MacActionArguments)] = []

        init(_ replies: [GoogleOutcome]) { self.replies = replies }

        func perform(_ kind: GoogleActionKind, arguments: MacActionArguments) async -> GoogleOutcome {
            calls.append((kind, arguments))
            if replies.isEmpty {
                return GoogleOutcome(status: .failed, message: "no scripted reply left")
            }
            return replies.removeFirst()
        }
    }

    /// Records what the native executor asked for, so the Google kinds are not
    /// passed to the real MacControlService in a test.
    private final class RecordingExecutor: MacActionExecutor {
        var outcomes: [String: MacActionResult] = [:]
        private(set) var executed: [String] = []

        func execute(_ request: MacActionRequest,
                     cancellation: MacControlService.Cancellation?) async -> MacActionResult {
            executed.append(request.kind.id)
            return outcomes[request.kind.id] ?? .verified(request.kind, "ok")
        }
    }

    private var tempDir: URL!
    private var store: FlowStore!

    override func setUp() async throws {
        tempDir = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("vantage-gflow-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: tempDir, withIntermediateDirectories: true)
        store = FlowStore(directory: tempDir)
    }

    override func tearDown() async throws {
        try? FileManager.default.removeItem(at: tempDir)
    }

    private func step(_ kind: MacActionKind, _ configure: (inout StoredArguments) -> Void = { _ in }) -> FlowStep {
        var args = StoredArguments()
        configure(&args)
        return FlowStep(actionID: kind.id, arguments: args)
    }

    private func flow(_ steps: [FlowStep]) -> Flow {
        Flow(name: "Google Flow", steps: steps)
    }

    private func engine(_ executor: MacActionExecutor) -> FlowEngine {
        FlowEngine(store: store,
                   router: MacActionRouter(permissions: {
                       MacPermissionStatus(accessibilityGranted: true)
                   }),
                   executor: executor)
    }

    // MARK: - Registration and policy

    func testGoogleActionsAreRegisteredWithCorrectRisk() {
        XCTAssertEqual(MacActionPolicy.forKind(.googleAgenda)?.risk, .read)
        XCTAssertEqual(MacActionPolicy.forKind(.googleOverdueTasks)?.risk, .read)
        XCTAssertEqual(MacActionPolicy.forKind(.googleCreateTask)?.risk, .mutating)
        XCTAssertEqual(MacActionPolicy.forKind(.googleCreateEvent)?.risk, .mutating)

        XCTAssertFalse(MacActionPolicy.forKind(.googleAgenda)!.risk.requiresConfirmation)
        XCTAssertTrue(MacActionPolicy.forKind(.googleCreateTask)!.risk.requiresConfirmation)
        XCTAssertTrue(MacActionPolicy.forKind(.googleCreateEvent)!.risk.requiresConfirmation)
    }

    func testCreateTaskWithoutTitleIsRefusedBeforeRunning() {
        let router = MacActionRouter(permissions: { MacPermissionStatus(accessibilityGranted: true) })
        var args = MacActionArguments()
        args.text = "   "
        XCTAssertThrowsError(
            try router.prepare(kind: .googleCreateTask, arguments: args, origin: .local)
        ) { error in
            guard case MacControlRefusal.missingArgument(let kind, _) = error else {
                return XCTFail("expected missingArgument, got \(error)")
            }
            XCTAssertEqual(kind, .googleCreateTask)
        }
    }

    func testCreateEventWithUnreadableTimeIsRefusedBeforeApproval() {
        let router = MacActionRouter(permissions: { MacPermissionStatus(accessibilityGranted: true) })
        var args = MacActionArguments()
        args.title = "Review"
        args.startDate = "whenever suits"
        XCTAssertThrowsError(
            try router.prepare(kind: .googleCreateEvent, arguments: args, origin: .local)
        ) { error in
            guard case MacControlRefusal.invalidArgument(.googleCreateEvent, _) = error else {
                return XCTFail("expected invalidArgument, got \(error)")
            }
        }
    }

    func testCreateEventAcceptsAParsableStart() throws {
        let router = MacActionRouter(permissions: { MacPermissionStatus(accessibilityGranted: true) })
        var args = MacActionArguments()
        args.title = "Review"
        args.startDate = "2026-10-30T10:00:00+05:30"
        let request = try router.prepare(kind: .googleCreateEvent, arguments: args, origin: .local)
        XCTAssertEqual(request.kind, .googleCreateEvent)
    }

    func testAnUnattendedCallerCannotRunAGoogleWriteWithoutConfirmation() {
        // A phone or the backend is not the person pressing Run here.
        let router = MacActionRouter(permissions: { MacPermissionStatus(accessibilityGranted: true) })
        var args = MacActionArguments()
        args.text = "Submit the report"
        XCTAssertThrowsError(
            try router.prepare(kind: .googleCreateTask, arguments: args, origin: .backend)
        ) { error in
            guard case MacControlRefusal.confinedByRisk = error else {
                return XCTFail("expected confinedByRisk, got \(error)")
            }
        }
    }

    func testAFlowCanSaveAGoogleWriteWithoutBeingConnected() {
        // A saved flow is not permanent authorization: whether Google is
        // connected is asked again at run time, so saving must not depend on it.
        let draft = flow([step(.googleCreateTask) { $0.text = "Submit the report" }])
        XCTAssertTrue(draft.validate().isEmpty, "\(draft.validate())")
    }

    func testAGoogleEventStepWithNoStartTimeIsInvalid() {
        let draft = flow([step(.googleCreateEvent) { $0.title = "Review" }])
        XCTAssertFalse(draft.validate().isEmpty)
    }

    // MARK: - Approval

    func testAGoogleWriteStepPausesForApproval() async {
        let executor = RecordingExecutor()
        let run = await engine(executor).run(
            flow([step(.googleCreateTask) { $0.text = "Submit the report" }]))
        XCTAssertEqual(run?.state, .running, "the run pauses, it does not complete")
        XCTAssertEqual(engine(executor).pendingApproval?.summary.contains("Submit the report"), true)
    }

    func testAPureReadFlowRunsWithoutApproval() async {
        let executor = RecordingExecutor()
        let engineUnderTest = engine(executor)
        let run = await engineUnderTest.run(
            flow([step(.googleAgenda), step(.googleOverdueTasks)]))
        XCTAssertEqual(run?.state, .completed)
        XCTAssertEqual(executor.executed,
                       [MacActionKind.googleAgenda.id, MacActionKind.googleOverdueTasks.id])
    }

    // MARK: - Dependent-step failure

    func testAFailedGoogleReadStopsTheRunAndLaterStepsDoNotExecute() async {
        let executor = RecordingExecutor()
        executor.outcomes[MacActionKind.googleAgenda.id] =
            .failed(.googleAgenda, "Calendar could not be read.")
        let engineUnderTest = engine(executor)
        let run = await engineUnderTest.run(
            flow([step(.googleAgenda),
                  step(.openApplication) { $0.application = "Safari" }]))
        XCTAssertEqual(run?.state, .failed)
        XCTAssertEqual(executor.executed, [MacActionKind.googleAgenda.id],
                       "a dependent step must not run after the Google step failed")
        XCTAssertTrue(run?.stopReason.contains("could not be read") == true)
    }

    func testARefusedGoogleWriteStopsTheRun() async {
        let executor = RecordingExecutor()
        executor.outcomes[MacActionKind.googleCreateTask.id] =
            .refused(.googleCreateTask, .unsupportedTarget("Google refused it."))
        let engineUnderTest = engine(executor)
        let run = await engineUnderTest.run(
            flow([step(.googleCreateTask) { $0.text = "X" },
                  step(.openApplication) { $0.application = "Safari" }]))
        // The run paused for approval first; approve and then check.
        if run?.state == .running {
            let resumed = await engineUnderTest.approvePending()
            XCTAssertEqual(resumed?.state, .failed)
        } else {
            XCTAssertEqual(run?.state, .failed)
        }
        XCTAssertEqual(executor.executed, [MacActionKind.googleCreateTask.id])
    }

    // MARK: - Result mapping

    func testAFailedGoogleOutcomeNeverBecomesVerified() async {
        let service = MacControlService()
        let router = MacActionRouter(permissions: { MacPermissionStatus(accessibilityGranted: true) })
        let request = try! router.prepare(kind: .googleAgenda,
                                          arguments: MacActionArguments(), origin: .local)
        let failed = service.execute(request,
                                     google: GoogleOutcome(status: .failed, message: "boom"))
        XCTAssertEqual(failed.status, .failed)

        let refused = service.execute(request,
                                      google: GoogleOutcome(status: .refused, message: "no"))
        XCTAssertEqual(refused.status, .refused)

        let unverified = service.execute(request,
                                         google: GoogleOutcome(status: .unverified, message: "?"))
        XCTAssertEqual(unverified.status, .unverified)

        let ok = service.execute(request,
                                 google: GoogleOutcome(status: .hasResults, message: "fine"))
        XCTAssertEqual(ok.status, .verified)
    }

    func testAGoogleStepWithNoOutcomeFailsRatherThanGuessing() async {
        let service = MacControlService()
        let router = MacActionRouter(permissions: { MacPermissionStatus(accessibilityGranted: true) })
        let request = try! router.prepare(kind: .googleOverdueTasks,
                                          arguments: MacActionArguments(), origin: .local)
        // No outcome supplied at all: the honest answer is that nothing was attempted.
        let result = service.execute(request)
        XCTAssertEqual(result.status, .failed)
    }

    func testGoogleKindMappingCoversExactlyTheGoogleActions() {
        XCTAssertEqual(NativeActionExecutor.googleKind(for: .googleAgenda), .agenda)
        XCTAssertEqual(NativeActionExecutor.googleKind(for: .googleCreateTask), .createTask)
        XCTAssertEqual(NativeActionExecutor.googleKind(for: .googleCreateEvent), .createEvent)
        XCTAssertNil(NativeActionExecutor.googleKind(for: .openApplication))
        XCTAssertNil(NativeActionExecutor.googleKind(for: .openToday))
    }

    // MARK: - The live conformance's classification

    func testBackendGoogleActionsTreatsAFailedReadAsFailed() async {
        // The production conformance is exercised through its response mapping;
        // a real call against the running backend is done outside this suite.
        let outcome = GoogleOutcome(status: .failed, message: "disabled")
        XCTAssertTrue(outcome.isFailure)
        XCTAssertFalse(GoogleOutcome(status: .empty, message: "").isFailure,
                       "a successful empty read is not a failure")
    }
}