import SwiftUI

/// Audit log and task management combined view.
struct ActivityView: View {

    @State private var selectedTab: ActivityTab = .tasks

    var body: some View {
        VStack(spacing: 0) {
            PageHeader(title: "Activity", subtitle: "Tasks Vantage is running, and the audit log.")
                .padding(.horizontal, V.Space.gutter)
                .padding(.top, V.Space.section)
                .padding(.bottom, V.Space.l)
                .frame(maxWidth: 760)
                .frame(maxWidth: .infinity, alignment: .center)

            Picker("View", selection: $selectedTab) {
                Text("Tasks").tag(ActivityTab.tasks)
                Text("Audit Log").tag(ActivityTab.audit)
            }
            .pickerStyle(.segmented)
            .padding(.horizontal, V.Space.gutter)
            .padding(.bottom, V.Space.s)
            .frame(maxWidth: 760, alignment: .leading)
            .frame(maxWidth: 400)

            switch selectedTab {
            case .tasks: TasksList()
            case .audit: AuditList()
            }
        }
        .navigationTitle("Activity")
    }
}

enum ActivityTab { case tasks, audit }

// MARK: - Tasks list

struct TasksList: View {
    @State private var tasks: [APIModels.TaskInfo] = []
    @State private var isLoading = true
    @State private var errorMessage: String?
    @State private var selectedTask: APIModels.TaskInfo?

    private let api = APIClient.shared

    var body: some View {
        VStack {
            if let error = errorMessage {
                ErrorBanner(message: error, retry: { Task { await load() } })
                    .padding(.horizontal, 24)
            }

            if isLoading {
                Spacer()
                ProgressView()
                Spacer()
            } else if tasks.isEmpty {
                Spacer()
                if errorMessage != nil {
                    Text("I could not read the task list, so this is not an empty list.")
                        .foregroundStyle(V.textSecondary)
                } else {
                    Text("No tasks.")
                        .foregroundStyle(V.textSecondary)
                }
                Spacer()
            } else {
                List(tasks) { task in
                    TaskRow(task: task, onAction: { action in
                        await handleTaskAction(task.job_id, action: action)
                    })
                    .onTapGesture {
                        selectedTask = task
                    }
                }
                .listStyle(.inset)
            }
        }
        .task { await load() }
        .refreshable { await load() }
        .sheet(item: $selectedTask) { task in
            TaskDetailSheet(task: task)
        }
    }

    private func load() async {
        isLoading = true
        errorMessage = nil
        do {
            tasks = try await api.get("/tasks", as: [APIModels.TaskInfo].self)
        } catch {
            errorMessage = error.localizedDescription
        }
        isLoading = false
    }

    private func handleTaskAction(_ jobId: String, action: String) async {
        do {
            let _: [String: String] = try await api.post("/tasks/\(jobId)/\(action)")
            await load()
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}

// MARK: - Task row

struct TaskRow: View {
    let task: APIModels.TaskInfo
    var onAction: (String) async -> Void
    @State private var confirmCancel = false

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: statusIcon)
                .foregroundStyle(statusColor)

            VStack(alignment: .leading, spacing: 2) {
                Text(task.displayName)
                    .font(.callout)
                if let updated = task.updated_at {
                    Text(updated)
                        .font(.caption2)
                        .foregroundStyle(V.textSecondary)
                }
            }

            Spacer()

            HStack(spacing: 6) {
                if task.status == "running" {
                    Button("Pause") { Task { await onAction("pause") } }
                        .buttonStyle(.plain)
                        .font(.caption)
                } else if task.status == "paused" {
                    Button("Resume") { Task { await onAction("resume") } }
                        .buttonStyle(.plain)
                        .font(.caption)
                        .foregroundStyle(V.accent)
                }
                if task.status != "completed" && task.status != "cancelled" && task.status != "failed" {
                    Button { confirmCancel = true } label: {
                        Image(systemName: "xmark.circle")
                            .font(.caption)
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(V.danger)
                }
            }
        }
        .padding(.vertical, 4)
        .alert("Cancel this task?", isPresented: $confirmCancel) {
            Button("Cancel Task", role: .destructive) {
                Task { await onAction("cancel") }
            }
            Button("Keep Running", role: .cancel) { }
        } message: {
            Text("Work in progress is stopped. Completed steps are kept.")
        }
    }

    private var statusIcon: String {
        switch task.status {
        case "running": return "play.circle.fill"
        case "paused": return "pause.circle.fill"
        case "completed": return "checkmark.circle.fill"
        case "failed": return "xmark.circle.fill"
        case "cancelled": return "stop.circle.fill"
        default: return "questionmark.circle"
        }
    }

    private var statusColor: Color {
        switch task.status {
        case "running": return V.accent
        case "completed": return V.accent
        case "failed": return V.danger
        case "paused": return V.warning
        case "cancelled": return .secondary
        default: return .secondary
        }
    }
}

// MARK: - Task detail

struct TaskDetailSheet: View {
    let task: APIModels.TaskInfo

    /// The job's own detail, fetched on appear.
    ///
    /// ``GET /tasks`` is a *list*: it carries the job id, the goal and the
    /// status, and no steps. ``GET /tasks/{job_id}`` is the detail, and it is
    /// where the steps live. The sheet used to render the list row's fields
    /// and show a "Steps" heading that could never appear, because the list
    /// payload has no steps in it.
    @State private var detail: APIModels.TaskInfo?
    @State private var loadError: String?

    private let api = APIClient.shared

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text(detail?.displayName ?? task.displayName)
                .font(.headline)
            if let status = detail?.status ?? task.status {
                Text("Status: \(status)")
                    .font(.callout)
            }
            if let created = detail?.created_at ?? task.created_at {
                Text("Created: \(created)")
                    .font(.caption)
                    .foregroundStyle(V.textSecondary)
            }
            if let loadError {
                Text(loadError)
                    .font(.caption)
                    .foregroundStyle(V.danger)
            }
            let steps = detail?.steps ?? []
            if !steps.isEmpty {
                Text("Steps")
                    .font(.subheadline)
                    .fontWeight(.medium)
                ForEach(Array(steps.enumerated()), id: \.offset) { (_, step) in
                    Text(String(describing: step.value))
                        .font(.caption)
                        .foregroundStyle(V.textSecondary)
                }
            }
            Spacer()
        }
        .padding(24)
        .frame(width: 420, height: 320)
        .task { await loadDetail() }
    }

    /// Fetch the job's own detail, which is where the steps are.
    ///
    /// A failure here is not fatal: the sheet still shows what the list
    /// row carried, and says that the detail could not be read.
    private func loadDetail() async {
        loadError = nil
        do {
            detail = try await api.get("/tasks/\(task.job_id)",
                                       as: APIModels.TaskInfo.self)
        } catch {
            loadError = "Could not load this job's steps: \(error.localizedDescription)"
        }
    }
}

// MARK: - Audit list

struct AuditList: View {
    @State private var entries: [APIModels.AuditEntry] = []
    @State private var isLoading = true
    @State private var errorMessage: String?

    private let api = APIClient.shared

    var body: some View {
        VStack {
            if let error = errorMessage {
                ErrorBanner(message: error, retry: { Task { await load() } })
                    .padding(.horizontal, 24)
            }

            if isLoading {
                Spacer()
                ProgressView()
                Spacer()
            } else if entries.isEmpty {
                Spacer()
                if errorMessage != nil {
                    Text("I could not read the audit log, so this is not an empty log.")
                        .foregroundStyle(V.textSecondary)
                } else {
                    Text("No audit entries.")
                        .foregroundStyle(V.textSecondary)
                }
                Spacer()
            } else {
                List(entries) { entry in
                    AuditRow(entry: entry)
                }
                .listStyle(.inset)
            }
        }
        .task { await load() }
        .refreshable { await load() }
    }

    private func load() async {
        isLoading = true
        errorMessage = nil
        do {
            entries = try await api.get("/audit", as: [APIModels.AuditEntry].self)
        } catch {
            errorMessage = error.localizedDescription
        }
        isLoading = false
    }
}

// MARK: - Audit row

struct AuditRow: View {
    let entry: APIModels.AuditEntry

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: resultIcon)
                .foregroundStyle(resultColor)

            VStack(alignment: .leading, spacing: 2) {
                Text(entry.displayTitle)
                    .font(.callout)
                let intent = entry.displayIntent
                if !intent.isEmpty {
                    Text(intent)
                        .font(.caption)
                        .foregroundStyle(V.textSecondary)
                        .lineLimit(1)
                }
                let detail = entry.displayDetail
                if !detail.isEmpty {
                    Text(detail)
                        .font(.caption2)
                        .foregroundStyle(V.textTertiary)
                        .lineLimit(1)
                }
            }

            Spacer()

            if let timestamp = entry.created_at {
                Text(timestamp)
                    .font(.caption2)
                    .foregroundStyle(V.textSecondary)
            }
        }
        .padding(.vertical, 2)
    }

    /// The icon for the row's outcome.
    ///
    /// The server's own vocabulary is used, in full:
    /// `success`, `unverified`, `failure`, `denied`, `blocked`, `expired`,
    /// `awaiting_confirmation`, `pending`, `clarification`, `recovered`,
    /// `no_action`. The previous switch knew only `allowed` / `denied` /
    /// `error`, so **every** row -- including the successful ones -- fell
    /// through to a neutral grey circle.
    ///
    /// `unverified` is its own case and is not `success`: the server records
    /// it when a tool reported `ok` but the effect could not be confirmed.
    /// Drawing it as a green tick would claim a verification that did not
    /// happen.
    private var resultIcon: String {
        switch entry.result {
        case "success", "allowed", "executed": return "checkmark.circle.fill"
        case "recovered": return "arrow.uturn.backward.circle.fill"
        case "unverified": return "questionmark.circle.fill"
        case "denied", "blocked": return "xmark.circle.fill"
        case "failure", "error", "failed": return "exclamationmark.triangle.fill"
        case "expired": return "hourglass.bottomhalf.filled"
        case "clarification": return "questionmark.bubble.fill"
        case "awaiting_confirmation", "pending": return "clock.fill"
        case "no_action": return "minus.circle"
        default: return "circle"
        }
    }

    private var resultColor: Color {
        switch entry.result {
        case "success", "allowed", "executed", "recovered": return V.accent
        case "unverified": return V.warning
        case "denied", "blocked": return V.danger
        case "failure", "error", "failed": return V.warning
        case "expired": return .secondary
        case "clarification": return V.accent
        case "awaiting_confirmation", "pending": return V.gold
        case "no_action": return .secondary
        default: return .secondary
        }
    }
}