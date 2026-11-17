import Foundation
import Combine

/// Sends a turn to the backend and brings back the answer.
///
/// This is the boundary between the conversation and the network. The view
/// asks for a reply; this decides what to send, how long to wait, and how to
/// turn a failure into something the person can read. The backend is the only
/// thing that knows how a turn is executed — this service never interprets a
/// sentence and never runs a tool.
@MainActor
final class ConversationService: ObservableObject {

    /// The conversation in progress.
    @Published private(set) var conversation = Conversation()

    /// A request is in flight.
    @Published private(set) var isThinking = false

    /// The last failure, in words a person can act on.
    @Published private(set) var lastError: String?

    /// The last thing the person said, kept so a failed turn can be retried
    /// without them retyping it.
    @Published private(set) var lastPrompt: String = ""

    /// The desktop plan that is holding for the person's decision, if any.
    ///
    /// A desktop step that acts — click, type, key press — is gated as level 3,
    /// so a mission stops at the first one. The turn has to stop with it: the
    /// backend accepts a supplied confirmation id as the approval, so sending
    /// that id back on our own would be approving a plan nobody was shown. This
    /// value is the only thing that carries a parked plan to the two methods
    /// that can answer it, and both of those are called by the person.
    @Published private(set) var pendingDesktopApproval: PendingDesktopApproval?

    /// Whether the backend can answer conversationally at all.
    ///
    /// This is not decoration. With no provider configured, the backend falls
    /// back to a Wikipedia lookup, and for a question like "explain how solar
    /// panels generate electricity" that lookup returns the **Solar power**
    /// article — a set of UK generation statistics, which is a different
    /// subject from the one that was asked about. The answer would look like
    /// an answer. The app has to know the difference, and say so.
    @Published private(set) var aiAvailable: Bool = true

    /// Whether the capability check has actually answered at least once.
    ///
    /// `aiAvailable` starts optimistic (`true`) so a configured provider is not
    /// announced as missing for the first second of the screen's life. But if
    /// the check has *never* succeeded, the default is a guess and the empty
    /// state must not present it as a fact -- it said "using your configured AI
    /// provider" on a screen that had not managed to read anything.
    @Published private(set) var capabilityKnown: Bool = false

    /// Which provider is answering, when one is. Shown so the person can see
    /// who is talking rather than assuming.
    @Published private(set) var providerName: String = ""

    /// Why the AI path is unavailable, in words a person can act on. Empty
    /// when it is available.
    @Published private(set) var aiUnavailableReason: String = ""

    private let api: APIClient

    /// How long a turn may take before it is called a timeout.
    ///
    /// The server's own budget for a conversation turn is 30 seconds, and the
    /// client's request timeout is 8 — long enough for a cold route but short
    /// enough that a person is not left staring at a spinner. The backend
    /// threads conversation history to the model, so the request itself is a
    /// single round trip: no streaming to wait for.
    private let timeout: TimeInterval

    init(api: APIClient = .shared, timeout: TimeInterval = 60) {
        self.api = api
        self.timeout = timeout
    }

    /// Send a turn and record the answer.
    ///
    /// The person's own words are added to the transcript *before* the request
    /// goes out, so a slow or failing turn still shows what was asked.
    @discardableResult
    func send(_ text: String) async -> ChatMessage? {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return nil }

        // Mac control is executed by the native app, not the backend, so it is
        // tried first: "open Safari" must actually open Safari, and the phone
        // surface must not be the thing that does it. `handleCommandCenter`
        // returns nil for anything that is not a Mac request (including a
        // question — "how do I open Safari" is answered, not executed), and
        // only a `verified`/`unverified`/`failed` result is shown as a tool
        // result. A refusal is shown too, because "I won't do that" is an
        // answer the person needs to see.
        if let result = await MacControlCoordinator.shared.handleCommandCenter(trimmed) {
            conversation.appendUser(trimmed)
            conversation.append(result.message, role: .tool,
                                meta: ChatMessage.MessageMeta(model: "mac-control"))
            return conversation.messages.last
        }
        return await turn(trimmed, appendUserTurn: true)
    }

    /// Re-send the last thing the person said.
    ///
    /// The failed turn stays in the transcript, so a retry does not hide that
    /// something went wrong; it adds the answer next to it.
    ///
    /// The question itself is **not** appended a second time: `send` already
    /// recorded it before the request failed. The previous version called
    /// `send(lastPrompt)` here, which appended the same user turn again — a
    /// retry left two copies of the question in the transcript.
    func retry() async {
        guard !lastPrompt.isEmpty else { return }
        // Drop the failed answer so the retry does not duplicate the failure.
        conversation.removeLastIfError()
        await turn(lastPrompt, appendUserTurn: false)
    }

    // MARK: - Desktop missions

    /// Plan and run a desktop instruction, or nil when this is not one.
    ///
    /// Three outcomes, all shown to the person:
    /// - `planned`: the mission ran (or parked) -- report the real step results.
    /// - `clarification`: nothing was planned; say what the planner needs.
    /// - 422: not a desktop instruction, so the caller falls through to /command.
    private func desktopMissionTurn(_ text: String) async -> ChatMessage? {
        struct Created: Decodable {
            let status: String
            let mission: APIModels.Mission?
            let clarification: String?
        }
        guard let created: Created = try? await api.post(
            "/missions/desktop",
            body: ["request": text],
            as: Created.self) else {
            return nil          // 422 or transport: let the chat path handle it
        }
        if created.status == "clarification" {
            conversation.appendUser(text)
            conversation.append(created.clarification ?? "That instruction needs more detail.",
                                role: .tool, meta: ChatMessage.MessageMeta(model: "desktop-planner"))
            return conversation.messages.last
        }
        guard let mission = created.mission else { return nil }

        conversation.appendUser(text)
        guard let summary = await runDesktopMission(mission) else {
            // The run failed and the reason is already the last turn.
            return conversation.messages.last
        }
        conversation.append(summary, role: .tool, meta: ChatMessage.MessageMeta(model: "desktop-mission"))
        return conversation.messages.last
    }

    /// Plan and run a desktop mission. Stops at the first step that needs a person.
    ///
    /// It runs the mission **once**. If the backend parks it at
    /// `awaiting_approval`, the plan is recorded in `pendingDesktopApproval`
    /// and the turn ends there: the person is shown what would run and answers
    /// through `approvePendingDesktop()` or `denyPendingDesktop()`.
    ///
    /// The previous version looped and resubmitted the confirmation id the
    /// backend had just issued. The backend checks that the id matches the one
    /// it minted, so it accepted it — but "the client fetched the id and sent
    /// it back" is not a person approving a level-3 action, and it meant every
    /// click and keystroke ran with nobody having seen the plan.
    private func runDesktopMission(_ mission: APIModels.Mission) async -> String? {
        guard let current = await runMissionOnce(mission.mission_id) else {
            return nil          // runMissionOnce already recorded the failure
        }
        if current.state == "awaiting_approval" {
            pendingDesktopApproval = Self.park(current)
            let parked = Self.pendingSteps(of: current)
            return Self.parkedNotice(parked) + "\n" + Self.describeSteps(parked)
        }
        return Self.describe(current)
    }

    /// Say a plan is parked without overstating how far it got.
    ///
    /// A plan can run its ungated steps (moving the pointer, for instance)
    /// before it reaches one that needs a person. "Nothing has run yet" is only
    /// true when no step finished, and the transcript must not claim either way
    /// without checking.
    private static func parkedNotice(_ steps: [PendingDesktopApproval.Step]) -> String {
        let ran = steps.contains { $0.status == "done" || $0.status == "skipped" }
        return ran
            ? "This plan is waiting for your approval. The steps marked done have "
              + "already run; nothing further will run until you approve."
            : "This plan is waiting for your approval and nothing has run yet."
    }

    /// Approve the plan that is waiting, then run it on from the step it parked on.
    ///
    /// The run continues until it finishes or parks again. A later park is a
    /// different step with a different confirmation, so it is handed back as a
    /// new pending approval instead of being answered — a 5-step mission may
    /// need the person's decision five times, and each one has to be theirs.
    ///
    /// A no-op when nothing is pending.
    func approvePendingDesktop() async {
        guard let pending = pendingDesktopApproval else { return }
        pendingDesktopApproval = nil
        lastError = nil
        isThinking = true
        defer { isThinking = false }

        // The id can only come from what the person was shown. When the backend
        // did not record one, there is nothing to approve *with*, and guessing
        // or re-fetching would be exactly the auto-approval this replaces.
        guard let confirmation = pending.confirmationId else {
            let reason = "This plan did not record a confirmation id, so it cannot "
                + "be approved from here. Open Mission Control to review it."
            lastError = reason
            conversation.appendError(reason)
            return
        }

        guard let resumed = await runMissionOnce(
            pending.missionId,
            approval: APIModels.MissionApproval(
                confirmation_id: confirmation, lease_id: "")) else {
            return          // runMissionOnce already recorded the failure
        }
        if resumed.state == "awaiting_approval" {
            pendingDesktopApproval = Self.park(resumed)
            conversation.append(
                "Approved one step. The plan is waiting again:\n"
                    + Self.describeSteps(Self.pendingSteps(of: resumed)),
                role: .tool, meta: ChatMessage.MessageMeta(model: "desktop-mission"))
            return
        }
        conversation.append(Self.describe(resumed), role: .tool,
                            meta: ChatMessage.MessageMeta(model: "desktop-mission"))
    }

    /// Deny the plan that is waiting.
    ///
    /// The backend needs a decision for the *parked step*, so the step id is
    /// sent with it; denying marks that step failed and blocks the mission. A
    /// no-op when nothing is pending.
    func denyPendingDesktop() async {
        guard let pending = pendingDesktopApproval else { return }
        pendingDesktopApproval = nil
        lastError = nil
        isThinking = true
        defer { isThinking = false }

        guard let stepId = pending.parkedStepId else {
            // The decide route answers per step, and it refused nothing here:
            // there is no step for the backend to deny. Saying "denied" would
            // claim a state change that did not happen.
            let reason = "This plan has no step awaiting a decision, so the deny "
                + "could not be sent. The mission is unchanged."
            lastError = reason
            conversation.appendError(reason)
            return
        }

        do {
            let _: APIModels.Mission = try await api.post(
                "/missions/\(pending.missionId)/steps/\(stepId)/decide",
                body: APIModels.StepDecisionRequest(decision: "deny", lease_id: nil),
                as: APIModels.Mission.self)
            conversation.append(
                "Denied. Nothing was run.",
                role: .tool, meta: ChatMessage.MessageMeta(model: "desktop-mission"))
        } catch {
            let reason = describe(error)
            lastError = reason
            conversation.appendError(reason)
        }
    }

    /// Run a mission once and return the mission as the backend left it.
    ///
    /// A failure is recorded as an error turn rather than returned as a
    /// mission, so a caller cannot mistake "the request failed" for "the
    /// mission finished".
    private func runMissionOnce(_ missionId: String,
                                approval: APIModels.MissionApproval? = nil) async -> APIModels.Mission? {
        do {
            return try await api.post(
                "/missions/\(missionId)/run",
                body: APIModels.MissionRunRequest(approval: approval),
                as: APIModels.Mission.self)
        } catch {
            let reason = describe(error)
            lastError = reason
            conversation.appendError(reason)
            return nil
        }
    }

    static func parkedConfirmation(_ mission: APIModels.Mission) -> String? {
        for step in mission.steps ?? [] {
            guard let detail = step.detail, let range = detail.range(of: "confirmation ") else { continue }
            let id = detail[range.upperBound...].prefix { !$0.isWhitespace }
            if !id.isEmpty && id != "unspecified" { return String(id) }
        }
        return nil
    }

    /// The steps of a parked mission, as the person needs to read them.
    ///
    /// Every step is carried, not only the parked one: a plan is approved as a
    /// plan, and the steps that already ran say how much has happened.
    static func pendingSteps(of mission: APIModels.Mission) -> [PendingDesktopApproval.Step] {
        (mission.steps ?? []).map { step in
            PendingDesktopApproval.Step(
                id: step.step_id,
                title: step.title,
                status: step.status,
                tool: step.tool ?? "",
                detail: step.detail ?? "",
                paramsSummary: (step.params ?? [:])
                    .sorted { $0.key < $1.key }
                    .map { "\($0.key): \($0.value.text)" }
                    .joined(separator: ", "))
        }
    }

    /// Record a parked mission as the pending decision.
    ///
    /// The steps and summary are captured here, once, so the view renders what
    /// was parked rather than re-reading a mission that may have moved on.
    static func park(_ mission: APIModels.Mission) -> PendingDesktopApproval {
        PendingDesktopApproval(
            mission: mission,
            missionId: mission.mission_id,
            confirmationId: parkedConfirmation(mission),
            steps: pendingSteps(of: mission),
            summary: summary(of: mission))
    }

    /// What approving would do, in one sentence.
    static func summary(of mission: APIModels.Mission) -> String {
        let names = (mission.steps ?? [])
            .filter { $0.status == "pending" }
            .compactMap { $0.tool }
            .filter { !$0.isEmpty }
        if names.isEmpty { return "Run the desktop plan." }
        return "Run \(names.joined(separator: ", ")) on this Mac."
    }

    private static func describeSteps(_ steps: [PendingDesktopApproval.Step]) -> String {
        guard !steps.isEmpty else { return "The plan has no steps to show." }
        return steps
            .map { step in
                let what = step.paramsSummary.isEmpty ? step.detail : step.paramsSummary
                let label = step.tool.isEmpty ? step.title : step.tool
                return "\(step.id) [\(step.status)] \(label)"
                    + (what.isEmpty ? "" : ": \(what)")
            }
            .joined(separator: "\n")
    }

    /// The mission as it ended, step by step.
    static func describe(_ mission: APIModels.Mission) -> String {
        let lines = (mission.steps ?? []).map { step -> String in
            let detail = step.detail ?? ""
            return "\(step.step_id) \(step.status): \(detail.isEmpty ? (step.tool ?? "") : detail)"
        }
        if lines.isEmpty { return "The desktop mission finished with no steps to report." }
        return "Desktop mission \(mission.state):\n" + lines.joined(separator: "\n")
    }

    /// One network turn. `appendUserTurn` is false only for a retry, whose
    /// question is already in the transcript.
    @discardableResult
    private func turn(_ trimmed: String, appendUserTurn: Bool) async -> ChatMessage? {
        if appendUserTurn {
            conversation.appendUser(trimmed)
        }
        lastPrompt = trimmed
        lastError = nil
        isThinking = true
        defer { isThinking = false }

        // A desktop instruction belongs to the desktop planner, not to the chat
        // planner. Measured before this branch existed: "move the cursor to
        // 400 300" came back from /command as `move_file` with source="the cursor"
        // -- a rename of a file that does not exist -- and the pointer never moved.
        //
        // /missions/desktop owns the question of what is a desktop instruction:
        // it answers 422 for anything else, so this branch falls through rather
        // than duplicating the verb list here where the two would drift.
        if let desktopTurn = await desktopMissionTurn(trimmed) {
            return desktopTurn
        }

        // The session id is what makes this a conversation rather than a
        // series of unrelated questions.
        let request = APIModels.CommandRequest(
            text: trimmed,
            sessionId: conversation.outgoingSessionID
        )

        do {
            let response: APIModels.CommandResponse = try await withTimeout(timeout) {
                // The turn gets its own transport timeout. The client's default
                // 8s request timeout is tuned for cheap reads and would cut a
                // model turn off mid-answer -- the backend's own chat budget is
                // 30s, so anything above that is the provider being slow, which
                // the timeout path below reports in words.
                try await self.api.post("/command", body: request, timeout: self.timeout)
            }
            // Adopt the session the server used, so the next turn joins this
            // conversation. The server mints one when the request did not
            // carry a session, which is the first turn of a new conversation.
            conversation.adoptSession(response.session_id)

            let meta = ChatMessage.MessageMeta(
                model: response.model_used ?? "",
                durationMS: response.duration_ms,
                awaitingApproval: response.needs_confirmation == true,
                blocked: response.decision == "BLOCK",
                sessionID: response.session_id ?? ""
            )
            let answer = response.reply?.trimmingCharacters(in: .whitespacesAndNewlines)
            var text = (answer?.isEmpty == false ? answer! : fallbackText(for: response))
            // A lookup answer is not a model answer, and the difference matters:
            // the Wikipedia fallback can return an article about a *different*
            // subject from the one that was asked about. Saying so is the
            // difference between an answer and something that looks like one.
            if !meta.isModelAnswer && !text.isEmpty {
                text += "\n\n(That came from a lookup, not the AI model — no provider "
                    + "is configured, so this is the closest reference I have.)"
            }
            conversation.appendAssistant(text, meta: meta)
            return conversation.messages.last
        } catch {
            let reason = describe(error)
            lastError = reason
            conversation.appendError(reason)
            return nil
        }
    }

    /// Read the backend's health and record whether the AI path is available.
    ///
    /// The backend reports `llm_enabled`, `ai_provider` and `llm_source` on
    /// `/health`. This reads them so the app can say, before a question is
    /// answered, that the answer will come from a lookup rather than a model.
    ///
    /// A failure here is NOT an error state: the health endpoint being
    /// unreachable says nothing about whether a provider is configured, and the
    /// request itself will report the real problem. The previous value is kept.
    func refreshCapability() async {
        do {
            let health: APIModels.HealthResponse = try await api.get("/health")
            capabilityKnown = true
            aiAvailable = health.llm_enabled ?? true
            providerName = health.ai_provider ?? ""
            if aiAvailable {
                aiUnavailableReason = ""
            } else {
                aiUnavailableReason = health.llm_source == "none"
                    ? "No AI provider is configured. Add a key in Settings → AI "
                      + "Providers to get conversational answers."
                    : "The AI provider (\(health.ai_provider ?? "unknown")) is not "
                      + "available right now."
            }
        } catch {
            // Leave the last known state alone: an unreachable health endpoint
            // is a connection problem, not a statement about the provider.
        }
    }

    /// Begin a new conversation.
    func startNewConversation() {
        conversation.startNewConversation()
        lastError = nil
        lastPrompt = ""
        // A plan parked in the old conversation belongs to it. Carrying it into
        // a fresh transcript would offer an approval with no visible plan above
        // it; the mission itself stays parked in Mission Control either way.
        pendingDesktopApproval = nil
    }

    /// Add a turn that is not a chat reply — an attachment's result or an
    /// approval's outcome.
    ///
    /// It belongs in the same transcript because it happened in the same
    /// conversation, and a result the person cannot see is a result they
    /// cannot trust. It does not touch the session id: an attachment is
    /// attached to the chat conversation, which is a different thing from the
    /// command session.
    ///
    /// A successful result is a `.tool` turn, not an `.assistant` one, so the
    /// transcript can show it as what it is instead of drawing "file read" in
    /// the same shape as a model answer.
    func appendResult(_ text: String, isError: Bool,
                       meta: ChatMessage.MessageMeta = .init()) {
        conversation.append(
            text,
            role: isError ? .error : .tool,
            meta: meta
        )
    }

    /// The text to show when the server answered but with no readable body.
    private func fallbackText(for response: APIModels.CommandResponse) -> String {
        if response.needs_confirmation == true {
            return "This needs your confirmation before I run it."
        }
        if response.blocked_reason?.isEmpty == false {
            return "I did not do that: \(response.blocked_reason!)"
        }
        if let intent = response.intent, intent != "unknown" {
            return "Done (\(intent))."
        }
        // Not "(no response)": that is a developer's token, and it used to be
        // drawn in a chat bubble as though it were the answer.
        return "I ran that, but the server did not say what it did. Check the "
            + "activity log for this turn before assuming it worked."
    }

    /// Turn a failure into a sentence a person can act on.
    ///
    /// The three cases a person can actually do something about are called out
    /// by name: the backend is not running, the credential is not accepted,
    /// and the provider refused or was slow. Everything else is reported
    /// plainly.
    private func describe(_ error: Error) -> String {
        if error is TurnTimeout {
            return "The answer did not arrive within \(Int(timeout))s. The AI provider "
                + "is slow or not responding — try again, or pick another provider in "
                + "Settings → AI Providers."
        }
        if let apiError = error as? APIError {
            switch apiError {
            case .unauthorized:
                return "The backend rejected this app's credential. "
                    + "Check that the API token exists at ~/.jarvis-forge/api_token."
            case .rateLimited:
                return "The provider is rate-limiting. Wait a moment and try again."
            case .transport(let msg):
                return "Cannot reach the backend (\(msg)). "
                    + "Is Vantage's service running?"
            case .httpError(let code, let detail):
                if code == 503 {
                    return "The AI provider is not configured or not available "
                        + "(\(detail)). Add one in Settings → AI Providers."
                }
                if code == 401 {
                    return "The AI provider rejected its key (\(detail))."
                }
                return "The backend returned \(code): \(detail)"
            case .invalidURL, .decodeError:
                return error.localizedDescription
            }
        }
        if error is CancellationError {
            return "The request was cancelled."
        }
        // A transport-level timeout (URLSession's own, or the system's) means
        // the same thing as our budget expiring: the provider did not answer.
        // Reporting it as "cannot reach the backend" would send a person to
        // debug a service that is running fine.
        if let urlError = error as? URLError, urlError.code == .timedOut {
            return "The answer did not arrive in time. The AI provider is slow or "
                + "not responding — try again, or pick another provider in "
                + "Settings → AI Providers."
        }
        return error.localizedDescription
    }

    /// Fail a task that has not finished in time.
    ///
    /// `URLSession` has its own request timeout, but the app should not depend
    /// on that alone: a reply that never arrives must not leave the composer
    /// disabled forever.
    private func withTimeout<T>(_ seconds: TimeInterval,
                                _ operation: @escaping () async throws -> T) async throws -> T {
        try await withThrowingTaskGroup(of: T.self) { group in
            group.addTask { try await operation() }
            group.addTask {
                try await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
                throw TurnTimeout()
            }
            guard let result = try await group.next() else {
                throw APIError.transport("no answer")
            }
            group.cancelAll()
            return result
        }
    }
}

/// A turn exceeded this app's own budget.
///
/// It is its own type, not an `APIError`, because it is not a statement about
/// the transport: the connection may be perfectly healthy while the model
/// behind it is slow. The person reading the transcript needs to be pointed at
/// the provider, not at the backend.
struct TurnTimeout: Error {}

/// A desktop plan that is parked, waiting for the person to answer it.
///
/// It carries the mission it belongs to as well as the flattened parts to
/// show: the mission is what `approvePendingDesktop()` resumes, and the steps
/// and summary are what a view renders. `confirmationId` may be nil when the
/// backend recorded no id — approving then is refused rather than invented,
/// because a confirmation this app made up is not an answer from anyone.
///
/// Not `Equatable`: it holds an `APIModels.Mission`, which is not, and nothing
/// needs to compare two whole plans — the view reads the parts it shows.
struct PendingDesktopApproval {

    /// One proposed step, as the person reads it.
    struct Step: Equatable, Identifiable {
        let id: String
        let title: String
        let status: String
        let tool: String
        let detail: String
        /// The step's parameters in one readable line, e.g. `text: hello`.
        let paramsSummary: String
    }

    /// The mission as the backend left it when it parked.
    let mission: APIModels.Mission
    let missionId: String
    let confirmationId: String?
    /// Every step of the plan, in order, with the status it had at the park.
    let steps: [Step]
    /// One sentence on what approving would do.
    let summary: String

    /// The step whose approval is being asked for: the first one still pending.
    var parkedStepId: String? {
        steps.first { $0.status == "pending" }?.id
    }
}
