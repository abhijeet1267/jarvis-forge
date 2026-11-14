import Foundation

/// One step's outcome inside a run.
///
/// A snapshot of what the executor actually returned, stored independently of the
/// flow definition — so editing or deleting a flow does not rewrite its history.
struct FlowStepOutcome: Codable, Identifiable, Equatable {
    let id: UUID
    let actionID: String
    /// The step's title and target *at the time it ran*, not re-derived later.
    let title: String
    let target: String
    let status: String
    let message: String
    let durationMS: Int
    let startedAt: Date

    var resultStatus: MacActionResult.Status {
        MacActionResult.Status(rawValue: status) ?? .failed
    }

    init(step: FlowStep, result: MacActionResult, startedAt: Date) {
        self.id = step.id
        self.actionID = step.actionID
        self.title = step.title
        self.target = step.summary
        self.status = result.status.rawValue
        self.message = result.message
        self.durationMS = result.durationMS
        self.startedAt = startedAt
    }
}

/// A single execution of a flow.
struct FlowRun: Codable, Identifiable, Equatable {
    enum State: String, Codable {
        case running, completed, completedWithErrors, cancelled, failed
    }

    let id: UUID
    let flowID: UUID
    let flowName: String
    /// The definition that was executed, kept so history is readable even after
    /// the flow is edited or deleted.
    let stepCount: Int
    let startedAt: Date
    var finishedAt: Date?
    var state: State
    var outcomes: [FlowStepOutcome]
    /// Included so "why did this stop" is answerable without re-reading a policy
    /// table that may since have changed.
    var stopReason: String

    var durationMS: Int {
        Int(((finishedAt ?? Date()).timeIntervalSince(startedAt)) * 1000)
    }

    var verifiedCount: Int { outcomes.filter { $0.resultStatus == .verified }.count }
}

/// Runs a flow's steps in order through the canonical action executor.
///
/// The engine owns no action logic. It asks `MacActionRouter` to validate each
/// step, `MacActionExecutor` to run it, and records exactly what came back. That
/// is what makes the flows, the palette and Control Mac share one path: there is
/// one executor, and this is a caller of it.
///
/// `MacActionExecutor` is a protocol so the ordering, failure, cancellation and
/// retry rules can be tested deterministically. The production conformance wraps
/// the real `MacControlService`.
protocol MacActionExecutor {
    func execute(_ request: MacActionRequest,
                 cancellation: MacControlService.Cancellation?) async -> MacActionResult
}

/// Production: the real native service. Validation has already happened, so this
/// is only the hand-off — it deliberately has no policy of its own.
///
/// Google steps are the exception to "hand-off only": their work is a network
/// round trip, so it happens *here*, asynchronously, before the synchronous
/// service is asked to record the outcome. The service still owns the result
/// vocabulary and never sees a raw HTTP response.
struct NativeActionExecutor: MacActionExecutor {
    private let service = MacControlService()
    private let google: GoogleActions

    init(google: GoogleActions = BackendGoogleActions()) {
        self.google = google
    }

    func execute(_ request: MacActionRequest,
                 cancellation: MacControlService.Cancellation?) async -> MacActionResult {
        if let kind = Self.googleKind(for: request.kind) {
            let outcome = await google.perform(kind, arguments: request.arguments)
            return await Task.detached(priority: .userInitiated) { [service] in
                service.execute(request, google: outcome, cancellation: cancellation)
            }.value
        }
        return await Task.detached(priority: .userInitiated) { [service] in
            service.execute(request, cancellation: cancellation)
        }.value
    }

    /// The Google operation a kind maps to, or nil for a native action.
    static func googleKind(for kind: MacActionKind) -> GoogleActionKind? {
        switch kind {
        case .googleAgenda: return .agenda
        case .googleOverdueTasks: return .overdueTasks
        case .googleCreateTask: return .createTask
        case .googleCreateEvent: return .createEvent
        default: return nil
        }
    }
}

@MainActor
final class FlowEngine: ObservableObject {

    /// What the screens read while a run is in progress.
    struct Live: Equatable {
        enum Phase: String, Equatable {
            case preparing, awaitingApproval, running, cancelling
            case completed, completedWithErrors, cancelled, failed
        }
        var phase: Phase
        var flowName: String
        var currentStep: Int          // 1-based; 0 before the first step
        var totalSteps: Int
        var currentTitle: String
        var outcomes: [FlowStepOutcome]
        var note: String
        var cancellable: Bool
    }

    @Published private(set) var live: Live?
    @Published private(set) var lastRun: FlowRun?

    private let router: MacActionRouter
    private let executor: MacActionExecutor
    private let store: FlowStore
    /// One run at a time. A second "Run" while a flow is running is refused rather
    /// than queued: two runs of a flow that creates things is how duplicates appear.
    private(set) var isRunning = false
    private var cancellation: MacControlService.Cancellation?

    /// A run that stopped because a step needs a person's approval.
    struct PendingApproval: Identifiable, Equatable {
        let id = UUID()
        let flow: Flow
        let requests: [MacActionRequest]
        let outcomes: [FlowStepOutcome]
        let nextIndex: Int
        let runID: UUID
        let startedAt: Date
        let summary: String
    }
    @Published var pendingApproval: PendingApproval?

    init(store: FlowStore,
         router: MacActionRouter? = nil,
         executor: MacActionExecutor? = nil) {
        self.store = store
        self.router = router ?? MacActionRouter()
        self.executor = executor ?? NativeActionExecutor()
    }

    // MARK: - Preflight

    /// Everything that can be known before running: is each step valid, is its
    /// permission present, and which steps will need approval.
    struct Preflight: Equatable {
        var problems: [String]
        var protectedSteps: [String]
        var missingPermissions: [String]
        var canRun: Bool { problems.isEmpty }
    }

    func preflight(_ flow: Flow, permissions: MacPermissionStatus) -> Preflight {
        var problems = flow.validate().map { $0.message }
        var protectedSteps: [String] = []
        var missing: Set<String> = []

        for step in flow.steps {
            guard let kind = step.kind, let policy = MacActionPolicy.forKind(kind) else { continue }
            if let permission = policy.permission, !permissions.permits(permission) {
                missing.insert(permission.displayName)
                problems.append("\(kind.displayName) needs \(permission.displayName) permission.")
            }
            if policy.risk.requiresConfirmation {
                protectedSteps.append("\(kind.displayName) — \(step.summary)")
            }
        }
        return Preflight(problems: problems,
                         protectedSteps: protectedSteps,
                         missingPermissions: missing.sorted())
    }

    // MARK: - Running

    /// Run a flow. Refuses a second concurrent run, validates first, and pauses at
    /// the first protected step so a person approves exactly that step.
    @discardableResult
    func run(_ flow: Flow, permissions: MacPermissionStatus? = nil) async -> FlowRun? {
        guard !isRunning else {
            return nil
        }
        let status = permissions ?? MacPermissionInspector.current()
        let check = preflight(flow, permissions: status)
        guard check.canRun else {
            live = Live(phase: .failed, flowName: flow.name, currentStep: 0,
                        totalSteps: flow.steps.count, currentTitle: "",
                        outcomes: [], note: check.problems.joined(separator: " "),
                        cancellable: false)
            return nil
        }

        isRunning = true
        let runID = UUID()
        let startedAt = Date()
        let token = MacControlService.Cancellation()
        cancellation = token
        live = Live(phase: .preparing, flowName: flow.name, currentStep: 0,
                    totalSteps: flow.steps.count, currentTitle: "",
                    outcomes: [], note: "Checking each step…", cancellable: true)

        defer {
            isRunning = false
            cancellation = nil
        }

        let prepared = await prepare(flow, status: status)
        if prepared.requests.count < flow.steps.count {
            live = Live(phase: .failed, flowName: flow.name, currentStep: 0,
                        totalSteps: flow.steps.count, currentTitle: "",
                        outcomes: [], note: prepared.problems.joined(separator: " "),
                        cancellable: false)
            let run = FlowRun(id: runID, flowID: flow.id, flowName: flow.name,
                              stepCount: flow.steps.count, startedAt: startedAt,
                              finishedAt: Date(), state: .failed, outcomes: [],
                              stopReason: prepared.problems.joined(separator: " "))
            store.record(run)
            lastRun = run
            return run
        }

        return await execute(flow: flow, requests: prepared.requests,
                             from: 0, outcomes: [], runID: runID, startedAt: startedAt,
                             token: token)
    }

    /// The person pressed "Run" on the approval prompt. Continues from exactly the
    /// step that was shown, with the same prepared requests.
    @discardableResult
    func approvePending() async -> FlowRun? {
        guard let waiting = pendingApproval else { return nil }
        pendingApproval = nil
        let token = MacControlService.Cancellation()
        cancellation = token
        isRunning = true
        defer { isRunning = false; cancellation = nil }
        // Everything from `nextIndex` on is pre-approved: the person saw the whole
        // prepared sequence and said yes. The executor's own risk check still runs.
        let remaining = Array(waiting.requests[(waiting.nextIndex)...])
        return await execute(flow: waiting.flow, requests: remaining,
                             from: 0, outcomes: waiting.outcomes,
                             runID: waiting.runID, startedAt: waiting.startedAt,
                             token: token, preApproved: true)
    }

    func cancelPending() {
        pendingApproval = nil
    }

    /// Stop after the current step. A native call already in flight finishes —
    /// killing it mid-write would be the un-undoable thing — and the run is
    /// recorded as cancelled, never as completed.
    func cancel() {
        guard isRunning else { return }
        cancellation?.cancel()
        if var current = live {
            current.phase = .cancelling
            current.note = "Stopping after the current step…"
            current.cancellable = false
            live = current
        }
    }

    // MARK: - Internals

    private struct Prepared {
        var requests: [MacActionRequest]
        var problems: [String]
    }

    private func prepare(_ flow: Flow, status: MacPermissionStatus) async -> Prepared {
        var requests: [MacActionRequest] = []
        var problems: [String] = []
        let router = MacActionRouter(permissions: { status })
        for step in flow.steps {
            guard let kind = step.kind else {
                problems.append("“\(step.actionID)” is not supported by this build.")
                continue
            }
            do {
                // `origin: .local` because a person is pressing Run in the app —
                // the same authorization a Control Mac button has. The risk gate
                // is therefore *not* what stops Class C/D here; `confirmed` is
                // false, so those steps surface an approval pause instead.
                requests.append(try router.prepare(kind: kind,
                                                   arguments: step.arguments.live,
                                                   origin: .local, confirmed: false))
            } catch let refusal as MacControlRefusal {
                problems.append(refusal.message)
            } catch {
                problems.append(error.localizedDescription)
            }
        }
        return Prepared(requests: requests, problems: problems)
    }

    private func execute(flow: Flow, requests: [MacActionRequest], from startIndex: Int,
                         outcomes initial: [FlowStepOutcome], runID: UUID,
                         startedAt: Date, token: MacControlService.Cancellation,
                         preApproved: Bool = false) async -> FlowRun {
        var outcomes = initial
        var stopReason = ""
        var state: FlowRun.State = .completed
        let total = flow.steps.count

        for (offset, request) in requests.enumerated() {
            let index = startIndex + offset
            let step = flow.steps[index]

            if token.isCancelled {
                state = .cancelled
                stopReason = "Cancelled before step \(index + 1)."
                break
            }

            // A protected step stops here until a person says yes — unless the
            // person already approved the prepared sequence. This is the only
            // place approval is decided, and it is decided by the policy table,
            // not by the surface.
            if request.risk.requiresConfirmation && !preApproved {
                live = Live(phase: .awaitingApproval, flowName: flow.name,
                            currentStep: index + 1, totalSteps: total,
                            currentTitle: step.title, outcomes: outcomes,
                            note: "Waiting for your approval to run “\(step.title)”.",
                            cancellable: true)
                pendingApproval = PendingApproval(
                    flow: flow, requests: requests, outcomes: outcomes,
                    nextIndex: index, runID: runID, startedAt: startedAt,
                    summary: "\(flow.name): \(step.title) — \(step.summary)")
                // Not a finished run: the caller resumes via approvePending().
                return FlowRun(id: runID, flowID: flow.id, flowName: flow.name,
                               stepCount: total, startedAt: startedAt, finishedAt: nil,
                               state: .running, outcomes: outcomes,
                               stopReason: "Awaiting approval for step \(index + 1).")
            }

            live = Live(phase: .running, flowName: flow.name, currentStep: index + 1,
                        totalSteps: total, currentTitle: step.title, outcomes: outcomes,
                        note: "Running “\(step.title)”…", cancellable: true)

            let stepStarted = Date()
            let result = await executor.execute(request, cancellation: token)
            let outcome = FlowStepOutcome(step: step, result: result, startedAt: stepStarted)
            outcomes.append(outcome)

            if result.status == .cancelled {
                state = .cancelled
                stopReason = "Cancelled during “\(step.title)”."
                break
            }
            if result.status == .failed || result.status == .refused {
                let keepGoing = flow.failurePolicy == .continueIndependent
                    && step.continueOnFailure
                if !keepGoing {
                    state = .failed
                    stopReason = "“\(step.title)” failed: \(result.message)"
                    break
                }
                state = .completedWithErrors
                stopReason = "“\(step.title)” failed but the flow continued: \(result.message)"
            }
            // An unverified step is not a failure — the action may well have
            // worked — but the run records it and the overall result says so.
            if result.status == .unverified && state == .completed {
                state = .completedWithErrors
            }
        }

        if state != .cancelled && state != .failed && outcomes.count < total {
            state = .failed
            stopReason = "Only \(outcomes.count) of \(total) steps ran."
        }

        let finished = Date()
        let run = FlowRun(id: runID, flowID: flow.id, flowName: flow.name,
                          stepCount: total, startedAt: startedAt, finishedAt: finished,
                          state: state, outcomes: outcomes, stopReason: stopReason)
        store.record(run)
        lastRun = run
        live = Live(phase: {
            switch state {
            case .completed: return .completed
            case .completedWithErrors: return .completedWithErrors
            case .cancelled: return .cancelled
            case .failed, .running: return .failed
            }
        }(), flowName: flow.name, currentStep: outcomes.count, totalSteps: total,
            currentTitle: outcomes.last?.title ?? "",
            outcomes: outcomes,
            note: stopReason.isEmpty
                ? "All \(outcomes.count) steps completed."
                : stopReason,
            cancellable: false)
        return run
    }

    // MARK: - Retry

    /// Whether re-running after this run is safe without asking.
    ///
    /// Read-only steps are always safe. A step that *succeeded* must not be
    /// repeated blindly — opening an app twice is harmless, but a future mutating
    /// step would not be — so a retry asks unless nothing mutating ran.
    func retrySafety(for run: FlowRun) -> (safe: Bool, reason: String) {
        let mutating = run.outcomes.compactMap { outcome -> String? in
            guard let kind = MacActionKind(rawValue: outcome.actionID),
                  let policy = MacActionPolicy.forKind(kind) else { return nil }
            return policy.risk.requiresConfirmation ? kind.displayName : nil
        }
        if mutating.isEmpty {
            return (true, "These steps only read or open things, so repeating them is safe.")
        }
        return (false, "This run included \(Set(mutating).sorted().joined(separator: ", ")), "
            + "which can change your Mac. Re-running repeats them from the start.")
    }
}