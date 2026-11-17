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
        let summary = await runDesktopMission(mission)
        conversation.append(summary, role: .tool, meta: ChatMessage.MessageMeta(model: "desktop-mission"))
        return conversation.messages.last
    }

    /// Run a planned desktop mission, approving the parked steps it asks for.
    ///
    /// The desktop tools are LOW/EXTERNAL risk and still require an approval --
    /// that is the autonomy rule, not a defect -- so this drives the same
    /// decide-then-run loop a person would use in Mission Control. Bounded: a
    /// mission that keeps parking is reported, not retried forever.
    private func runDesktopMission(_ mission: APIModels.Mission) async -> String {
        var current = mission
        var lines: [String] = []
        for _ in 0..<8 {
            let run: APIModels.Mission? = try? await api.post(
                "/missions/\(current.mission_id)/run",
                body: APIModels.MissionRunRequest(approval: nil),
                as: APIModels.Mission.self)
            guard var latest = run else { break }
            if latest.state == "awaiting_approval" {
                guard let confirmation = Self.parkedConfirmation(latest) else { break }
                latest = (try? await api.post(
                    "/missions/\(latest.mission_id)/run",
                    body: APIModels.MissionRunRequest(
                        approval: .init(confirmation_id: confirmation, lease_id: "")),
                    as: APIModels.Mission.self)) ?? latest
            }
            current = latest
            if current.state != "awaiting_approval" { break }
        }
        for step in current.steps ?? [] {
            let detail = step.detail ?? ""
            lines.append("\(step.step_id) \(step.status): \(detail.isEmpty ? (step.tool ?? "") : detail)")
        }
        if lines.isEmpty { return "The desktop mission finished with no steps to report." }
        return "Desktop mission \(current.state ?? "unknown"):\n" + lines.joined(separator: "\n")
    }

    static func parkedConfirmation(_ mission: APIModels.Mission) -> String? {
        for step in mission.steps ?? [] {
            guard let detail = step.detail, let range = detail.range(of: "confirmation ") else { continue }
            let id = detail[range.upperBound...].prefix { !$0.isWhitespace }
            if !id.isEmpty && id != "unspecified" { return String(id) }
        }
        return nil
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
