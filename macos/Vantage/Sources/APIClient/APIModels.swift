import Foundation

/// Codable models mirroring the Vantage FastAPI response schemas.
enum APIModels {

    // MARK: - Health

    struct HealthResponse: Codable {
        let status: String
        let tools: Int?
        let agents: [String]?
        let llm_enabled: Bool?
        let llm_source: String?
        let ai_provider: String?
        let provider_paths: [String: String]?
        let pending_confirmations: Int?
        let phone_urls: [String]?
        /// The security and privacy sections are **mixed-type** maps: the
        /// server sends booleans (`auth_enabled`, `redaction_enabled`),
        /// strings (`token_path`), numbers (`rate_limit_per_minute`) and lists
        /// (`allowed_origins`, `delete_scopes`) side by side.
        ///
        /// These were declared `[String: String]?`, and decoding a real
        /// `/health` response therefore **failed outright** — the first
        /// non-string value (`host_header_enforced`, a bool) aborted it. The
        /// whole health object was unavailable, so the app had no connection
        /// state, no device counts and no provider information, and the
        /// failure was silent because the caller swallows the error.
        let security: [String: AnyCodable]?
        let privacy: [String: AnyCodable]?
        let devices: DeviceHealth?
        let peripherals: [String: AnyCodable]?
        /// Each integration reports the adapter it is using — "local_maildir",
        /// "local_calendar" — so this is a map of strings, not of booleans.
        /// It was declared `[String: Bool]?`, and `integrations.calendar`
        /// is a string, so decoding a real `/health` response failed here
        /// too.
        let integrations: [String: String]?
        let scheduler: [String: AnyCodable]?
        let memory_retrieval: [String: AnyCodable]?
        let embeddings: [String: AnyCodable]?
        let recent_failures: Int?
    }

    struct DeviceHealth: Codable {
        let pairing: [String: AnyCodable]?
        let transport: [String: AnyCodable]?
        let paired_devices: Int?
    }

    // MARK: - Backend identity

    /// Who is answering on the backend port (`GET /health/identity`).
    ///
    /// The route is deliberately unauthenticated: the one question a client
    /// must be able to ask before it holds a valid token is "is the process on
    /// this port actually Vantage?". Without that answer, *any* HTTP response
    /// on the port looked like a healthy backend — and a stale process that
    /// had been running for a day served old routing while the app showed
    /// "Connected".
    ///
    /// Only `service` and `instance_id` are required inside `identity`; every
    /// other fact may be absent in a given build and must not fail the whole
    /// decode. A missing `identity` or `code` object is likewise not an error:
    /// a build that predates this route answers 404, which the client reads as
    /// "older build" rather than as an outage.
    struct BackendIdentityResponse: Codable, Equatable {
        let identity: BackendIdentity?
        let code: BackendCodeFreshness?
        let fingerprint: String?
    }

    struct BackendIdentity: Codable, Equatable {
        let service: String
        let instance_id: String
        let pid: Int?
        let started_at: Double?
        let app_version: String?
        let revision: String?
        let branch: String?
        let source_root: String?
        let python: String?
    }

    /// The backend's own verdict on whether its running code is older than the
    /// files on disk (`code` in the identity response). `stale == true` means
    /// the process predates the code on disk, so a fix on disk is not loaded.
    struct BackendCodeFreshness: Codable, Equatable {
        let stale: Bool?
        let started_at: Double?
        let newest_mtime: Double?
        let note: String?
    }

    // MARK: - AI Providers

    struct ProviderInfo: Codable, Identifiable {
        let provider_id: String
        let display_name: String
        let type: String?
        let enabled: Bool?
        let configured: Bool
        let verified: Bool
        let test_status: String?
        let last_verified_at: String?
        let last_error: String?
        let base_url: String?
        let model: String?
        let models: [String]?
        let capabilities: [String: AnyCodable]?

        var id: String { provider_id }
    }

    struct TestProviderResponse: Codable {
        let success: Bool
        let message: String?
        let timestamp: String?
        let notes: [String: AnyCodable]?
    }

    struct ProviderHealth: Codable {
        let provider_id: String
        let model: String?
        let configured: Bool
        let gateway_reachable: Bool?
        let route_health: String?
        let route_health_label: String?
        let last_success_at: Double?
        let last_failure_at: Double?
        let last_latency_ms: Int?
        let consecutive_failures: Int?
        let last_error_category: String?
        let last_http_status: Int?
        let last_check_source: String?
        let upstream_model: String?
        let upstream_route_id: String?
        let upstream_identity_known: Bool?
        let message: String?
    }

    struct StoreCredentialsResponse: Codable {
        let message: String?
        let provider_id: String?
        let status: String?
    }

    struct DeleteCredentialsResponse: Codable {
        let message: String?
        let provider_id: String?
    }

    struct ProviderSelection: Codable {
        // The real GET /ai/selection response (see jarvis/api/app.py
        // get_ai_selection): ai_provider + llm_model + llm_base_url, plus
        // enabled/credential flags. The old provider_id/provider_name fields
        // never existed on this response, which is why the view showed "Unknown".
        let ai_provider: String?
        let llm_model: String?
        let llm_base_url: String?
        let effective_model: String?
        let ai_enabled: Bool?
        let llm_enabled: Bool?
        let credential_configured: Bool?
        let known_providers: [String]?
    }

    struct ProviderCapability: Codable {
        let provider_id: String?
        let paths: [String: String]?
    }

    // MARK: - Command

    struct CommandRequest: Codable {
        let text: String
        let session_id: String?
        let device_id: String
        let source: String
        let context: [String: String]

        /// The conversation this turn belongs to.
        ///
        /// This was hardcoded to `nil`, which made every message a brand new
        /// conversation: the backend mints a `SESS-` id per request when none is
        /// given, so the second message could not see the first, and a follow-up
        /// like "explain the second part" had nothing to refer to. The session id
        /// is what the backend uses to scope a conversation, to thread history
        /// to the model, and to keep two conversations apart.
        init(text: String, sessionId: String? = nil) {
            self.text = text
            self.session_id = sessionId
            self.device_id = "laptop_main"
            self.source = "native"
            self.context = [:]
        }
    }

    /// What `/command` answers with.
    ///
    /// The field names are the server's, checked against a live response rather
    /// than guessed. The previous struct read `status`, `message`,
    /// `confirmation_required` and `elapsed_ms` — **none of which the server
    /// sends**. The server sends `reply`, `decision`, `needs_confirmation`,
    /// `duration_ms`, `model_used`, `intent` and `session_id`. Because the
    /// missing ones are all optional, the struct decoded fine and the app
    /// simply showed less than the backend had said: no model attribution, no
    /// timing, and a confirmation that was announced only by its own absence.
    struct CommandResponse: Codable {
        let status: String?
        let message: String?
        let reply: String?
        let actions: [AnyCodable]?
        let confirmation_required: Bool?
        let confirmation_id: String?
        let part: String?
        let tool_calls: [AnyCodable]?
        let elapsed_ms: Double?
        let agent_id: String?

        // --- The fields the server actually sends -------------------------
        //
        // `reply` is the answer. `needs_confirmation` says an action is being
        // held for a person; `decision` says what the permission engine
        // concluded; `model_used` names who answered, so the app can show
        // "answered by 9router" rather than implying the rules spoke;
        // `duration_ms` is the server's own timing; and `session_id` is the
        // conversation the turn belongs to, which the app adopts so the next
        // turn joins the same conversation.
        let decision: String?
        let needs_confirmation: Bool?
        let model_used: String?
        let intent: String?
        let duration_ms: Int?
        let session_id: String?
        let blocked_reason: String?
    }

    // MARK: - Confirmations

    /// A pending confirmation, as the backend's `/confirmations` returns it.
    ///
    /// The field names are the server's, checked against a live response
    /// rather than guessed: `confirmation_id`, `tool`, `preview`, `created_at`
    /// and so on. The previous version of this struct had `id`, `action`,
    /// `description` and `expires_at` — **none of which the server sends**,
    /// so decoding a real confirmation would have failed outright.
    struct Confirmation: Codable, Identifiable {
        let confirmation_id: String
        let request_id: String?
        let session_id: String?
        let tool: String?
        /// The parameters, as a JSON object.
        let params: [String: AnyCodable]?
        let device: String?
        let risk: Int?
        /// A one-line description of what will happen, for the person to read
        /// before deciding.
        let preview: String?
        let state: String?
        let created_at: String?
        let resolved_at: String?
        let requested_by_session: String?
        let requested_surface: String?
        let approved_by_session: String?
        let approved_surface: String?

        var id: String { confirmation_id }

        /// What to show the person: the tool, and what it will do.
        ///
        /// The preview is the server's own one-line description, so the app
        /// does not have to reconstruct "what will this do" from the
        /// parameters — and cannot get it subtly wrong.
        var displayTitle: String {
            guard let tool = tool, !tool.isEmpty else { return "Pending action" }
            return tool.replacingOccurrences(of: "_", with: " ")
        }

        var displayDetail: String {
            preview ?? ""
        }
    }

    struct ConfirmationResponse: Codable {
        let status: String?
        let message: String?
    }

    // MARK: - Devices

    /// A device, as the backend's ``/devices`` returns it.
    ///
    /// The field names are the server's, checked against a live response
    /// rather than guessed. The previous struct read ``device_name``,
    /// ``device_type`` and ``status`` -- **none of which the server sends**.
    /// The server sends ``type``, ``owner``, ``platform``, ``trust_level``,
    /// ``online`` and ``auth_state``, so every device rendered as "unknown"
    /// and the type-specific icon in ``DevicesView`` never matched.
    struct Device: Codable, Identifiable {
        let device_id: String
        /// The kind of device: "computer", "phone", "tablet".
        let type: String?
        let owner: String?
        let platform: String?
        let trust_level: String?
        let online: Bool?
        let auth_state: String?
        let capabilities: [String]?
        let paired_at: String?
        let last_seen: String?
        let is_host: Bool?

        var id: String { device_id }

        /// What to show as the device's name.
        ///
        /// The server has no display name for a device -- it has an owner and
        /// a type. Inventing a name would be a fabrication; the device id is
        /// the truth the server actually holds.
        var displayName: String {
            device_id
        }

        /// The device's type, as the server names it.
        var deviceType: String {
            type ?? "unknown"
        }
    }

    struct PairingRequest: Codable, Identifiable {
        let pairing_id: String
        let device_name: String?
        let device_type: String?
        let requested_at: String?

        var id: String { pairing_id }
    }

    // MARK: - Tasks

    /// A job, as the backend's ``/tasks`` returns it.
    ///
    /// The server sends ``goal`` -- the sentence the job is for -- and no
    /// ``name``. The previous struct read ``name`` only, so every job showed
    /// its job id where the goal should have been.
    struct TaskInfo: Codable, Identifiable {
        let job_id: String
        let goal: String?
        let status: String?
        let created_at: String?
        let updated_at: String?
        let steps: [AnyCodable]?

        var id: String { job_id }

        /// What the job is for, falling back to the id when the server sent
        /// no goal.
        var displayName: String {
            if let goal, !goal.isEmpty { return goal }
            return job_id
        }
    }


    // MARK: - Audit

    /// An audit row, as the backend's ``/audit`` returns it.
    ///
    /// The previous struct declared ``detail`` as a ``String?`` while the
    /// server sends a **nested object** (``{"ok": true, "verification":
    /// "success"}``). That one type mismatch aborted the decode of the whole
    /// array, so the Activity screen's audit list was **empty** -- the screen
    /// looked broken and reported nothing.
    ///
    /// The other names were wrong too: the row carries ``log_id`` (not
    /// ``id``), ``intent`` and ``tool`` (not ``action``), ``device`` (not
    /// ``device_id``) and ``created_at`` (not ``timestamp``). Every one of
    /// those read as nil, so each row showed "Unknown".
    struct AuditEntry: Codable, Identifiable {
        let log_id: String?
        let request_id: String?
        let session_id: String?
        let intent: String?
        let tool: String?
        let target: String?
        let device: String?
        let user_id: String?
        let risk: String?
        let confirmation: Int?
        let result: String?
        /// The detail is an object, not a string.
        let detail: [String: AnyCodable]?
        let duration_ms: Int?
        let created_at: String?

        var id: String { log_id ?? UUID().uuidString }

        /// What the row is about, in the words the server used.
        ///
        /// A row records an *action*, so the tool is the most precise label;
        /// the intent is the sentence the person actually asked for.
        var displayTitle: String {
            if let tool, !tool.isEmpty { return tool }
            if let intent, !intent.isEmpty { return intent }
            return "Unknown"
        }

        /// The intent, when the row has one, for the second line.
        var displayIntent: String {
            guard let intent, !intent.isEmpty else { return "" }
            return intent
        }

        /// The detail, as one readable line.
        ///
        /// The detail is a nested object whose shape varies with the tool, so
        /// the values are joined in the order the server sent them rather
        /// than being reshaped into a structure the server does not have.
        var displayDetail: String {
            guard let detail, !detail.isEmpty else { return "" }
            return detail
                .map { "\($0.key): \($0.value.displayText)" }
                .sorted()
                .joined(separator: "  ")
        }
    }

    // MARK: - Memory

    struct MemoryItem: Codable, Identifiable {
        let id: String?
        let content: String?
        let scope: String?
        let project_id: String?
        let created_at: String?
        let updated_at: String?

        var identifier: String { id ?? UUID().uuidString }
    }

    struct MemoryResponse: Codable {
        let stats: [String: AnyCodable]?
        let items: [MemoryItem]?
    }

    struct CreateMemoryRequest: Codable {
        let content: String
        let scope: String
        let project: String?

        init(content: String, scope: String = "long_term", project: String? = nil) {
            self.content = content
            self.scope = scope
            self.project = project
        }
    }

    // MARK: - Projects

    /// A project, as the backend's ``/projects`` returns it.
    ///
    /// The server sends ``project_id`` and no ``id``, so the previous
    /// ``id``-based identity was always a fresh UUID: every row had a
    /// different identity on every load, and SwiftUI's list diffing treated
    /// the whole list as changed on every refresh.
    struct ProjectInfo: Codable, Identifiable {
        let project_id: String?
        let name: String?
        let description: String?
        let status: String?
        let created_at: String?
        let updated_at: String?

        var id: String { project_id ?? UUID().uuidString }
    }

    // MARK: - Metrics

    struct MetricsResponse: Codable {
        let uptime_seconds: Double?
        let total_commands: Int?
        let total_confirmations: Int?
        let active_sessions: Int?
        let memory_items: Int?
        let failures_24h: Int?
    }

    // MARK: - Auth

    struct AuthSessionResponse: Codable {
        let auth_enabled: Bool?
        let token: String?
        let expires_in: Int?
    }

    /// A live session, as the backend's ``/auth/sessions`` returns it.
    ///
    /// The server sends ``session_ref`` and no ``ref``, and no
    /// ``created_at`` -- it sends ``expires_in`` instead. The previous
    /// struct read ``ref``, so every session was identified by a fresh UUID
    /// and the Settings screen had no way to say which session it was.
    struct SessionInfo: Codable, Identifiable {
        let session_ref: String?
        let surface: String?
        let expires_in: Int?

        var id: String { session_ref ?? UUID().uuidString }

        /// The session's own reference, as the server names it.
        var displayRef: String {
            session_ref ?? "unknown"
        }
    }

    // MARK: - Attachments

    /// The conversation an attachment hangs off.
    struct ChatCreated: Codable {
        let id: String
        let title: String?
    }

    /// The reply to an upload.
    struct AttachmentUpload: Codable {
        let success: Bool?
        let attachment_id: String?
        let filename: String?
        let file_type: String?
        let size_bytes: Int?
        let status: String?
    }

    /// A message, saved so an attachment has something to belong to.
    struct MessageSaved: Codable {
        let id: String
        let conversation_id: String?
        let role: String?
    }

    /// What the pipeline made of one attachment.
    ///
    /// The fields are the ones the backend actually produces, and the text is
    /// optional because it is only present when the recogniser read something.
    /// A missing `text` and an empty `text` mean different things -- "not
    /// processed" versus "processed, and there was nothing written in it" --
    /// so the note is carried alongside rather than inferred.
    struct AttachmentResult: Codable {
        let status: String?
        let type: String?
        let summary: String?
        let text: String?
        let text_note: String?
        let text_lines: Int?
        let reason: String?
        let error: String?

        /// What to show the person, in one honest line (or more).
        ///
        /// The order matters: the extracted text is the point of the
        /// attachment, so it comes first; the reason comes last, and only
        /// when there is nothing else to say.
        var displayText: String {
            if let status = status, status == "error" {
                return "Could not process: " + (reason ?? error ?? "unknown reason")
            }
            if let text = text, !text.isEmpty {
                return text
            }
            if let note = text_note, !note.isEmpty {
                return note
            }
            if let summary = summary, !summary.isEmpty {
                return summary
            }
            if let status = status, status == "unsupported" {
                return "No processing is defined for this kind of file."
            }
            if let status = status, !status.isEmpty {
                return "Status: " + status
            }
            return "Not processed yet."
        }
    }

    /// The result of processing one attachment.
    struct AttachmentProcessing: Codable {
        let processed_count: Int?
        let results: [ProcessedAttachment]?
    }

    struct ProcessedAttachment: Codable {
        let attachment_id: String?
        let status: String?
        let type: String?
        let summary: String?
        let text: String?
        let text_note: String?
        let text_lines: Int?
        let reason: String?
        let error: String?
    }

    /// One attachment as the backend lists it.
    struct AttachmentSummary: Codable, Identifiable {
        let id: String
        let file_type: String?
        let filename: String?
        let file_size: Int?
        let upload_status: String?
        let processing_status: String?
        let result: AttachmentResult?
        let created_at: String?

        var identity: String { id }
    }

    struct AttachmentsForMessage: Codable {
        let message_id: String?
        let attachments: [AttachmentSummary]?
        let total_count: Int?
    }

    // MARK: - Planner (Epic 1)

    /// One of the person's own to-do items (`/planner/tasks`).
    struct PlannerTask: Codable, Identifiable {
        let task_ref: String
        let title: String
        let detail: String?
        let status: String?
        let priority: String?
        let due_at: String?
        let start_at: String?
        let estimate_minutes: Int?
        let depends_on: [String]?
        let project: String?
        let done_at: String?
        let evidence: String?
        let overdue: Bool?
        let blocked: Bool?

        var id: String { task_ref }
    }

    /// The day's agenda (`/planner/today`). The calendar's own read result is
    /// carried as `calendar_read_failed` so a failed read is never shown as
    /// "nothing scheduled".
    struct PlannerAgenda: Codable {
        let day: String?
        let calendar_read_failed: Bool?
        let events: [AnyCodable]?
        let conflicts: [AnyCodable]?
        let tasks: [PlannerTask]?
        let overdue: [PlannerTask]?
        let blocked: [PlannerTask]?
        let next_action: AnyCodable?
        /// The backend computes this against its own clock and its own open-task
        /// set, so it is more accurate than re-deriving "due today" from
        /// `tasks`. Optional because an older backend may not send it.
        let due_today: [PlannerTask]?
        let has_unscheduled: Bool?
        let generated_at: String?
    }

    /// A scheduled automation (`/schedules`).
    struct ScheduledJob: Codable, Identifiable {
        let schedule_id: String
        let name: String
        let schedule: String?
        let action: String?
        let status: String?
        let next_run: String?
        let last_run: String?
        let run_count: Int?
        let failure_count: Int?
        let last_error: String?
        let last_result: String?

        var id: String { schedule_id }
    }

    struct ScheduleList: Codable {
        let schedules: [ScheduledJob]?
    }

    /// One recorded run of a scheduled job.
    struct ScheduleRun: Codable, Identifiable {
        let run_id: String?
        let started_at: String?
        let finished_at: String?
        let outcome: String?
        let detail: String?
        var id: String { (run_id ?? "") + (started_at ?? "") }
    }

    /// One thing that needs a person (`/attention`).
    struct AttentionItem: Codable, Identifiable {
        let kind: String?
        let surface: String?
        let title: String
        let detail: String?
        let ref: String?
        var id: String { (kind ?? "") + "|" + (ref ?? title) }
    }

    struct AttentionList: Codable {
        let count: Int?
        let items: [AttentionItem]?
    }

    /// An open workspace (`/workspaces`).
    struct WorkspaceInfo: Codable, Identifiable {
        let workspace_id: String
        let name: String?
        let root: String?
        var id: String { workspace_id }
    }

    struct WorkflowTemplate: Codable, Identifiable {
        let workflow_id: String
        let name: String?
        let description: String?
        var id: String { workflow_id }
    }
    struct WorkflowTemplateList: Codable {
        let workflows: [WorkflowTemplate]?
    }
    struct WorkflowRunOutcome: Codable {
        let status: String?
        let run_id: String?
        let workflow_id: String?
        let current_status: String?
        let step: Int?
        let total_steps: Int?
        let result: String?
        let error: String?
    }

    struct WorkspaceList: Codable {
        let workspaces: [WorkspaceInfo]?
    }

    // MARK: - Contacts & posts

    struct BeginWorkResult: Codable {
        let headline: String?
        let opened_project: OpenedProject?
        let agent: AgentOffer?
        let workflow: [String]?
        let note: String?
    }
    struct OpenedProject: Codable {
        let attempted: Bool?
        let ok: Bool?
        let message: String?
    }
    struct AgentOffer: Codable {
        let available: Bool?
        let agent: String?
        let prompt: String?
        let note: String?
    }

    struct AgentSummary: Codable, Identifiable {
        let name: String
        let tool_count: Int
        let usable: Bool
        var id: String { name }
    }
    struct AgentList: Codable { let agents: [AgentSummary]? }

    struct CodingAvailability: Codable {
        let available: Bool
        let agent: String?
        let detail: String?
        let requires_approval: Bool?
    }
    struct CodingTaskResult: Codable {
        let ok: Bool
        let job_id: String?
        let message: String?
        let verified: Bool?
        let turns: Int?
        let cost_usd: Double?
        let workspace: String?
        let changed_files: [String]?
    }

    struct AppStatus: Codable, Identifiable {
        let key: String
        let name: String
        let category: String?
        let installed: Bool
        let running: Bool
        var id: String { key }
    }
    struct AppStatusList: Codable { let apps: [AppStatus]? }

    struct Contact: Codable, Identifiable {
        let contact_id: String
        let name: String
        let address: String
        var id: String { contact_id }
    }

    struct OutreachDraft: Codable {
        let contact: ContactRef?
        let draft: OutreachBody?
        let context: OutreachContext?
        let saved: Bool?
        let email_error: String?
    }
    struct OutreachBody: Codable {
        let to: String?
        let subject: String?
        let body: String?
    }
    struct OutreachContext: Codable {
        let open_tasks: [String]?
        let recent_email: [String]?
    }
    struct CallNotesResult: Codable {
        let ok: Bool?
        let contact: String?
        let created_count: Int?
        let note: String?
    }

    struct CallBrief: Codable {
        let contact: ContactRef?
        let open_tasks: [BriefTask]?
        let recent_email: [BriefEmail]?
        let talking_points: [String]?
        let email_error: String?
        let place_call: PlaceCall?
    }
    struct MeetingBrief: Codable {
        let ok: Bool
        let reason: String?
        let meeting: APIModels.MeetingRef?
        let related_email: [BriefEmail]?
        let related_tasks: [BriefTask]?
        let talking_points: [String]?
        let conflict: APIModels.MeetingRef?
        let workflow: [String]?
        let email_error: String?
        let note: String?
    }
    struct MeetingRef: Codable {
        let uid: String?
        let title: String?
        let start: String?
        let end: String?
        let location: String?
        let description: String?
    }

    struct ContactRef: Codable { let name: String?; let address: String? }
    struct BriefTask: Codable { let task_ref: String?; let title: String?; let due_at: String? }
    struct InboxMessage: Codable, Identifiable {
        let uid: String
        let subject: String?
        let from: String?
        let date: String?
        let snippet: String?
        var id: String { uid }
    }
    struct InboxResponse: Codable { let results: [InboxMessage]? }
    struct EmailBody: Codable {
        let uid: String?
        let subject: String?
        let from: String?
        let to: [String]?
        let body: String?
    }
    struct EmailReadResponse: Codable { let message: EmailBody? }

    struct BriefEmail: Codable {
        let subject: String?
        let from: String?
        let date: String?
        let snippet: String?
    }
    struct PlaceCall: Codable { let supported: Bool?; let reason: String? }

    struct RecapResult: Codable {
        let created: PostDraft?
        let completed_count: Int?
        let projects: [String]?
        let reason: String?
        let publishing: PublishingBoundary?
    }

    struct PostDraft: Codable, Identifiable {
        let draft_id: String
        let channel: String?
        let body: String
        let status: String?
        let scheduled_for: String?
        var id: String { draft_id }
    }

    struct PostsResponse: Codable {
        let publishing: PublishingBoundary?
        let drafts: [PostDraft]?
        let due: [PostDraft]?
    }
    struct PublishingBoundary: Codable { let supported: Bool?; let reason: String? }

}

/// A type-erased Codable value for flexible JSON fields.
struct AnyCodable: Codable {
    let value: Any

    init(_ value: Any) { self.value = value }

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if let intVal = try? container.decode(Int.self) {
            value = intVal
        } else if let doubleVal = try? container.decode(Double.self) {
            value = doubleVal
        } else if let boolVal = try? container.decode(Bool.self) {
            value = boolVal
        } else if let stringVal = try? container.decode(String.self) {
            value = stringVal
        } else if let arrayVal = try? container.decode([AnyCodable].self) {
            value = arrayVal.map { $0.value }
        } else if let dictVal = try? container.decode([String: AnyCodable].self) {
            value = dictVal.mapValues { $0.value }
        } else {
            value = NSNull()
        }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        if let intVal = value as? Int {
            try container.encode(intVal)
        } else if let doubleVal = value as? Double {
            try container.encode(doubleVal)
        } else if let boolVal = value as? Bool {
            try container.encode(boolVal)
        } else if let stringVal = value as? String {
            try container.encode(stringVal)
        } else if let arrayVal = value as? [Any] {
            try container.encode(arrayVal.map { AnyCodable($0) })
        } else if let dictVal = value as? [String: Any] {
            try container.encode(dictVal.mapValues { AnyCodable($0) })
        } else {
            var c = container
            try c.encodeNil()
        }
    }

    /// The value as one readable line.
    ///
    /// Used where a nested, shape-varying object has to be shown to a person:
    /// the audit row's `detail` is an object whose keys depend on the tool
    /// that ran, so it is rendered as its own values rather than as JSON.
    var displayText: String {
        switch value {
        case let text as String:
            return text
        case let flag as Bool:
            return flag ? "yes" : "no"
        case let number as Int:
            return String(number)
        case let number as Double:
            return String(number)
        case let list as [Any]:
            return list.map { AnyCodable($0).displayText }.joined(separator: ", ")
        case let map as [String: Any]:
            return map
                .map { "\($0.key): \(AnyCodable($0.value).displayText)" }
                .sorted()
                .joined(separator: ", ")
        default:
            return String(describing: value)
        }
    }
}
// Convenience readers for the loosely-typed `AnyCodable` payloads the API
// returns (a planner agenda's events/conflicts, a next-action object). They
// return nil when the underlying value is a different shape, so a screen reads
// what it can and shows nothing rather than crashing on a type it did not expect.
extension AnyCodable {
    var stringValue: String? { value as? String }
    var intValue: Int? { value as? Int }
    var boolValue: Bool? { value as? Bool }
    /// The object's fields. `AnyCodable.init` stores a nested object as
    /// `[String: Any]` (it maps out the inner wrappers), so casting to
    /// `[String: AnyCodable]` would always fail — which silently blanked the
    /// planner's "next up" line. Re-wrap each stored value.
    var dictionary: [String: AnyCodable]? {
        guard let raw = value as? [String: Any] else { return nil }
        return raw.mapValues { AnyCodable($0) }
    }
    var arrayValue: [Any]? { value as? [Any] }
}

// MARK: - Google integration
//
// The shapes the backend's /google/* routes return. Status strings are kept as
// `String` rather than an enum: an unknown value from a newer backend must
// decode rather than throw, and the views branch on the specific values they
// know.
extension APIModels {

    struct GoogleConnection: Codable {
        let state: String            // not_configured | disconnected | connected | expired | error
        let detail: String?
        let account: String?
        let scopes: [String]?
        let configured: Bool?
        let has_token: Bool?
        let usable: Bool?

        var isConnected: Bool { usable == true }
    }

    struct GoogleSetup: Codable {
        let configured: Bool?
        let calendar_enabled: Bool?
        let tasks_enabled: Bool?
        let scopes: [String]?
        let setup: [String]?
        let redirect_uri: String?
    }

    struct GoogleStatus: Codable {
        let connection: GoogleConnection
        let setup: GoogleSetup?
    }

    // MARK: - MCP

    /// One remote MCP server. `state` uses the same vocabulary as Google's
    /// connection so a reader does not have to learn a second one.
    struct MCPConnection: Codable {
        let state: String
        let detail: String?
        let name: String?
        let url: String?
        let configured: Bool?
        let has_token: Bool?
        let allowed_tools: [String]?
        /// Names the server advertises. Listed is not callable. The server
        /// truncates this array (50 names) while `tool_count` is the real
        /// total, so the count shown to a person must come from `tool_count`.
        let tools: [MCPTool]?
        let tool_count: Int?
        /// The advertised tools that are also on the allowlist.
        let callable: [String]?
        /// `serverInfo` from the handshake, present only when connected.
        let server: MCPServerInfo?
    }

    struct MCPServerInfo: Codable {
        let name: String?
        let version: String?
    }

    struct MCPTool: Codable, Identifiable {
        let name: String
        let description: String?
        var id: String { name }
    }

    /// The setup block `/mcp/status` sends.
    ///
    /// This is the same object `describe_mcp` returns: a **dict** carrying the
    /// connector's public config (`name`, `url`, `configured`, `has_token`,
    /// `allowed_tools`) plus a `setup` array of steps. The model previously
    /// declared the whole block as `[String]?`, so decoding a real response
    /// failed with a `typeMismatch` and the MCP screen could never show the
    /// connection at all -- it rendered only "Unexpected response format" and
    /// "I could not read the connection state." forever.
    struct MCPSetup: Codable {
        let name: String?
        let url: String?
        let configured: Bool?
        let has_token: Bool?
        let allowed_tools: [String]?
        let setup: [String]?
    }

    struct MCPStatus: Codable {
        let connection: MCPConnection
        let setup: MCPSetup?
        /// The Vantage-side tool names an allowlisted remote tool registers
        /// under. Kept as sent; the view reads presence from `connection`.
        let callable_tools: [String]?
    }

    struct GoogleConnectBegin: Codable {
        let url: String
        let state: String?
        let redirect_uri: String?
    }

    struct GoogleDisconnect: Codable {
        let removed: Bool?
        let revoked: Bool?
        let detail: String?
    }

    /// The reply to `POST /ai/chat/send`.
    ///
    /// `ok` is the provider's own verdict. When it is false the route still
    /// answers 200 with the reason in `error`; treating a missing `response` as an
    /// empty answer would turn a refused request into a blank one, so `ok` and
    /// `error` are read rather than inferred from the body's emptiness.
    struct AIChatReply: Codable {
        let ok: Bool
        let response: String?
        let provider: String?
        let model: String?
        let error: String?
    }

    struct GoogleEvent: Codable, Identifiable {
        let uid: String
        let title: String
        let start: String?
        let end: String?
        let location: String?
        let description: String?
        let calendar: String?
        let all_day: Bool?
        let duration_minutes: Int?

        var id: String { uid }
    }

    struct GoogleEventsResponse: Codable {
        let provider: String?
        /// The provider's own verdict. `has_results` and `real_empty` both mean
        /// the read succeeded; everything else is a failure that must be shown
        /// as one rather than as an empty calendar.
        let status: String?
        let read_succeeded: Bool?
        let detail: String?
        let count: Int?
        let events: [GoogleEvent]?
    }

    struct GoogleTask: Codable, Identifiable {
        let id: String
        let title: String
        let notes: String?
        let status: String?
        let done: Bool?
        let due: String?
        let completed: String?
        let list_id: String?
        let parent: String?
        let source: String?
    }

    struct GoogleTasksResponse: Codable {
        let provider: String?
        let status: String?
        let read_succeeded: Bool?
        let detail: String?
        let count: Int?
        let tasks: [GoogleTask]?
    }

    struct GoogleTaskList: Codable, Identifiable {
        let id: String
        let title: String
        let updated: String?
    }

    struct GoogleTaskListsResponse: Codable {
        let ok: Bool?
        let lists: [GoogleTaskList]?
        let count: Int?
        let error: String?
        let status: String?
    }

    struct GoogleTaskCreateResult: Codable {
        let ok: Bool?
        let provider: String?
        let task: GoogleTask?
        let error: String?
        let status: String?
    }

    struct GoogleTaskCreateRequest: Codable {
        let title: String
        let notes: String?
        let due: String?
        let list_id: String?
        let source: String?
    }

    /// The reply to `POST /calendar/events`.
    ///
    /// `ok` is the tool's own verdict and `verified` is separate: the route
    /// writes, then reads the calendar back. A connection that is read-only
    /// answers `ok: false` with the tool's sentence in `message`, which is the
    /// case that matters here — the Google connection holds
    /// `calendar.readonly`, so an event cannot be written to it yet.
    struct CalendarEventCreateResult: Codable {
        let ok: Bool?
        let event: GoogleEvent?
        let message: String?
        let verified: Bool?
    }

    struct N8NExecutionRow: Codable {
        let at: String?
        let workflow: String?
        let correlation_id: String?
        let status: String?
        let message: String?
        let http_status: Int?
        let duration_ms: Int?
    }

    struct N8NWorkflowStatus: Codable {
        let workflow: String?
        let found: Bool?
        let latest: N8NExecutionRow?
        let count: Int?
    }

    // MARK: - Provider categories (the seven tabs)

    struct CategoryProvider: Codable, Identifiable {
        let id: String
        let label: String
        let default_base_url: String?
    }

    struct CategorySettings: Codable {
        let category: String
        let provider: String?
        let provider_label: String?
        let model: String?
        let base_url: String?
        let enabled: Bool?
        let timeout: Double?
        let capability: String?
        let options: [String: String]?
        let has_key: Bool?
        let configured: Bool?
        let tested_at: Double?
        let test_ok: Bool?
        let test_note: String?
    }

    struct CategoryTab: Codable, Identifiable {
        let category: String
        let purpose: String
        let capabilities: [String]
        let providers: [CategoryProvider]
        let settings: CategorySettings
        var id: String { category }
    }

    struct CategoryTabs: Codable {
        let categories: [CategoryTab]?
        let order: [String]?
    }

    struct CategorySaveResult: Codable {
        let saved: Bool?
        let key_stored: Bool?
        let category: CategorySettings?
    }

    struct DiscoveredModel: Codable, Identifiable {
        let id: String
        let label: String?
        let description: String?
        let output_modalities: [String]?
        let input_modalities: [String]?
        let supported_endpoints: [String]?
        let category: String?
    }

    struct ModelDiscovery: Codable {
        let ok: Bool?
        let models: [DiscoveredModel]?
        let matching: [DiscoveredModel]?
        let matching_count: Int?
        let error: String?
    }

    struct CategoryTestResult: Codable {
        let ok: Bool?
        let detail: String?
        let error: String?
        let bytes: Int?
        let content_type: String?
        let job_id: String?
        let url: String?
        let category: CategorySettings?
    }

    // MARK: - Capability routing (speech, images, vision, video)

    struct CapabilityModel: Codable {
        let provider_id: String
        let model: String?
        let base_url: String?
        /// verified | declared | unknown
        let status: String?
        let verified_at: Double?
        let note: String?

        var isVerified: Bool { status == "verified" }
    }

    struct CapabilityAssignment: Codable {
        let task: String
        let primary: CapabilityModel?
        let fallbacks: [CapabilityModel]?
        let configured: Bool?
        let ready: Bool?
    }

    struct CapabilityRouting: Codable {
        let tasks: [String]?
        let routed: [String]?
        let assignments: [String: CapabilityAssignment]?
    }

    struct CapabilityTestAttempt: Codable {
        let provider_id: String
        let model: String?
        let status: String?
        let ok: Bool?
        let error: String?
        let duration_s: Double?
    }

    struct CapabilityTestResult: Codable {
        let task: String?
        let ok: Bool?
        let used_fallback: Bool?
        let attempts: [CapabilityTestAttempt]?
        let error: String?
        let result: CapabilityMediaResult?
        let assignments: [String: CapabilityAssignment]?
    }

    struct CapabilityMediaResult: Codable {
        let ok: Bool?
        let content_type: String?
        let bytes: Int?
        let text: String?
        let url: String?
        let job_id: String?
        let verified: Bool?
        let detail: String?
        let error: String?
    }

    // MARK: - n8n automation
    //
    // The shapes `/n8n/*` returns. Status strings stay `String` for the same
    // reason the Google ones do: a newer backend must decode rather than throw.

    struct N8NField: Codable, Identifiable {
        let name: String
        let kind: String?
        let required: Bool?
        let description: String?
        let choices: [String]?
        let max_length: Int?
        let minimum: Int?
        let maximum: Int?

        var id: String { name }
    }

    struct N8NWorkflow: Codable, Identifiable {
        let key: String
        let title: String
        let description: String?
        let risk: String?
        let requires_confirmation: Bool?
        let mode: String?
        let idempotent: Bool?
        let fields: [N8NField]?
        let returns: String?
        let enabled: Bool?

        var id: String { key }
    }

    struct N8NWorkflowList: Codable {
        let workflows: [N8NWorkflow]?
        let enabled: Bool?
        let count: Int?
    }

    struct N8NStatus: Codable {
        /// disabled | not_configured | unreachable | unauthorized | misconfigured
        /// | ready | error
        let state: String?
        let detail: String?
        let problems: [String]?
        let approved_keys: [String]?
        let latency_ms: Int?
        let config: N8NPublicConfig?
    }

    struct N8NPublicConfig: Codable {
        let enabled: Bool?
        let base_url: String?
        let configured: Bool?
        let has_api_key: Bool?
        let timeout: Double?
        let allow_remote: Bool?
        let allowed_keys: [String]?
    }

    /// The reply to `POST /n8n/workflows/{key}/run`.
    ///
    /// `ok` is the adapter's verdict and `status` is the specific outcome:
    /// a timeout, an unreachable server and a workflow that failed are three
    /// different facts, and the screen says which happened.
    struct N8NRunResult: Codable {
        let ok: Bool?
        let status: String?
        let correlation_id: String?
        let workflow: String?
        let message: String?
        let output: [String: AnyCodable]?
        let http_status: Int?
        let duration_ms: Int?
        let retry_safe: Bool?
    }

    // MARK: - MCP configuration

    /// Body of `PUT /mcp/config`.
    ///
    /// The bearer token is deliberately absent: the API does not accept one,
    /// and posting a credential to a save route would put a secret into a
    /// request body and every log line that echoes it. The token stays in the
    /// Keychain (`mcp`) or `JARVIS_MCP_TOKEN`.
    struct MCPConfigRequest: Codable {
        let url: String
        let allowed_tools: String
        let name: String
    }

    /// The block `PUT /mcp/config` echoes under `config`: the saved strings as
    /// they sit in `~/.jarvis-forge/ai_selection.json`. Note the allowlist is a
    /// comma-joined string here, not an array -- `MCPConfigSaveResult.allowed_tools`
    /// carries the parsed form.
    struct MCPConfigSaved: Codable {
        let url: String?
        let allowed_tools: String?
        let name: String?
    }

    struct MCPConfigSaveResult: Codable {
        let saved: Bool?
        let config: MCPConfigSaved?
        /// The parsed, de-duplicated allowlist the server actually saved. This
        /// is the authority, not the text that was typed.
        let allowed_tools: [String]?
    }

    // MARK: - Missions

    /// One mission contract as the server reports it.
    ///
    /// Every field past the identity is optional on purpose: the mission view
    /// must keep working when the server grows or drops a field. The rule this
    /// file already follows for `/health` -- an optional is "not sent", not a
    /// decode failure that blanks the whole screen -- applies here too.
    struct Mission: Codable, Identifiable {
        let mission_id: String
        let request: String
        let outcome: String?
        let state: String
        /// One of verified / partial / failed / unknown. The server computes it
        /// from the conditions; the client never re-derives or upgrades it.
        let verdict: String
        let reason: String?
        let conditions: [MissionCondition]?
        let steps: [MissionStep]?
        let condition_summary: [String: Int]?
        /// Which revision of the plan this mission is on. It moves only when the
        /// plan is steered, so it answers "is this still the plan that was
        /// approved?" -- unlike `version`, which moves on every write. Optional
        /// so a mission from an older backend still decodes.
        let plan_revision: Int?

        var id: String { mission_id }

        var revision: Int { plan_revision ?? 0 }
    }

    struct MissionCondition: Codable, Identifiable {
        let condition_id: String
        let statement: String
        let status: String
        let observation: String?

        var id: String { condition_id }
    }

    struct MissionStep: Codable, Identifiable {
        let step_id: String
        let title: String
        let status: String
        let detail: String?
        /// The tool this step calls. Optional so a step written by an older
        /// server, or one the planner left without a tool, still decodes.
        let tool: String?
        /// The tool call this step makes, as the server recorded it. Optional so
        /// a step written by an older server still decodes.
        let params: [String: String]?

        var id: String { step_id }
    }

    struct MissionList: Codable {
        let missions: [Mission]
    }

    /// `POST /missions/{id}/shadow`. What a plan would do, executed by nothing.
    struct ShadowReport: Codable {
        struct Step: Codable, Identifiable {
            let step_id: String
            let title: String
            let tool: String
            /// One of would_run, would_need_approval, would_be_refused,
            /// not_simulated, predicted. The server decides it; the client
            /// renders it and never upgrades it.
            let verdict: String
            let detail: String?
            let before: String?
            let after: String?
            /// True when `before` is what an earlier step in the same plan would
            /// have written, not what is on disk now.
            let source_is_shadowed: Bool?

            var id: String { step_id }
        }

        struct Summary: Codable {
            let steps: Int
            let would_run: Int
            let would_be_refused: Int
            let would_need_approval: Int
            let not_simulated: Int
        }

        let steps: [Step]
        let summary: Summary
        /// One sentence on what this shadow can and cannot tell you.
        let conclusion: String
        let note: String
        let mission_id: String
        let mission_state: String
    }

    // MARK: - Recovery Tournament

    /// A failed call from the failure ledger (`GET /failures`).
    struct RecordedFailure: Codable, Identifiable {
        let failure_id: String
        let tool: String?
        let error: String?
        let recovered: Int?
        let strategy: String?
        let attempts: Int?
        let note: String?
        let created_at: String?

        var id: String { failure_id }
    }

    /// `POST /recovery/tournament`. The ranked safe repairs for one failure.
    struct TournamentReport: Codable {
        struct Candidate: Codable, Identifiable {
            let kind: String       // retry | alternative | ask
            let tool: String
            let safety: String     // safe | unsafe
            let safety_reason: String
            let likelihood: String
            let cost: String
            let rank: Int

            var id: String { "\(kind):\(tool):\(rank)" }
        }

        let tool: String
        let failure: String
        let candidates: [Candidate]
        /// The best *automatic* repair, or nil when nothing is safe.
        let recommended: Candidate?
        let note: String
    }

    struct TournamentRequest: Codable {
        let tool: String
        let error: String
    }

    // MARK: - Agent Control Tower

    struct Worker: Codable, Identifiable {
        let name: String
        let state: String
        let tools: [String]
        let tool_count: Int
        let call_budget: Int?
        let calls_made: Int
        let calls_refused: Int
        let failures: Int
        let remaining_budget: Int?
        let last_call_at: Double?
        let last_duration_ms: Int?
        let last_tool: String?
        let last_error: String?
        let in_flight: Int
        let paused_by: String?
        let paused_reason: String?

        var id: String { name }
    }

    struct TowerSnapshot: Codable {
        struct Summary: Codable {
            let total: Int
            let running: Int
            let paused: Int
            let budget_exhausted: Int
            let stopped: Int
            let calls_made: Int
            let calls_refused: Int
        }

        let workers: [Worker]
        let summary: Summary
        /// The boundary note: a pause takes effect at the next dispatch, not
        /// mid-call. Shown so the screen never implies pre-emption it cannot do.
        let note: String
    }

    struct BudgetRequest: Codable {
        let budget: Int?
    }

    // MARK: - Privacy Egress Firewall

    /// One declared destination and why it is reachable.
    struct EgressDestination: Codable, Identifiable {
        let host: String
        let kind: String        // loopback | provider | lookup | service
        let why: String
        let private_ok: Bool

        var id: String { host }
    }

    /// `GET /egress`. What this project may contact, and what the gate does not
    /// cover -- the last field is the honest limit, shown on screen.
    struct EgressPosture: Codable {
        let privacy_mode: Bool
        let enforced_at: String
        let declared_hosts: Int
        let by_kind: [String: Int]
        let destinations: [EgressDestination]
        let not_covered: String
    }

    /// `POST /egress/check`. Would a read to this address be allowed right now?
    struct EgressVerdict: Codable {
        let allowed: Bool
        let host: String
        let kind: String
        let decision: String    // allowed | refused
        let reason: String
        let privacy_mode: Bool
    }

    struct EgressCheckRequest: Codable {
        let url: String
    }

    // MARK: - Adaptive Autonomy Control

    struct AutonomyLevel: Codable, Identifiable {
        let level: Int
        let name: String

        var id: Int { level }
    }

    /// `GET /autonomy`. The dial's position, the levels it can hold, and the
    /// ceiling no level crosses.
    struct AutonomyPosture: Codable {
        let level: Int
        let level_name: String
        let scope: String
        let note: String
        let updated_at: Double
        let ceiling_risk: Int
        let levels: [AutonomyLevel]
        /// The sentence stating a destructive/physical action is never auto-run.
        let ceiling: String
    }

    struct AutonomySetRequest: Codable {
        let level: Int
        let scope: String
        let note: String
    }

    /// `POST /autonomy/preview`. What the classifier decided and what the dial
    /// made of it -- the two kept separate so a person can see the dial's effect.
    struct AutonomyPreview: Codable {
        struct Verdict: Codable {
            let decision: String
            let reason: String
        }

        let tool: String
        let risk: Int
        let risk_label: String
        let baseline: Verdict
        let final: Verdict
        let stricter: Bool
        let level: Int
    }

    struct AutonomyPreviewRequest: Codable {
        let tool: String
        let level: Int
        let params: [String: String]
    }

    // MARK: - Outcome Assurance

    struct AssuranceKind: Codable, Identifiable {
        let kind: String
        let question: String
        let carries: String

        var id: String { kind }
    }

    struct AssuranceKinds: Codable {
        let kinds: [AssuranceKind]
        /// "An observer reads the world, never the executor's message..."
        let rule: String
    }

    /// `POST /assurance/observe`. One observation against the world, run now.
    struct AssuranceObservation: Codable {
        let status: String      // verified | failed | unknown
        let detail: String
    }

    struct AssuranceObserveRequest: Codable {
        let spec: String        // JSON, as the mission store holds it
    }

    // MARK: - Temporal Undo Map

    /// One recorded effect and whether it can honestly be reversed.
    struct UndoEntry: Codable, Identifiable {
        let entry_id: String
        let tool: String
        let path: String
        let state: String       // reversible | irreversible | conflicted | undone | unknown
        let why: String
        let undo_kind: String
        let created_at: Double
        let undone_at: Double
        let current_sha256: String

        var id: String { entry_id }
    }

    struct UndoTimeline: Codable {
        let counts: [String: Int]
        let total: Int
        /// The rule: reversible only when the bytes exist; egress has no inverse.
        let note: String
        let entries: [UndoEntry]
    }

    struct UndoResult: Codable {
        let entry_id: String
        let restored: String
        let undo_entry: String
        let verified_sha256: String
    }

    // MARK: - Live Mission Steering

    /// A proposed replacement for the un-run remainder of a plan.
    struct SteerRequest: Codable {
        struct Step: Codable {
            let step_id: String
            let title: String?
            let tool: String?
            let params: [String: String]?
        }

        let steps: [Step]
        let reason: String
    }

    /// `POST /missions/{id}/steer/preview`. What a revision would change, before
    /// anybody commits to it -- and what it cannot reach.
    struct SteerPreview: Codable {
        struct Effect: Codable, Identifiable {
            let step_id: String
            let title: String
            let tool: String
            let status: String
            let resource: String
            /// Always false on this server: a rule added now binds the future.
            let reversible: Bool
            let note: String

            var id: String { step_id }
        }

        struct Refusal: Codable, Identifiable {
            let step_id: String
            let violation: String
            let reason: String

            var id: String { step_id + violation }
        }

        let mission_id: String
        let from_revision: Int
        let to_revision: Int
        let steps: [String]
        let added_step_ids: [String]
        let removed_future_step_ids: [String]
        let completed_step_ids: [String]
        let new_tools: [String]
        let already_done: [Effect]
        let intent_refusals: [Refusal]
        let requires_new_approval: Bool
        let note: String
    }

    /// The `steering` object on a committed revision's reply.
    struct SteeringReport: Codable {
        struct Step: Codable, Identifiable {
            let step_id: String
            let title: String
            let tool: String
            let status: String

            var id: String { step_id }
        }

        let plan_revision: Int
        let steps: [Step]
        let already_done: [SteerPreview.Effect]?
        let intent_refusals: [SteerPreview.Refusal]?
        let note: String
    }

    /// The mission plus what the steer did, as `POST /steer` returns it.
    struct SteerResult: Codable {
        let mission_id: String
        let state: String
        let plan_revision: Int
        let steering: SteeringReport
    }

    struct MissionProof: Codable {
        let mission_id: String
        let verdict: String
        let claims: [ProofClaim]
        /// One sentence on what the evidence chain does and does not establish.
        let evidence_integrity: String
        let unsupported_claims: [String]?
    }

    struct ProofClaim: Codable, Identifiable {
        let condition_id: String
        let claim: String
        let status: String?
        let supported: Bool
        let observations: [ProofObservation]
        let contradictions: [ProofObservation]?

        var id: String { condition_id }
    }

    struct ProofObservation: Codable {
        let observation: String
        let evidence_id: String?
    }

    // MARK: - ParallelWorld

    struct FileOperation: Codable, Hashable {
        let op: String
        let path: String
        let content: String
    }

    struct FilePlan: Codable {
        let operations: [FileOperation]
    }

    struct FileApplyRequest: Codable {
        let operations: [FileOperation]
        let approved: Bool
    }

    struct FilePreview: Codable {
        let changes: [FileChange]
        let conflicts: [FileConflict]
        let summary: FileSummary
        let applicable: Bool
        let note: String
    }

    struct FileChange: Codable, Identifiable {
        let path: String
        let op: String
        let exists: Bool
        let before: String
        let after: String
        let truncated: Bool
        let reason: String

        var id: String { path + "|" + op }
    }

    struct FileConflict: Codable {
        let path: String
        let reason: String
    }

    struct FileSummary: Codable {
        let created: Int
        let modified: Int
        let deleted: Int
        let conflicts: Int
    }

    struct FileApplyResult: Codable {
        let applied: Bool
        let results: [FileApplyEntry]
        let rolled_back: [String]?
    }

    struct FileApplyEntry: Codable {
        let path: String
        let ok: Bool
        let detail: String
        let sha256: String?
    }

    // MARK: - Safety Center

    struct PermissionLease: Codable, Identifiable {
        let lease_id: String
        let mission_id: String
        let session_id: String?
        let capability: String
        let scope: [String]
        let risk: Int?
        let issued_at: Double?
        let expires_at: Double
        let max_uses: Int
        let uses: Int
        let revoked: Bool?
        let plan_revision: Int?

        var id: String { lease_id }
        var remaining_uses: Int { max(0, max_uses - uses) }
    }

    struct LeaseList: Codable {
        let leases: [PermissionLease]
    }

    // MARK: - Skills Library

    struct Skill: Codable, Identifiable {
        let skill_id: String
        let version: Int
        let name: String
        let description: String
        let required_capabilities: [String]
        let required_scope: [String]
        let reliability: SkillReliability?

        var id: String { skill_id }
    }

    struct SkillList: Codable {
        let skills: [Skill]
    }

    struct SkillReliability: Codable {
        let verified_successes: Int?
        let failures: Int?
        let unknowns: Int?
        /// nil until the skill has actually run. The view shows "never run" for
        /// nil rather than inventing a percentage.
        let success_rate: Double?
        let runs: Int?
        let last_duration_s: Double?
    }

    // MARK: - Skill Evolution Lab

    /// What the lab may change, what it refuses to change, and why.
    struct EvolutionLab: Codable {
        let min_runs: Int
        let can_change: [String]
        let will_not_change: [String]
        let note: String
    }

    /// A proposed next version derived from recorded runs.
    ///
    /// `manifest` is nil when nothing should change — the reason is in `notes`,
    /// never an invented improvement.
    struct EvolutionProposal: Codable {
        let skill_id: String
        let from_version: Int
        let reasons: [String]
        let changes: [String]
        let notes: [String]
        let manifest: EvolutionManifest?
        let changes_anything: Bool
        let evidence: EvolutionEvidence?

        /// The measured counters the proposal was derived from — never a score.
        struct EvolutionEvidence: Codable {
            let runs: Int
            let verified_successes: Int
            let failures: Int
            let unknowns: Int
            /// nil with no runs recorded; the view never prints 0% for it.
            let success_rate: Double?
            let last_verdict: String?
        }

        struct EvolutionManifest: Codable {
            let skill_id: String
            let name: String?
            let risk: Int?
            let steps: [Step]?
            let verification: [Check]?

            struct Step: Codable {
                let tool: String?
                let satisfies: String?
            }

            struct Check: Codable {
                let condition_id: String?
                let kind: String?
            }
        }
    }

    struct EvolutionAccept: Codable {
        let version: Int
        let note: String
    }

    /// What each condition of the skill's last run actually observed.
    struct EvolutionObservations: Codable {
        struct Condition: Codable, Identifiable {
            let condition_id: String
            let statement: String
            let verifier: String
            let status: String
            let observation: String

            var id: String { condition_id }
        }

        let skill_id: String
        let mission_id: String
        let observations: [Condition]
        let runs: Int
        let note: String
    }

    struct SkillRunRequest: Codable {
        let available_capabilities: [String]
        let allowed_scope: [String]
    }

    struct SkillRun: Codable {
        let skill_id: String
        let state: String
        let verdict: String
        let reason: String?
        let mission_id: String?
    }

    // MARK: - Trust Delta

    struct TrustDeltaRequest: Codable {
        let old: IntentScopePayload
        let new: IntentScopePayload
    }

    struct IntentScopePayload: Codable {
        let mission_id: String
        let goal: String
        let allowed_capabilities: [String]
        let allowed_resource_prefixes: [String]
        let risk_budget: Int
    }

    struct TrustDelta: Codable {
        let added_capabilities: [String]
        let removed_capabilities: [String]
        let added_resource_prefixes: [String]
        let increased_risk: Int
        let expanded: Bool
        let requires_new_approval: Bool
        let note: String
    }

    // MARK: - GoalGuard

    struct MonitoredGoal: Codable, Identifiable {
        let goal_id: String
        let kind: String
        let target: String
        let interval_seconds: Double
        let enabled: Bool
        let last_result: String?
        let last_detail: String?
        let last_observed_at: Double?
        let run_count: Int
        let consecutive_failures: Int

        var id: String { goal_id }
    }

    struct GoalList: Codable {
        let goals: [MonitoredGoal]
    }

    struct GoalTick: Codable {
        let observations: [GoalObservationEntry]
    }

    struct GoalObservationEntry: Codable {
        let goal_id: String
        let result: String
        let detail: String
    }

    // MARK: - Mission creation

    struct CreateMissionRequest: Codable {
        let request: String
        let outcome: String
        let conditions: [CreateMissionCondition]
        let steps: [CreateMissionStep]
    }

    struct CreateMissionCondition: Codable {
        let statement: String
    }

    struct CreateMissionStep: Codable {
        let title: String
        let tool: String
        let params: [String: String]
    }

    // MARK: - Project Doctor

    struct DoctorInspectRequest: Codable {
        let path: String
        let run_checks: Bool
    }

    struct DoctorRepairRequest: Codable {
        let path: String
        let finding: String
    }

    struct ProjectReport: Codable {
        let kind: String
        let root: String
        let name: String?
        let manifest: String
        let build_file: String?
        let test_command: [String]?
        let findings: [ProjectFinding]
        let problems: [ProjectFinding]
    }

    struct ProjectFinding: Codable {
        let check: String
        let status: String
        let detail: String
        let command: String?
        let exit_code: Int?
    }

    struct ProjectRepair: Codable {
        let operations: [FileOperationLoose]
        let summary: String
        let applicable: Bool
    }

    /// A repair operation as the doctor proposes it. Kept separate from the
    /// strict FileOperation because a proposal may omit fields the apply route
    /// would require -- it is a suggestion, not a validated plan.
    struct FileOperationLoose: Codable {
        let op: String?
        let path: String?
        let content: String?
    }

    // MARK: - Running a mission

    struct MissionRunRequest: Codable {
        let approval: MissionApproval?
    }

    struct MissionApproval: Codable {
        let confirmation_id: String
        let lease_id: String
    }

    struct MissionLeaseRequest: Codable {
        let capability: String
        let scope: [String]
        let max_uses: Int
        let ttl_seconds: Int
    }

    struct MissionLease: Codable {
        let lease_id: String
    }

    // MARK: - Smart Files

    struct FolderScanRequest: Codable {
        let path: String
    }

    struct FolderScan: Codable {
        let root: String
        let file_count: Int
        let truncated: Bool
        let duplicate_groups: [DuplicateGroup]
        let reclaimable_bytes: Int
        let largest: [LargeFile]
        let unreadable: [String]
        let note: String
    }

    struct DuplicateGroup: Codable {
        let sha256: String
        let size: Int
        let keep: String
        let copies: [String]
        let wasted_bytes: Int
    }

    struct LargeFile: Codable {
        let path: String
        let size: Int
        let name: String
    }

    struct OrganizePlan: Codable {
        let root: String
        let operations: [OrganizeMove]
        let summary: String
        let applicable: Bool
    }

    struct OrganizeMove: Codable {
        let op: String?
        let path: String?
        let destination: String?
        let reason: String?
    }

    struct StepDecisionRequest: Codable {
        let decision: String
        let lease_id: String?
    }

    struct RenamePlanRequest: Codable {
        let path: String
        let find: String
        let replace: String
    }

    struct RenamePlan: Codable {
        let root: String
        let operations: [OrganizeMove]
        let conflicts: [RenameConflict]
        let applicable: Bool
        let summary: String
        let preview: [RenamePreview]
    }

    struct RenameConflict: Codable {
        let name: String
        let reason: String
        let sources: [String]
    }

    struct RenamePreview: Codable {
        let from: String
        let to: String
    }

    // MARK: - Mac Control

    struct MacApp: Codable, Identifiable {
        let key: String
        let name: String
        let aliases: [String]
        let launch_target: String
        let running: Bool

        var id: String { key }
    }

    struct MacRunning: Codable {
        let name: String
    }

    struct MacStatus: Codable {
        let supported: [MacApp]
        let running: [MacRunning]
        let can_list_running: Bool
        let note: String
    }

    struct MacOpenRequest: Codable {
        let app: String
    }

    struct MacLaunchResult: Codable {
        let ok: Bool
        let launched: Bool
        let observed_running: Bool
        let detail: String
        let app: String?
    }

    // MARK: - Saved Workspaces

    struct Workspace: Codable, Identifiable {
        let workspace_id: String
        let name: String
        let description: String
        let project_dir: String
        let apps: [String]
        let files: [String]
        let urls: [String]
        let created_at: String
        let updated_at: String
        let launch_count: Int
        let last_launched_at: String?

        var id: String { workspace_id }

        var isEmpty: Bool {
            project_dir.isEmpty && apps.isEmpty && files.isEmpty && urls.isEmpty
        }
    }

    struct SavedWorkspaceList: Codable {
        let workspaces: [Workspace]
    }

    struct WorkspaceRequest: Codable {
        let name: String?
        let description: String?
        let project_dir: String?
        let apps: [String]?
        let files: [String]?
        let urls: [String]?
    }

    struct WorkspaceLaunchResult: Codable {
        struct Member: Codable {
            let kind: String
            let target: String
            let ok: Bool
            let detail: String
        }

        let workspace_id: String
        let name: String
        let results: [Member]
        let verified: Bool
        let summary: String
    }

    struct WorkspaceDuplicateRequest: Codable {
        let name: String?
    }

    struct WorkspaceDeleteResult: Codable {
        let deleted: String
    }
}

// MARK: - Automation Studio (Demonstrate-to-Automate)

extension APIModels {

    /// One semantic step captured during a demonstration.
    ///
    /// A skipped step is a real event: something happened there that the
    /// recorder refused to store (a password field, a token). It is kept so the
    /// reviewer sees the gap, and its reason names the parameter, never a value.
    struct RecordedStep: Codable, Identifiable {
        let index: Int
        let kind: String
        let label: String
        let at: Double
        let skipped: Bool
        let skip_reason: String

        var id: Int { index }
    }

    struct RecordingSession: Codable {
        let recording_id: String
        let name: String
        let started_at: Double
        let stopped_at: Double
        let open: Bool
        let duration_s: Double
        let steps: [RecordedStep]
    }

    struct RecordingStartRequest: Codable {
        let name: String
    }

    struct RecordingStepRequest: Codable {
        let kind: String
        let label: String
        let params: [String: String]
    }

    /// The reviewable workflow draft. `verdict` begins at `unverified` and only a
    /// real replay can change it, so it is never shown as a tick on the strength
    /// of a demonstration alone.
    ///
    /// Steps are editable before saving: a demonstration records what happened,
    /// and a person is allowed to correct it before it becomes a mission. The
    /// editable fields are the ones the tool's own schema declares
    /// (`AutomationKinds.fields(for:)`), never free-form replacements for a
    /// parameter the tool requires.
    /// One step as the editor holds it, sent back for review. `index` is the
    /// step's position in the *recording*, which is what identifies it: the
    /// editor may have reordered steps, so its current position says nothing
    /// about which recorded step it is editing.
    struct RecordingDraftReviewStep: Codable {
        let index: Int
        let title: String
        let tool: String
        let params: [String: String]
    }

    struct RecordingDraftReview: Codable {
        let steps: [RecordingDraftReviewStep]
    }

    struct RecordingDraft: Codable {
        struct Step: Codable, Identifiable {
            var title: String
            var tool: String
            var needs_input: String?

            /// The parameters the demonstration actually captured. Absent on a
            /// step that was skipped because it carried a sensitive value.
            var params: [String: String]?

            /// The condition this step is meant to establish, when the recorder
            /// could name one it can actually re-check.
            var satisfies: String?

            /// Where this step sat in the recording. Reordering in the editor
            /// changes the plan's order but not which recorded step this is, so
            /// the review identifies steps by this rather than by their current
            /// position.
            var recordedIndex: Int = 0

            private enum CodingKeys: String, CodingKey {
                case title, tool, needs_input, params, satisfies
            }

            init(from decoder: Decoder) throws {
                let c = try decoder.container(keyedBy: CodingKeys.self)
                title = try c.decode(String.self, forKey: .title)
                tool = try c.decode(String.self, forKey: .tool)
                needs_input = try c.decodeIfPresent(String.self, forKey: .needs_input)
                params = try c.decodeIfPresent([String: String].self, forKey: .params)
                satisfies = try c.decodeIfPresent(String.self, forKey: .satisfies)
                recordedIndex = 0
            }

            func encode(to encoder: Encoder) throws {
                var c = encoder.container(keyedBy: CodingKeys.self)
                try c.encode(title, forKey: .title)
                try c.encode(tool, forKey: .tool)
                try c.encodeIfPresent(needs_input, forKey: .needs_input)
                try c.encodeIfPresent(params, forKey: .params)
                try c.encodeIfPresent(satisfies, forKey: .satisfies)
            }

            var id: String { "\(recordedIndex)-" + title + tool
                + (params ?? [:]).sorted { $0.key < $1.key }
                    .map { $0.key + $0.value }.joined() }
        }

        struct Condition: Codable {
            let condition_id: String
            let statement: String
            let verifier: [String: String]
        }

        var recording_id: String
        var name: String
        var steps: [Step]
        /// What a replay will be checked against, derived from the parameters
        /// the demonstration captured. Empty when no step's effect can be
        /// re-observed, which reads honestly as "nothing to verify".
        var conditions: [Condition]
        var required_capabilities: [String]
        var required_scope: [String]
        var needs_input: Int
        let verdict: String
        let review_required: Bool
        let notes: String

        /// The parameters this step must carry for the tool behind it to run.
        /// Empty for a tool the schema does not describe, which the editor
        /// reports rather than guessing at.
        static func requiredFields(for tool: String) -> [AutomationKinds.ParamField] {
            AutomationKinds.fields(for: tool)
        }
    }

    struct RecordingDiscardResult: Codable {
        let discarded: Bool
    }
}

// MARK: - Automation Studio (follow-on types)

extension APIModels {

    /// `GET /intelligence/recordings/current`. `null` when nothing is open.
    struct CurrentRecording: Codable {
        let recording: RecordingSession?
    }

    struct MissionStepDraft: Codable {
        let step_id: String
        let title: String
        let tool: String
        let params: [String: String]
        /// The condition this step is meant to establish, when the recorder
        /// could name one. Absent for a step whose effect nothing can re-check.
        var satisfies: String?
    }

    /// A condition with a declarative verifier. `verifier` carries a `kind` and
    /// the fields that kind needs, in the same shape the backend parses; the
    /// wire names an observer this build knows, never code.
    struct MissionConditionDraft: Codable {
        let condition_id: String
        let statement: String
        let verifier: [String: String]
    }

    struct MissionCreateRequest: Codable {
        let request: String
        let outcome: String
        let steps: [MissionStepDraft]
        var conditions: [MissionConditionDraft] = []
    }

    /// One executed step in a run's response, flattened for display.
    struct MissionDecision: Codable {
        let decision: String
    }

    struct MissionRunStep {
        let title: String
        let ok: Bool
        let detail: String
    }

    struct MissionRunResult {
        let verdict: String
        let steps: [MissionRunStep]
    }

    /// The recorder's own action list, and the fields each action takes.
    ///
    /// These mirror `jarvis/core/recorder.py`'s `SUPPORTED_KINDS` and the
    /// registered tools' parameter names. They are written here rather than
    /// fetched because the fields are needed to draw the form *before* any
    /// request is made; `tests/test_recorder.py` pins the server side to real
    /// tools, so a drift between the two is a test failure rather than a
    /// silently unrunnable step.
    enum AutomationKinds {
        static let names = ["write_file", "read_file", "delete_file",
                            "open_application", "open_url",
                            "run_allowlisted_command", "move_file", "copy_file"]

        struct ParamField: Identifiable {
            let name: String
            let placeholder: String
            var value: String = ""
            var options: [String] = []
            /// Named rather than hidden: the person sees which field will not be
            /// stored instead of wondering why the step came back empty.
            let isSensitive: Bool

            var id: String { name }
            var isChoice: Bool { !options.isEmpty }
        }

        static func fields(for kind: String) -> [ParamField] {
            switch kind {
            case "write_file":
                return [ParamField(name: "path", placeholder: "absolute path", isSensitive: false),
                        ParamField(name: "content", placeholder: "contents", isSensitive: false),
                        ParamField(name: "mode", placeholder: "", value: "overwrite",
                                   options: ["overwrite", "append"], isSensitive: false)]
            case "read_file", "delete_file":
                return [ParamField(name: "path", placeholder: "absolute path", isSensitive: false)]
            case "open_application":
                return [ParamField(name: "app", placeholder: "e.g. finder", isSensitive: false)]
            case "open_url":
                return [ParamField(name: "url", placeholder: "https://…", isSensitive: false)]
            case "run_allowlisted_command":
                return [ParamField(name: "command", placeholder: "allowlisted command",
                                   isSensitive: false)]
            case "move_file", "copy_file":
                return [ParamField(name: "source", placeholder: "absolute path", isSensitive: false),
                        ParamField(name: "destination", placeholder: "absolute path",
                                   isSensitive: false)]
            default:
                return []
            }
        }
    }
}
