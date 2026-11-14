import Foundation

/// Central HTTP client for the Vantage backend API.
/// Reads the master API token from disk and attaches it to every request.
final class APIClient: ObservableObject {

    static let shared = APIClient()

    private let session: URLSession
    private var token: String?
    private let tokenURL: URL

    var baseURL: String {
        // Production is http://127.0.0.1:8765. An internal JARVIS_BACKEND_PORT
        // env override (same seam as BackendManager) lets an isolated
        // integration test point the client at a local stub server without
        // changing the production default or adding a user-facing setting.
        let port = ProcessInfo.processInfo.environment["JARVIS_BACKEND_PORT"] ?? "8765"
        return "http://127.0.0.1:\(port)"
    }

    private init() {
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 8
        // The resource timeout is the overall cap for one task and cannot be
        // overridden per request. It has to sit above the longest request this
        // app deliberately makes -- a conversation turn, whose own budget is
        // 60s (the server's chat budget is 30s, and a cold route can approach
        // it). Ordinary requests still fail at the 8s request timeout, so
        // raising this cap changes nothing for them.
        config.timeoutIntervalForResource = 90
        session = URLSession(configuration: config)

        let home = ProcessInfo.processInfo.environment["JARVIS_HOME"]
            ?? NSHomeDirectory() + "/.jarvis-forge"
        tokenURL = URL(fileURLWithPath: home).appendingPathComponent("api_token")
        token = readToken()
    }

    // MARK: - Token

    private func readToken() -> String? {
        guard let data = try? Data(contentsOf: tokenURL),
              let content = String(data: data, encoding: .utf8) else {
            return nil
        }
        return content.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    var hasToken: Bool {
        (token ?? "").isEmpty == false
    }

    func refreshToken() -> String? {
        token = readToken()
        return token
    }

    // MARK: - HTTP methods

    func get<T: Decodable>(_ path: String, as type: T.Type = T.self) async throws -> T {
        let req = try makeRequest(path: path, method: "GET")
        return try await perform(req, as: type)
    }

    /// - Parameter timeout: Overrides the session's 8s request timeout for this
    ///   one call. Used by a conversation turn, which legitimately waits on a
    ///   model. Without it, a provider taking longer than 8 seconds is killed
    ///   by the transport before the turn's own 60s budget is consulted.
    func post<T: Decodable>(_ path: String, body: Encodable? = nil,
                            timeout: TimeInterval? = nil,
                            as type: T.Type = T.self) async throws -> T {
        var req = try makeRequest(path: path, method: "POST", body: body)
        if let timeout { req.timeoutInterval = timeout }
        return try await perform(req, as: type)
    }

    func patch<T: Decodable>(_ path: String, body: Encodable? = nil,
                             as type: T.Type = T.self) async throws -> T {
        let req = try makeRequest(path: path, method: "PATCH", body: body)
        return try await perform(req, as: type)
    }

    func put<T: Decodable>(_ path: String, body: Encodable? = nil,
                           as type: T.Type = T.self) async throws -> T {
        let req = try makeRequest(path: path, method: "PUT", body: body)
        return try await perform(req, as: type)
    }

    func delete<T: Decodable>(_ path: String, as type: T.Type = T.self) async throws -> T {
        let req = try makeRequest(path: path, method: "DELETE")
        return try await perform(req, as: type)
    }

    func getRaw(_ path: String) async throws -> Data {
        let req = try makeRequest(path: path, method: "GET")
        let (data, response) = try await session.data(for: req)
        try validate(response: response, data: data)
        return data
    }

    // MARK: - Backend identity

    /// Who is answering on the backend port. `GET /health/identity` is
    /// deliberately unauthenticated, so this works before a token exists (and
    /// when the stored token is rejected) — which is the whole point: the
    /// question "is this process actually Vantage?" has to be answerable
    /// without credentials, or a stale process and a wrong token look alike.
    ///
    /// The token is sent when present (the route ignores it), but never
    /// required. The timeout is short: this is a liveness probe called on
    /// every health poll, not a data fetch.
    func backendIdentity() async throws -> APIModels.BackendIdentityResponse {
        guard let url = URL(string: baseURL + "/health/identity") else {
            throw APIError.invalidURL(baseURL + "/health/identity")
        }
        var req = URLRequest(url: url)
        req.httpMethod = "GET"
        req.timeoutInterval = 3
        req.setValue("application/json", forHTTPHeaderField: "Accept")
        if let tok = refreshToken(), !tok.isEmpty {
            req.setValue(tok, forHTTPHeaderField: "X-JARVIS-Token")
        }
        let (data, response) = try await session.data(for: req)
        try validate(response: response, data: data)
        do {
            return try JSONDecoder().decode(APIModels.BackendIdentityResponse.self, from: data)
        } catch let error as DecodingError {
            throw APIError.decodeError(Self.describe(error, path: "/health/identity"))
        }
    }

    // MARK: - Google Calendar + Tasks

    func googleStatus() async throws -> APIModels.GoogleStatus {
        try await get("/google/status")
    }

    /// Start an authorization. Returns the URL a person must open.
    func googleConnectBegin() async throws -> APIModels.GoogleConnectBegin {
        try await post("/google/connect/begin", body: Optional<String>.none)
    }

    func googleDisconnect() async throws -> APIModels.GoogleDisconnect {
        try await post("/google/disconnect", body: Optional<String>.none)
    }

    func googleEvents(date: String = "today", days: Int = 1) async throws -> APIModels.GoogleEventsResponse {
        try await get("/google/calendar/events?date=\(date)&days=\(days)")
    }

    func googleTasks(showCompleted: Bool = false) async throws -> APIModels.GoogleTasksResponse {
        try await get("/google/tasks?show_completed=\(showCompleted)&limit=100")
    }

    /// One service's read, asked for on its own.
    ///
    /// The Connections screen separates Calendar and Tasks because their
    /// outcomes genuinely differ: measured on this machine, one project had
    /// Tasks enabled and Calendar disabled, so a single combined "Google is
    /// connected" pill was true and useless at the same time. Each of these is
    /// the same read the Google screen performs, so the test reports what that
    /// read reports — no second code path, no invented health.
    func testGoogleCalendar() async throws -> APIModels.GoogleEventsResponse {
        try await googleEvents()
    }

    func testGoogleTasks() async throws -> APIModels.GoogleTasksResponse {
        try await googleTasks()
    }

    /// Create an event through the canonical calendar tool route.
    ///
    /// The provider behind it is whatever Vantage actually has connected. When
    /// that is Google's read-only connection the route answers `ok: false`
    /// with the tool's own explanation; it never invents a success.
    func createCalendarEvent(title: String, start: String,
                             durationMinutes: Int = 60,
                             location: String = "") async throws -> APIModels.CalendarEventCreateResult {
        struct Body: Encodable {
            let title: String; let start: String
            let duration_minutes: Int; let location: String
        }
        return try await post("/calendar/events",
                              body: Body(title: title, start: start,
                                         duration_minutes: durationMinutes,
                                         location: location))
    }

    func googleTaskLists() async throws -> APIModels.GoogleTaskListsResponse {
        try await get("/google/tasks/lists")
    }

    func googleTaskCreate(_ body: APIModels.GoogleTaskCreateRequest) async throws -> APIModels.GoogleTaskCreateResult {
        try await post("/google/tasks", body: body)
    }

    func googleTaskComplete(_ taskId: String, listId: String = "") async throws -> APIModels.GoogleTaskCreateResult {
        struct Body: Encodable { let list_id: String }
        return try await post("/google/tasks/\(taskId)/complete", body: Body(list_id: listId))
    }

    /// Delete a task. Google's API supports it; Vantage's route does not.
    ///
    /// This exists so the screen can ask the backend rather than assume. The
    /// answer is a 404 today, and the UI says "not offered" from that fact
    /// instead of hiding a capability it cannot deliver.
    func googleTaskDelete(_ taskId: String, listId: String = "") async throws -> [String: AnyCodable] {
        var path = "/google/tasks/\(taskId)?confirm=true"
        if !listId.isEmpty {
            path += "&list_id=\(listId.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? listId)"
        }
        return try await delete(path, as: [String: AnyCodable].self)
    }

    // MARK: - Provider categories

    func providerCategories() async throws -> APIModels.CategoryTabs {
        try await get("/ai/provider-categories")
    }

    /// Save one category. The key, when present, is written to the Keychain by
    /// the backend and is never returned.
    func saveProviderCategory(_ category: String,
                              provider: String, model: String, baseURL: String,
                              enabled: Bool, timeout: Double,
                              options: [String: String],
                              apiKey: String?) async throws -> APIModels.CategorySaveResult {
        struct Body: Encodable {
            let provider: String; let model: String; let base_url: String
            let enabled: Bool; let timeout: Double
            let options: [String: String]; let api_key: String?
        }
        return try await put("/ai/provider-categories/\(category)",
                             body: Body(provider: provider, model: model,
                                        base_url: baseURL, enabled: enabled,
                                        timeout: timeout, options: options,
                                        api_key: (apiKey?.isEmpty ?? true) ? nil : apiKey))
    }

    /// Perform the category's real operation.
    func testProviderCategory(_ category: String) async throws -> APIModels.CategoryTestResult {
        struct Empty: Codable {}
        return try await post("/ai/provider-categories/\(category)/test", body: Empty(),
                              timeout: 180)
    }

    func discoverProviderModels(_ category: String) async throws -> APIModels.ModelDiscovery {
        struct Empty: Codable {}
        return try await post("/ai/provider-categories/\(category)/discover", body: Empty(),
                              timeout: 90)
    }

    // MARK: - Capability routing

    func capabilityRouting() async throws -> APIModels.CapabilityRouting {
        try await get("/ai/capability-routing")
    }

    func assignCapability(task: String, providerID: String, model: String,
                          baseURL: String = "") async throws -> APIModels.CapabilityAssignment {
        struct Body: Encodable { let provider_id: String; let model: String
                                 let base_url: String }
        struct Reply: Codable { let saved: Bool?; let assignment: APIModels.CapabilityAssignment? }
        let reply: Reply = try await post("/ai/capability-routing/\(task)",
                                          body: Body(provider_id: providerID, model: model,
                                                     base_url: baseURL))
        guard let assignment = reply.assignment else {
            throw APIError.decodeError("The backend saved the assignment but did not return it.")
        }
        return assignment
    }

    /// Perform the real bounded request. This is what turns *declared* into
    /// *verified* — nothing else does.
    func testCapability(task: String) async throws -> APIModels.CapabilityTestResult {
        struct Empty: Codable {}
        return try await post("/ai/capability-routing/\(task)/test", body: Empty())
    }

    // MARK: - n8n automation

    func n8nStatus(probe: Bool = true) async throws -> APIModels.N8NStatus {
        try await get("/n8n/status?probe=\(probe)")
    }

    func n8nWorkflows() async throws -> APIModels.N8NWorkflowList {
        try await get("/n8n/workflows")
    }

    /// Run one approved workflow. `POST …/run` is SameOriginProtected, which
    /// `makeRequest` satisfies by sending the backend's own origin.
    /// A 409 means "this one needs confirmation"; the caller shows the prompt
    /// and calls again with `confirmed: true`.
    func n8nRun(_ key: String, inputs: [String: String],
                confirmed: Bool) async throws -> APIModels.N8NRunResult {
        struct Body: Encodable {
            let inputs: [String: String]
            let confirmed: Bool
        }
        return try await post("/n8n/workflows/\(key)/run",
                              body: Body(inputs: inputs, confirmed: confirmed))
    }

    /// Enable or disable one workflow in Vantage (never in n8n).
    func n8nSetWorkflowEnabled(_ key: String, enabled: Bool) async throws -> [String: AnyCodable] {
        struct Body: Encodable { let enabled: Bool }
        return try await put("/n8n/workflows/\(key)", body: Body(enabled: enabled))
    }

    /// Vantage's own record of this workflow's latest run.
    func n8nWorkflowStatus(_ key: String) async throws -> APIModels.N8NWorkflowStatus {
        try await get("/n8n/workflows/\(key)/status")
    }

    // MARK: - MCP

    /// The remote connector's real state. `probe: false` reports configuration
    /// without a network round trip.
    func mcpStatus(probe: Bool = true) async throws -> APIModels.MCPStatus {
        try await get("/mcp/status?probe=\(probe)")
    }

    /// Save the endpoint and allowlist. `PUT /mcp/config` is
    /// `SameOriginProtected`, which `makeRequest` satisfies by sending the
    /// backend's own origin as `Origin`.
    ///
    /// The token is not a parameter here, on purpose: the route does not accept
    /// one. A 422 carries the server's own sentence in `detail`, which
    /// `APIError.httpError` preserves.
    func mcpConfigure(url: String, allowedTools: String,
                      name: String) async throws -> APIModels.MCPConfigSaveResult {
        try await put("/mcp/config", body: APIModels.MCPConfigRequest(
            url: url, allowed_tools: allowedTools, name: name))
    }

    // MARK: - Confirmations

    /// The confirmations waiting for a person.
    func pendingConfirmations() async throws -> [APIModels.Confirmation] {
        try await get("/confirmations", as: [APIModels.Confirmation].self)
    }

    // MARK: - Planner (Epic 1)

    func plannerTasks() async throws -> [APIModels.PlannerTask] {
        try await get("/planner/tasks", as: [APIModels.PlannerTask].self)
    }

    func plannerToday() async throws -> APIModels.PlannerAgenda {
        try await get("/planner/today", as: APIModels.PlannerAgenda.self)
    }

    /// Turn a goal into ordered planner tasks. Returns the created tasks.
    func planGoal(_ goal: String, project: String = "") async throws -> [APIModels.PlannerTask] {
        struct Body: Codable { let goal: String; let project: String }
        struct Reply: Codable { let tasks: [APIModels.PlannerTask]? }
        let reply: Reply = try await post("/planner/plan-goal",
                                          body: Body(goal: goal, project: project), as: Reply.self)
        return reply.tasks ?? []
    }

    // MARK: - Contacts & posts

    /// Create a calendar event. Returns the backend's own message.
    func createEvent(title: String, start: String) async throws -> String {
        struct Body: Codable { let title: String; let start: String }
        struct Reply: Codable { let ok: Bool?; let message: String? }
        let reply: Reply = try await post("/calendar/events",
                                          body: Body(title: title, start: start),
                                          as: Reply.self)
        return reply.message ?? ""
    }

    /// Cancel an event. Irreversible, so it asks before it runs.
    func cancelEvent(_ uid: String) async throws -> String {
        struct Body: Codable { let confirm: Bool }
        struct Reply: Codable { let ok: Bool?; let message: String? }
        let encoded = uid.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? uid
        let reply: Reply = try await post("/calendar/events/\(encoded)/cancel",
                                          body: Body(confirm: true), as: Reply.self)
        return reply.message ?? ""
    }

    /// Plan the day, open the next task's project, and report the agent prompt.
    /// It does not start the agent — that action asks first.
    func beginWork() async throws -> APIModels.BeginWorkResult {
        struct Empty: Codable {}
        return try await post("/planner/begin-work", body: Empty(),
                              as: APIModels.BeginWorkResult.self)
    }

    /// The specialised agents and their real tool counts.
    func agents() async throws -> [APIModels.AgentSummary] {
        let reply: APIModels.AgentList = try await get("/agents", as: APIModels.AgentList.self)
        return reply.agents ?? []
    }

    /// Whether a coding agent is installed, and what it would need.
    func codingAvailability() async throws -> APIModels.CodingAvailability {
        try await get("/coding/availability", as: APIModels.CodingAvailability.self)
    }

    /// Hand a development goal to the coding agent and report what it did.
    func startCodingTask(goal: String, path: String = "") async throws -> APIModels.CodingTaskResult {
        struct Body: Codable { let goal: String; let path: String }
        return try await post("/coding/tasks", body: Body(goal: goal, path: path),
                              as: APIModels.CodingTaskResult.self)
    }

    /// Which known applications are installed and running on this Mac.
    func appStatus() async throws -> [APIModels.AppStatus] {
        let reply: APIModels.AppStatusList =
            try await get("/apps/status", as: APIModels.AppStatusList.self)
        return reply.apps ?? []
    }

    /// Bring a running application forward.
    func focusApp(_ app: String) async throws {
        struct Body: Codable { let text: String }
        struct Reply: Codable { let reply: String? }
        let _: Reply = try await post("/command", body: Body(text: "switch to \(app)"),
                                      as: Reply.self)
    }

    /// Launch an application.
    func openApp(_ app: String) async throws {
        struct Body: Codable { let text: String }
        struct Reply: Codable { let reply: String? }
        let _: Reply = try await post("/command", body: Body(text: "open \(app)"),
                                      as: Reply.self)
    }

    /// Search the mailbox. A blank query means recent messages.
    func inbox(_ query: String = "") async throws -> [APIModels.InboxMessage] {
        let encoded = query.addingPercentEncoding(
            withAllowedCharacters: .urlQueryAllowed) ?? ""
        let reply: APIModels.InboxResponse = try await get("/email/search?query=\(encoded)",
                                                          as: APIModels.InboxResponse.self)
        return reply.results ?? []
    }

    /// One message in full.
    func emailBody(_ uid: String) async throws -> APIModels.EmailBody? {
        let encoded = uid.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? uid
        let reply: APIModels.EmailReadResponse = try await get("/email/\(encoded)",
                                                               as: APIModels.EmailReadResponse.self)
        return reply.message
    }

    func contacts() async throws -> [APIModels.Contact] {
        try await get("/contacts", as: [APIModels.Contact].self)
    }

    func addContact(name: String, address: String) async throws {
        struct Body: Codable { let name: String; let address: String; let aliases: [String] }
        struct Reply: Codable { let contact_id: String? }
        let _: Reply = try await post("/contacts",
                                      body: Body(name: name, address: address, aliases: []),
                                      as: Reply.self)
    }

    func prepareCall(_ name: String) async throws -> APIModels.CallBrief {
        struct Empty: Codable {}
        let encoded = name.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? name
        return try await post("/contacts/\(encoded)/prepare-call", body: Empty(),
                              as: APIModels.CallBrief.self)
    }

    /// Prepare for a meeting: the event, related mail and tasks, and why.
    /// It prepares; Vantage does not attend the meeting.
    func prepareMeeting(_ meeting: String) async throws -> APIModels.MeetingBrief {
        struct Body: Codable { let meeting: String }
        return try await post("/meetings/prepare", body: Body(meeting: meeting),
                              as: APIModels.MeetingBrief.self)
    }

    /// Draft a recap from completed work. With nothing completed, no draft is
    /// created — the reason is returned instead.
    func draftRecap(days: Int = 7) async throws -> APIModels.RecapResult {
        struct Body: Codable { let days: Int }
        return try await post("/posts/recap", body: Body(days: days),
                              as: APIModels.RecapResult.self)
    }

    /// Draft a personalised outreach email and save it to the mailbox drafts.
    /// It does not send.
    func draftOutreach(_ name: String, purpose: String = "") async throws
        -> APIModels.OutreachDraft {
        struct Body: Codable { let purpose: String }
        let encoded = name.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? name
        return try await post("/contacts/\(encoded)/outreach",
                              body: Body(purpose: purpose),
                              as: APIModels.OutreachDraft.self)
    }

    /// Record what was said on a call; action lines become tasks.
    func saveCallNotes(_ name: String, notes: String) async throws
        -> APIModels.CallNotesResult {
        struct Body: Codable { let notes: String }
        let encoded = name.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? name
        return try await post("/contacts/\(encoded)/call-notes",
                              body: Body(notes: notes),
                              as: APIModels.CallNotesResult.self)
    }

    func posts() async throws -> APIModels.PostsResponse {
        try await get("/posts", as: APIModels.PostsResponse.self)
    }

    func createPost(_ body: String) async throws -> APIModels.PostDraft {
        struct Body: Codable { let body: String; let channel: String }
        return try await post("/posts", body: Body(body: body, channel: "linkedin"),
                              as: APIModels.PostDraft.self)
    }

    func deletePost(_ id: String) async throws {
        struct Reply: Codable { let deleted: Bool? }
        let _: Reply = try await delete("/posts/\(id)", as: Reply.self)
    }

    // MARK: - Workspaces & workflow jobs

    func workspaces() async throws -> [APIModels.WorkspaceInfo] {
        let list: APIModels.WorkspaceList = try await get("/workspaces", as: APIModels.WorkspaceList.self)
        return list.workspaces ?? []
    }

    func openWorkspace(path: String) async throws -> APIModels.WorkspaceInfo {
        struct Body: Codable { let path: String }
        return try await post("/workspaces", body: Body(path: path), as: APIModels.WorkspaceInfo.self)
    }

    /// `/schedules`-style responses are loose dicts; the inspect route spreads
    /// the inspection at the top level.
    func inspectWorkspace(_ id: String) async throws -> [String: AnyCodable] {
        try await get("/workspaces/\(id)/inspect", as: [String: AnyCodable].self)
    }

    func workflowTemplates() async throws -> [APIModels.WorkflowTemplate] {
        let reply: APIModels.WorkflowTemplateList =
            try await get("/workflows", as: APIModels.WorkflowTemplateList.self)
        return reply.workflows ?? []
    }

    /// Run a pre-built automation. The outcome's status and step count come from
    /// the engine, so a step that failed is visible as a failed run.
    func runWorkflow(_ templateId: String) async throws -> APIModels.WorkflowRunOutcome {
        struct Body: Codable { let session_id: String }
        let encoded = templateId.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed)
            ?? templateId
        return try await post("/workflows/\(encoded)/execute",
                              body: Body(session_id: "native-app"),
                              as: APIModels.WorkflowRunOutcome.self)
    }

    func workflowJobs(limit: Int = 20) async throws -> [APIModels.TaskInfo] {
        try await get("/tasks?limit=\(limit)", as: [APIModels.TaskInfo].self)
    }

    func workflowJob(_ id: String) async throws -> APIModels.TaskInfo {
        try await get("/tasks/\(id)", as: APIModels.TaskInfo.self)
    }

    func workflowAction(_ id: String, action: String) async throws -> [String: AnyCodable]? {
        struct Empty: Codable {}
        return try await post("/tasks/\(id)/\(action)", body: Empty(), as: [String: AnyCodable].self)
    }

    func attention() async throws -> [APIModels.AttentionItem] {
        let list: APIModels.AttentionList = try await get("/attention", as: APIModels.AttentionList.self)
        return list.items ?? []
    }

    /// Create a task. The body is the subset the route reads.
    func createPlannerTask(title: String, priority: String) async throws -> APIModels.PlannerTask {
        struct Body: Codable { let title: String; let priority: String }
        return try await post("/planner/tasks", body: Body(title: title, priority: priority),
                              as: APIModels.PlannerTask.self)
    }

    /// Create a task with the fields a captured item actually carries.
    ///
    /// The route accepts `detail` and `due_at` alongside the title; the older
    /// overload above only ever sent title/priority. A due date is only sent when
    /// the person confirmed one — the route parses ISO-8601 and refuses anything
    /// it cannot read, so a guessed format would be a 422 rather than a task.
    func createPlannerTask(title: String, priority: String, detail: String,
                           dueAt: Date?) async throws -> APIModels.PlannerTask {
        struct Body: Encodable {
            let title: String
            let priority: String
            let detail: String
            let due_at: String?
        }
        let due: String? = dueAt.map { date in
            let formatter = ISO8601DateFormatter()
            formatter.timeZone = .current
            formatter.formatOptions = [.withInternetDateTime]
            return formatter.string(from: date)
        }
        return try await post("/planner/tasks",
                              body: Body(title: title, priority: priority,
                                         detail: detail, due_at: due),
                              as: APIModels.PlannerTask.self)
    }

    // MARK: - Reminders (scheduled jobs)

    /// Create a reminder job.
    ///
    /// The backend has no separate reminder store: a reminder *is* a scheduled
    /// job with `action: "reminder"`, and that is what makes it fire while the
    /// app is closed. Writing a reminder into a local-only store would produce
    /// something that looks scheduled and never runs.
    ///
    /// The scheduler's grammar has **no absolute-date form**. Its one-off shape
    /// is a clock time ("at 8 PM", meaning the next occurrence of that time) or
    /// a relative offset ("in 2 hours"); an ISO date is refused by name. So a
    /// reminder a person asked for at a specific future *date* more than a day
    /// out cannot be expressed, and this refuses it here with a sentence that
    /// says why — rather than sending something the backend will reject, or
    /// quietly turning it into a daily job that fires tomorrow and every day
    /// after.
    func createReminder(message: String, at date: Date,
                        now: Date = Date()) async throws -> APIModels.ScheduledJob {
        struct Body: Codable {
            let action: String
            let schedule: String
            let params: [String: String]
        }
        let seconds = date.timeIntervalSince(now)
        guard seconds > 0 else {
            throw APIError.httpError(400, "That time is already past, so no reminder was set.")
        }
        let schedule: String
        if seconds <= 90 * 60 {
            // Closest to the exact instant the grammar can express.
            let minutes = max(1, Int((seconds / 60).rounded()))
            schedule = "in \(minutes) minutes"
        } else if Calendar.current.isDateInToday(date) {
            let formatter = DateFormatter()
            formatter.locale = Locale(identifier: "en_US_POSIX")
            formatter.timeZone = .current
            formatter.dateFormat = "h:mm a"
            schedule = "at " + formatter.string(from: date)
        } else {
            throw APIError.httpError(
                400,
                "The scheduler can only set a reminder for a clock time today or a relative "
                + "delay of up to about an hour; it has no way to express a specific future "
                + "date. Set a due date on a task instead, or schedule it for a time today.")
        }
        struct Reply: Codable { let schedule_id: String; let name: String? }
        let reply: Reply = try await post(
            "/schedules",
            body: Body(action: "reminder", schedule: schedule,
                       params: ["message": message]),
            as: Reply.self)
        return APIModels.ScheduledJob(
            schedule_id: reply.schedule_id,
            name: reply.name ?? message,
            schedule: schedule,
            action: "reminder", status: nil, next_run: nil, last_run: nil,
            run_count: nil, failure_count: nil, last_error: nil, last_result: nil)
    }

    /// Mark a task done. Returns the updated task.
    func completePlannerTask(_ ref: String) async throws -> APIModels.PlannerTask {
        struct Body: Codable { let evidence: String }
        return try await post("/planner/tasks/\(ref)/complete",
                              body: Body(evidence: ""), as: APIModels.PlannerTask.self)
    }

    func scheduledJobs() async throws -> [APIModels.ScheduledJob] {
        let list: APIModels.ScheduleList = try await get("/schedules", as: APIModels.ScheduleList.self)
        return list.schedules ?? []
    }

    /// Pause, resume, cancel, or run a scheduled job now.
    /// Edit a job in place (name and/or schedule). Same id, history preserved.
    func editSchedule(_ id: String, name: String?, schedule: String?) async throws {
        struct Body: Codable { let name: String?; let schedule: String? }
        struct Reply: Codable { let schedule_id: String? }
        let _: Reply = try await patch("/schedules/\(id)",
                                       body: Body(name: name, schedule: schedule), as: Reply.self)
    }

    /// A job's own runs (`schedule_runs`), newest first.
    func scheduleHistory(_ id: String) async throws -> [APIModels.ScheduleRun] {
        struct Reply: Codable { let history: [APIModels.ScheduleRun]? }
        let reply: Reply = try await get("/schedules/\(id)", as: Reply.self)
        return reply.history ?? []
    }

    func scheduleAction(_ id: String, action: String) async throws -> [String: AnyCodable]? {
        struct Empty: Codable {}
        return try await post("/schedules/\(id)/\(action)", body: Empty(),
                              as: [String: AnyCodable].self)
    }

    /// Approve or deny a pending confirmation.
    ///
    /// The approver's session is sent, because the backend records *who*
    /// answered -- a confirmation answered on the phone and one answered on
    /// the laptop are not the same fact, and the audit needs to tell them
    /// apart.
    func resolveConfirmation(_ confirmationId: String, approve: Bool,
                              sessionId: String) async throws -> APIModels.CommandResponse {
        var components = URLComponents()
        components.queryItems = [
            URLQueryItem(name: "approve", value: approve ? "true" : "false"),
            URLQueryItem(name: "session_id", value: sessionId),
        ]
        let path = "/confirm/\(confirmationId)?" + (components.percentEncodedQuery ?? "")
        return try await post(path, body: Optional<String>.none,
                             as: APIModels.CommandResponse.self)
    }

    // MARK: - Document analysis

    /// Send a composed prompt to the configured provider (`POST /ai/chat/send`).
    ///
    /// Deliberately no `session_id`/`conversation_id`: a document question is a
    /// one-shot analysis, not part of a chat thread, and supplying neither means
    /// the route will not run its action-proposal step — a document's contents
    /// must not be able to start a tool call.
    func sendAIChat(_ text: String, timeout: TimeInterval = 90) async throws -> APIModels.AIChatReply {
        struct Body: Codable { let text: String }
        return try await post("/ai/chat/send", body: Body(text: text), timeout: timeout,
                              as: APIModels.AIChatReply.self)
    }

    // MARK: - Multipart upload

    /// Upload a file as multipart/form-data.
    ///
    /// The attachment routes take the file itself plus the id of the message
    /// it belongs to, so this builds a form with a file part and a plain
    /// field. The boundary is generated per request -- a reused one would let
    /// a file's contents collide with the boundary and forge a part.
    func upload<T: Decodable>(_ path: String, fileURL: URL, fileName: String,
                              mimeType: String, fields: [String: String] = [:],
                              as type: T.Type = T.self) async throws -> T {
        let boundary = "----VantageBoundary\(UUID().uuidString)"
        var body = Data()

        for (key, value) in fields {
            body.append("--\(boundary)\r\n".data(using: .utf8)!)
            body.append("Content-Disposition: form-data; name=\"\(key)\"\r\n\r\n".data(using: .utf8)!)
            body.append("\(value)\r\n".data(using: .utf8)!)
        }

        // The file's bytes are read into memory rather than streamed. The
        // backend caps an upload at 10 MB, so this is bounded; reading the
        // whole thing is also what lets a failed read be reported before
        // anything is sent, rather than half way through.
        let payload: Data
        do {
            payload = try Data(contentsOf: fileURL)
        } catch {
            throw APIError.transport("Could not read \(fileName): \(error.localizedDescription)")
        }

        body.append("--\(boundary)\r\n".data(using: .utf8)!)
        body.append(
            "Content-Disposition: form-data; name=\"file\"; filename=\"\(fileName)\"\r\n".data(using: .utf8)!
        )
        body.append("Content-Type: \(mimeType)\r\n\r\n".data(using: .utf8)!)
        body.append(payload)
        body.append("\r\n--\(boundary)--\r\n".data(using: .utf8)!)

        guard let url = URL(string: baseURL + path) else {
            throw APIError.invalidURL(baseURL + path)
        }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Accept")
        req.setValue(ownOrigin, forHTTPHeaderField: "Origin")
        req.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        if let tok = token, !tok.isEmpty {
            req.setValue(tok, forHTTPHeaderField: "X-JARVIS-Token")
        }
        req.httpBody = body

        let (data, response) = try await session.data(for: req)
        try validate(response: response, data: data)
        return try JSONDecoder().decode(type, from: data)
    }

    // MARK: - Request building

    /// The `Origin` this app presents on its own loopback calls.
    ///
    /// Several routes (`/google/connect/begin`, `/google/disconnect`,
    /// `/google/tasks` writes) are `SameOriginProtected`: the backend refuses a
    /// request that carries neither `Origin` nor `Sec-Fetch-Site:
    /// same-origin`. A native URLSession request carries neither by default,
    /// so Connect, Disconnect, task create and task complete were **all**
    /// refused with `403 Same-origin request required.` — every write button
    /// on the Google screen could only fail. The backend's own comment is
    /// explicit that this layer is about web pages, and that a local process
    /// that already holds the token can set the header; this app *is* the
    /// laptop, addressing the backend's own origin, so it says so.
    private var ownOrigin: String { baseURL }

    private func makeRequest(path: String, method: String, body: Encodable? = nil) throws -> URLRequest {
        // Percent-encode the path so an id containing a space or non-ASCII
        // character cannot produce an invalid URL (URL(string:) would just
        // return nil -> invalidURL). Split any query string off first: its own
        // separators must survive, and its values are server-generated ids and
        // booleans. Today every id is a UUID/token, so encoding is a no-op;
        // this is the guard for the day one isn't.
        let pathPart: String
        let queryPart: String?
        if let qm = path.firstIndex(of: "?") {
            pathPart = String(path[..<qm])
            queryPart = String(path[path.index(after: qm)...])
        } else {
            pathPart = path
            queryPart = nil
        }
        let encodedPath = pathPart.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? pathPart
        let joinedPath = queryPart.map { encodedPath + "?" + $0 } ?? encodedPath
        guard let url = URL(string: baseURL + joinedPath) else {
            throw APIError.invalidURL(baseURL + joinedPath)
        }
        var req = URLRequest(url: url)
        req.httpMethod = method
        req.setValue("application/json", forHTTPHeaderField: "Accept")
        req.setValue(ownOrigin, forHTTPHeaderField: "Origin")

        if let tok = token, !tok.isEmpty {
            req.setValue(tok, forHTTPHeaderField: "X-JARVIS-Token")
        }

        if let body = body {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            let encoder = JSONEncoder()
            req.httpBody = try encoder.encode(AnyEncodableWrapper(body))
        }

        return req
    }

    // MARK: - Response handling

    private func perform<T: Decodable>(_ request: URLRequest, as type: T.Type) async throws -> T {
        let (data, response) = try await session.data(for: request)
        try validate(response: response, data: data)
        let decoder = JSONDecoder()
        do {
            return try decoder.decode(type, from: data)
        } catch let error as DecodingError {
            // A payload that does not match the model is a real failure and is
            // reported as one, with the field that failed. Letting the raw
            // DecodingError escape gives every screen only "the data couldn't
            // be read", which names neither the route nor the field.
            throw APIError.decodeError(Self.describe(error, path: request.url?.path ?? "?"))
        }
    }

    /// One line naming where a payload failed to decode, safe to show.
    private static func describe(_ error: DecodingError, path: String) -> String {
        func keyPath(_ keys: [CodingKey]) -> String {
            let joined = keys.map { $0.stringValue }.filter { !$0.isEmpty }.joined(separator: ".")
            return joined.isEmpty ? "the response" : joined
        }
        switch error {
        case .keyNotFound(let key, let context):
            return "\(path): \(keyPath(context.codingPath + [key])) is missing."
        case .typeMismatch(_, let context):
            return "\(path): \(keyPath(context.codingPath)) has an unexpected type."
        case .valueNotFound(_, let context):
            return "\(path): \(keyPath(context.codingPath)) was null."
        case .dataCorrupted(let context):
            let where_ = keyPath(context.codingPath)
            return where_ == "the response"
                ? "\(path): the response body was not valid JSON."
                : "\(path): not valid JSON at \(where_)."
        @unknown default:
            return "\(path): the response could not be read."
        }
    }

    private func validate(response: URLResponse, data: Data) throws {
        guard let http = response as? HTTPURLResponse else {
            throw APIError.transport("Not an HTTP response")
        }
        switch http.statusCode {
        case 200...299:
            return
        case 401:
            throw APIError.unauthorized
        case 429:
            throw APIError.rateLimited
        default:
            let body = String(data: data, encoding: .utf8) ?? ""
            // Try to extract detail from JSON error response
            if let json = try? JSONDecoder().decode([String: String].self, from: data),
               let detail = json["detail"] {
                throw APIError.httpError(http.statusCode, detail)
            }
            throw APIError.httpError(http.statusCode, body)
        }
    }
}

// MARK: - Error type

enum APIError: LocalizedError, Equatable {
    case invalidURL(String)
    case unauthorized
    case rateLimited
    case httpError(Int, String)
    case transport(String)
    case decodeError(String)

    var errorDescription: String? {
        switch self {
        case .invalidURL(let url):
            return "Invalid URL: \(url)"
        case .unauthorized:
            return "Not authorized. Check that the API token exists at ~/.jarvis-forge/api_token."
        case .rateLimited:
            return "Too many requests. Slow down."
        case .httpError(let code, let detail):
            return "Server returned \(code): \(detail)"
        case .transport(let msg):
            return "Cannot reach backend: \(msg)"
        case .decodeError(let msg):
            return "Unexpected response format: \(msg)"
        }
    }

    static func == (lhs: APIError, rhs: APIError) -> Bool {
        lhs.localizedDescription == rhs.localizedDescription
    }
}

// MARK: - Encodable wrapper

/// Wraps any Encodable so it can be passed to JSONEncoder.
private struct AnyEncodableWrapper: Encodable {
    let encode: (Encoder) throws -> Void
    init(_ wrapped: Encodable) { self.encode = { try wrapped.encode(to: $0) } }
    func encode(to encoder: Encoder) throws { try encode(encoder) }
}