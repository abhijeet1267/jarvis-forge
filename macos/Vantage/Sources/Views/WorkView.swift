import SwiftUI

/// Workspaces and workflow jobs — the native counterpart of `/work`.
///
/// A workspace is a folder Vantage may inspect and test; nothing more. A job's
/// progress is per step, so a job whose step is waiting for approval is never
/// shown as finished.
struct WorkView: View {

    @State private var workspaces: [APIModels.WorkspaceInfo] = []
    @State private var jobs: [APIModels.TaskInfo] = []
    @State private var detail: [String: AnyCodable]?
    @State private var detailTitle = ""
    @State private var newPath = ""
    @State private var isLoading = true
    @State private var errorMessage: String?
    /// The last *read* failed, so an empty list below means "unknown", not
    /// "nothing there". Kept apart from `errorMessage`, which an action also
    /// sets: a failed "run automation" must not make the list deny it has rows.
    @State private var readFailed = false
    @State private var expanded: Set<String> = []
    @State private var templates: [APIModels.WorkflowTemplate] = []
    @State private var runOutcome = ""
    @State private var coding: APIModels.CodingAvailability?
    @State private var codingGoal = ""
    @State private var codingPath = ""
    @State private var codingResult = ""
    @State private var codingBusy = false
    @State private var agents: [APIModels.AgentSummary] = []

    private let api = APIClient.shared

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            if let error = errorMessage {
                ErrorBanner(message: error, retry: { Task { await load() } })
                    .padding(.horizontal, 24).padding(.bottom, 8)
            }
            if isLoading {
                Spacer(); ProgressView().frame(maxWidth: .infinity); Spacer()
            } else {
                ScrollView {
                    HStack(alignment: .top, spacing: 18) {
                        VStack(alignment: .leading, spacing: 18) {
                            workspaceSection
                            agentSection
                            codingSection
                            automationSection
                            jobSection
                        }
                        detailSection.frame(maxWidth: 380)
                    }
                    .padding(24)
                }
            }
        }
        .task { await load() }
    }

    private var header: some View {
        PageHeader(title: "Work", subtitle: "Workspaces, agents and scheduled jobs.") {
            Button { Task { await load() } } label: {
                Label("Refresh", systemImage: "arrow.clockwise")
            }
            .buttonStyle(VSecondaryButtonStyle())
        }
        .padding(.horizontal, V.Space.gutter)
        .padding(.top, V.Space.section)
        .padding(.bottom, V.Space.l)
    }

    private var workspaceSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Workspaces").font(V.Font.caption.weight(.semibold)).tracking(0.6).foregroundStyle(V.textTertiary).textCase(.uppercase)
            if workspaces.isEmpty {
                Text(readFailed
                     ? "I could not read your workspaces, so this is not an empty list."
                     : "No workspace open.")
                    .foregroundStyle(V.textSecondary)
            } else {
                ForEach(workspaces) { w in
                    HStack {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(w.name ?? w.workspace_id)
                            Text(w.root ?? "").font(.caption2).foregroundStyle(V.textSecondary)
                        }
                        Spacer()
                        Button("Inspect") { Task { await inspect(w) } }.buttonStyle(.borderless)
                        Button("Open") {
                            Task { await runCommand("open \(w.root ?? "") in cursor") }
                        }.buttonStyle(.borderless)
                    }
                    .padding(.vertical, 3)
                    Divider()
                }
            }
            HStack(spacing: 8) {
                TextField("/Users/me/code/project", text: $newPath)
                    .textFieldStyle(.roundedBorder)
                Button("Open") { Task { await openWorkspace() } }
                    .buttonStyle(.borderedProminent)
                    .disabled(newPath.trimmingCharacters(in: .whitespaces).isEmpty)
            }
            Text("A workspace is a folder Vantage may inspect and test, nothing more.")
                .font(.caption2).foregroundStyle(V.textSecondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// The specialised agents and what each one may do.
    ///
    /// Read-only on purpose: an agent executes a PlannedAction inside a plan the
    /// permission engine has already judged, so starting work from here would
    /// route around that gate. This shows capability; actions run through chat.
    private var agentSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Agents").font(V.Font.caption.weight(.semibold)).tracking(0.6).foregroundStyle(V.textTertiary).textCase(.uppercase)
            Text("\(agents.count) specialised agents, "
                 + "\(agents.reduce(0) { $0 + $1.tool_count }) tool assignments.")
                .font(.caption2).foregroundStyle(V.textSecondary)
            ForEach(agents) { a in
                HStack {
                    Text(a.usable ? a.name : "\(a.name) (no tools)")
                        .font(.caption)
                    Spacer()
                    Text("\(a.tool_count) tool(s)").font(.caption2)
                        .foregroundStyle(V.textSecondary)
                }
            }
            Text("An agent executes inside a plan the permission engine has judged. Nothing is started from this screen.")
                .font(.caption2).foregroundStyle(V.textSecondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// Coding tasks: hand a goal to the agent and see what it did.
    ///
    /// Start stays disabled when no agent is installed, and a task that could not
    /// reach its model is shown as a failure with the reason — never as work
    /// completed.
    private var codingSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Coding tasks").font(V.Font.caption.weight(.semibold)).tracking(0.6).foregroundStyle(V.textTertiary).textCase(.uppercase)
            // A failed check used to leave "Checking for a coding agent…" on
            // screen for good: this placeholder doubled as both the loading
            // state and the unknown state. Say which one it is.
            Text(coding?.detail
                 ?? (isLoading ? "Checking for a coding agent…"
                               : "I could not check whether a coding agent is available here. Start is disabled until I can."))
                .font(.caption2).foregroundStyle(V.textSecondary)
            HStack(spacing: 8) {
                TextField("What should the agent do?", text: $codingGoal)
                    .textFieldStyle(.roundedBorder)
                Button("Start") { Task { await startCoding() } }
                    .buttonStyle(.borderedProminent)
                    .disabled(coding?.available != true || codingBusy
                              || codingGoal.trimmingCharacters(in: .whitespaces).isEmpty)
            }
            TextField("Folder (optional, defaults to this repo)", text: $codingPath)
                .textFieldStyle(.roundedBorder)
            if !codingResult.isEmpty {
                Text(codingResult).font(.caption2)
                    .foregroundStyle(V.textSecondary)
                    .textSelection(.enabled)
            }
            Text("This runs the real coding agent in that folder, with file access, so it asks only when you start it. Vantage reports what the agent answered and which files changed.")
                .font(.caption2).foregroundStyle(V.textSecondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func startCoding() async {
        let goal = codingGoal.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !goal.isEmpty else { return }
        codingBusy = true
        codingResult = "Working… this can take a few minutes."
        do {
            let result = try await api.startCodingTask(
                goal: goal, path: codingPath.trimmingCharacters(in: .whitespacesAndNewlines))
            var lines = ["job \(result.job_id ?? "?") · \(result.ok ? "completed" : "failed")"
                         + (result.verified == true ? " · verified" : " · unverified")]
            if let message = result.message, !message.isEmpty { lines.append(message) }
            if let changed = result.changed_files, !changed.isEmpty {
                lines.append("\(changed.count) file(s) changed:")
                lines.append(contentsOf: changed.prefix(12).map { "  " + $0 })
            }
            codingResult = lines.joined(separator: "\n")
            await load()
        } catch {
            codingResult = error.localizedDescription
        }
        codingBusy = false
    }

    /// The pre-built automations, run from here. Each is a fixed list of real
    /// steps, and the reported outcome is the engine's, not optimistic.
    private var automationSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Automations").font(V.Font.caption.weight(.semibold)).tracking(0.6).foregroundStyle(V.textTertiary).textCase(.uppercase)
            if templates.isEmpty {
                Text(readFailed
                     ? "I could not read the automation list, so this is not an empty list."
                     : "No automations available.")
                    .foregroundStyle(V.textSecondary)
            } else {
                ForEach(templates) { t in
                    HStack(alignment: .top) {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(t.name ?? t.workflow_id)
                            Text(t.description ?? "").font(.caption2)
                                .foregroundStyle(V.textSecondary)
                        }
                        Spacer()
                        Button("Run") { Task { await runTemplate(t) } }.buttonStyle(.borderless)
                    }
                    .padding(.vertical, 3)
                    Divider()
                }
            }
            if !runOutcome.isEmpty {
                Text(runOutcome).font(.caption2).foregroundStyle(V.textSecondary)
            }
            Text("Each automation is a fixed list of real steps. A step that cannot do its job reports failure — it is never counted as done.")
                .font(.caption2).foregroundStyle(V.textSecondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func runTemplate(_ t: APIModels.WorkflowTemplate) async {
        do {
            let outcome = try await api.runWorkflow(t.workflow_id)
            if let error = outcome.error, outcome.run_id == nil {
                runOutcome = "\(t.name ?? t.workflow_id) did not run: \(error)"
            } else {
                runOutcome = "\(t.name ?? t.workflow_id): \((outcome.current_status ?? outcome.status) ?? "?") · step \(outcome.step ?? 0) of \(outcome.total_steps ?? 0)"
            }
            await load()
        } catch {
            runOutcome = error.localizedDescription
        }
    }

    private var jobSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Workflow jobs").font(V.Font.caption.weight(.semibold)).tracking(0.6).foregroundStyle(V.textTertiary).textCase(.uppercase)
            if jobs.isEmpty {
                Text(readFailed
                     ? "I could not read your workflow jobs, so this is not an empty list."
                     : "No workflow jobs yet.")
                    .foregroundStyle(V.textSecondary)
            } else {
                ForEach(jobs) { job in
                    VStack(alignment: .leading, spacing: 4) {
                        HStack(alignment: .top) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(job.goal ?? job.job_id)
                                Text("\(job.status ?? "?")  ·  \(job.job_id)")
                                    .font(.caption2).foregroundStyle(V.textSecondary)
                            }
                            Spacer()
                            Button(expanded.contains(job.job_id) ? "Hide" : "Steps") {
                                Task { await toggleSteps(job) }
                            }.buttonStyle(.borderless)
                            if job.status == "running" {
                                Button("Pause") { Task { await act(job, "pause") } }.buttonStyle(.borderless)
                            }
                            Button("Cancel") { Task { await act(job, "cancel") } }.buttonStyle(.borderless)
                        }
                        if expanded.contains(job.job_id) {
                            ForEach(Array((job.steps ?? []).enumerated()), id: \.offset) { idx, raw in
                                let s = raw.dictionary ?? [:]
                                VStack(alignment: .leading, spacing: 2) {
                                    HStack(spacing: 6) {
                                        Text("\(idx + 1).").font(.caption2).foregroundStyle(V.textSecondary)
                                        Text(s["status"]?.stringValue ?? "?")
                                            .font(.caption2)
                                            .foregroundStyle(stepColour(s["status"]?.stringValue))
                                        Text(s["name"]?.stringValue ?? "")
                                            .font(.caption2)
                                    }
                                    // The step's own words: why it failed, or which
                                    // files a coding run changed. Without this the
                                    // list showed a status and nothing to act on.
                                    if let detail = s["detail"]?.stringValue, !detail.isEmpty {
                                        Text(detail)
                                            .font(.caption2)
                                            .foregroundStyle(V.textSecondary)
                                            .textSelection(.enabled)
                                            .fixedSize(horizontal: false, vertical: true)
                                    }
                                }
                                .padding(.leading, 8)
                            }
                        }
                    }
                    .padding(.vertical, 4)
                    Divider()
                }
            }
            Text("A job's progress is per step. A step waiting for approval is not \"done\".")
                .font(.caption2).foregroundStyle(V.textSecondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var detailSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Workspace detail").font(V.Font.caption.weight(.semibold)).tracking(0.6).foregroundStyle(V.textTertiary).textCase(.uppercase)
            if let d = detail {
                Text(detailTitle)
                row("git repo", (d["is_git_repo"]?.boolValue == true) ? "yes" : "no")
                row("source files", d["file_count"]?.intValue.map(String.init) ?? "—")
                row("test files", d["test_file_count"]?.intValue.map(String.init) ?? "—")
                row("languages", ((d["languages"]?.dictionary)?.keys.sorted().joined(separator: ", ")) ?? "—")
                row("test runners", {
                    let arr = d["likely_test_runners"]?.arrayValue ?? []
                    let names = arr.compactMap { $0 as? String }
                    return names.isEmpty ? "—" : names.joined(separator: ", ")
                }())
            } else {
                Text("Open a workspace to inspect it.").foregroundStyle(V.textSecondary)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func row(_ k: String, _ v: String) -> some View {
        HStack(alignment: .top, spacing: 8) {
            Text(k).font(.caption2).foregroundStyle(V.textSecondary).frame(width: 90, alignment: .leading)
            Text(v).font(.caption2)
        }
    }

    private func stepColour(_ status: String?) -> Color {
        switch status {
        case "succeeded", "success", "completed": return V.accent
        case "failed", "blocked": return V.danger
        case "awaiting_confirmation", "pending", "running": return V.warning
        default: return .secondary
        }
    }

    // MARK: - Actions

    private func load() async {
        isLoading = true; errorMessage = nil; readFailed = false
        // These two are independent of the lists below and were previously
        // fetched *after* them, inside the throwing region: a failure on any
        // list skipped them entirely, so the coding section never learned
        // anything at all.
        coding = try? await api.codingAvailability()
        agents = (try? await api.agents()) ?? []
        do {
            async let w = api.workspaces()
            async let j = api.workflowJobs()
            async let t = api.workflowTemplates()
            // Read all three before publishing any. Assigning as each one
            // arrived meant a failure on the second left the first list full,
            // the rest empty, and the screen claiming "No automations
            // available" under a banner saying the server was unreachable.
            let (loadedWorkspaces, loadedJobs, loadedTemplates) = try await (w, j, t)
            workspaces = loadedWorkspaces
            jobs = loadedJobs
            templates = loadedTemplates
        } catch {
            errorMessage = error.localizedDescription
            readFailed = true
        }
        isLoading = false
    }

    private func openWorkspace() async {
        let path = newPath.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !path.isEmpty else { return }
        do {
            _ = try await api.openWorkspace(path: path)
            newPath = ""
            await load()
        } catch { errorMessage = error.localizedDescription }
    }

    private func inspect(_ w: APIModels.WorkspaceInfo) async {
        do {
            detail = try await api.inspectWorkspace(w.workspace_id)
            detailTitle = w.name ?? w.workspace_id
        } catch { errorMessage = error.localizedDescription }
    }

    private func toggleSteps(_ job: APIModels.TaskInfo) async {
        if expanded.contains(job.job_id) { expanded.remove(job.job_id); return }
        expanded.insert(job.job_id)
        if let full = try? await api.workflowJob(job.job_id),
           let i = jobs.firstIndex(where: { $0.job_id == job.job_id }) {
            jobs[i] = full
        }
    }

    private func act(_ job: APIModels.TaskInfo, _ action: String) async {
        do {
            _ = try await api.workflowAction(job.job_id, action: action)
            await load()
        } catch { errorMessage = error.localizedDescription }
    }

    private func runCommand(_ text: String) async {
        do {
            _ = try await api.post("/command",
                                   body: APIModels.CommandRequest(text: text),
                                   as: APIModels.CommandResponse.self)
        } catch { errorMessage = error.localizedDescription }
    }
}