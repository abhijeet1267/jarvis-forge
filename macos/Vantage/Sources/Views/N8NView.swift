import SwiftUI

/// n8n automation: the connection's real state, and the approved workflows.
///
/// Two rules shape this screen, and they are the same two the rest of the app
/// learned the hard way:
///
/// * **A configured URL is not a working service.** The pill is drawn from
///   `/n8n/status?probe=true`, which asks n8n's own health endpoint just now.
///   "Not tested" and "unreachable" are distinct states from "ready".
/// * **The workflows listed are Vantage's approved registry, not n8n's
///   catalogue.** A workflow appears here because a person added it to the
///   backend; nothing is discovered, and nothing can be created, edited or
///   activated from this screen — the integration has no admin API access, and
///   that is deliberate.
///
/// A run reports the outcome the backend observed. A timeout, an unreachable
/// server and a workflow that failed are three different notices, because they
/// need three different fixes. A consequential workflow asks first.
struct N8NView: View {

    @State private var status: APIModels.N8NStatus?
    @State private var workflows: [APIModels.N8NWorkflow] = []
    @State private var isLoading = true
    @State private var isTesting = false
    @State private var errorMessage: String?

    /// Per-workflow input values, keyed by key then field name, so two cards
    /// cannot share a value.
    @State private var inputs: [String: [String: String]] = [:]
    /// The workflow awaiting confirmation, and the promise shown with it.
    @State private var pendingConfirm: ConfirmRequest?
    /// The workflow currently running, so its card shows progress and its
    /// button cannot start a second run.
    @State private var runningKey: String?
    /// The last result for a workflow, shown inside its card.
    @State private var results: [String: APIModels.N8NRunResult] = [:]
    @State private var resultErrors: [String: String] = [:]
    /// Vantage's own record of each workflow's latest run.
    @State private var statuses: [String: APIModels.N8NWorkflowStatus] = [:]
    @State private var togglingKey: String?

    private let api = APIClient.shared

    private struct ConfirmRequest: Identifiable {
        let id = UUID()
        let workflow: APIModels.N8NWorkflow
        let inputs: [String: String]
        /// The sentence shown on the confirmation, built from the workflow's
        /// own description — not a generic "are you sure".
        var promise: String {
            "'\(workflow.title)' can affect things outside Vantage. "
                + (workflow.description ?? "")
        }
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: V.Space.section) {
                header
                if let errorMessage {
                    Notice(kind: .error, message: errorMessage, actionTitle: "Retry") {
                        Task { await self.load() }
                    }
                }
                connectionCard
                if !workflows.isEmpty {
                    SectionHeader("Approved workflows")
                    ForEach(workflows) { workflow in
                        workflowCard(workflow)
                    }
                }
                footnote
            }
            .padding(.horizontal, V.Space.gutter)
            .padding(.vertical, V.Space.section)
            .frame(maxWidth: 780)
            .frame(maxWidth: .infinity, alignment: .center)
        }
        .background(V.background)
        .navigationTitle("Automation")
        .task { await load() }
        .refreshable { await load() }
        .sheet(item: $pendingConfirm) { request in
            confirmSheet(request)
        }
    }

    // MARK: - Header

    private var header: some View {
        PageHeader(
            title: "Automation",
            subtitle: "Approved n8n workflows, run by Vantage over their webhooks."
        ) {
            HStack(spacing: V.Space.s) {
                if isLoading || isTesting { ProgressView().controlSize(.small) }
                Button {
                    Task { await testConnection() }
                } label: {
                    Label("Test connection", systemImage: "bolt.horizontal")
                }
                .buttonStyle(VSecondaryButtonStyle())
                .disabled(isLoading || isTesting)
                Button {
                    Task { await load() }
                } label: {
                    Label("Refresh", systemImage: "arrow.clockwise")
                }
                .buttonStyle(VSecondaryButtonStyle())
                .disabled(isLoading || isTesting)
            }
        }
    }

    // MARK: - Connection

    private var connectionCard: some View {
        Card {
            VStack(alignment: .leading, spacing: V.Space.m) {
                HStack(alignment: .top, spacing: V.Space.m) {
                    Image(systemName: "point.3.connected.trianglepath.dotted")
                        .font(.system(size: 15, weight: .medium))
                        .foregroundStyle(V.accent)
                        .frame(width: 30, height: 30)
                        .background(V.accentSoft, in: RoundedRectangle(
                            cornerRadius: V.Radius.control, style: .continuous))
                    VStack(alignment: .leading, spacing: 2) {
                        Text("n8n").font(V.Font.cardTitle).foregroundStyle(V.text)
                        Text("Vantage runs only the workflows listed below, and only over "
                             + "their webhooks. It has no access to n8n's admin API.")
                            .font(V.Font.body)
                            .foregroundStyle(V.textSecondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Spacer(minLength: V.Space.m)
                    StatusPill(kind: statusKind, text: statusLabel)
                }

                if let detail = status?.detail, !detail.isEmpty {
                    Text(detail)
                        .font(V.Font.body)
                        .foregroundStyle(V.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let problems = status?.problems, !problems.isEmpty {
                    VStack(alignment: .leading, spacing: V.Space.xs) {
                        ForEach(problems, id: \.self) { problem in
                            HStack(alignment: .top, spacing: V.Space.s) {
                                Image(systemName: "exclamationmark.triangle.fill")
                                    .font(.system(size: 10, weight: .semibold))
                                    .foregroundStyle(V.warning)
                                Text(problem)
                                    .font(V.Font.caption)
                                    .foregroundStyle(V.textSecondary)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                    }
                }
                VStack(alignment: .leading, spacing: V.Space.xs) {
                    DetailRow(label: "Address",
                              value: status?.config?.base_url ?? "—",
                              monospaced: true)
                    DetailRow(label: "Credential",
                              value: status?.config?.has_api_key == true
                                ? "Stored in the keychain" : "None stored")
                    if let latency = status?.latency_ms {
                        DetailRow(label: "Health check", value: "\(latency) ms")
                    }
                    if status?.config?.allow_remote == true {
                        DetailRow(label: "Remote", value: "Allowed — non-loopback host",
                                  tone: V.warning)
                    }
                }
                .padding(.top, V.Space.xs)

                Text("Configure the URL and API key in AI Providers → Automation, or with "
                     + "JARVIS_N8N_BASE_URL and the keychain. Secrets are never shown here.")
                    .font(V.Font.caption)
                    .foregroundStyle(V.textTertiary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private var statusKind: StatusKind {
        guard let state = status?.state else {
            return isLoading ? .setup : .error
        }
        switch state {
        case "ready": return .connected
        case "disabled", "not_configured": return .idle
        case "unreachable": return .error
        case "unauthorized", "misconfigured", "error": return .error
        default: return .setup
        }
    }

    private var statusLabel: String {
        guard let state = status?.state else {
            return isLoading ? "Checking…" : "Could not read"
        }
        switch state {
        case "ready": return "Connected"
        case "disabled": return "Turned off"
        case "not_configured": return "Not configured"
        case "unreachable": return "Unreachable"
        case "unauthorized": return "Credential rejected"
        case "misconfigured": return "Configuration problem"
        case "error": return "Error"
        default: return state
        }
    }

    // MARK: - One workflow

    private func workflowCard(_ workflow: APIModels.N8NWorkflow) -> some View {
        Card(tone: workflow.enabled == false ? .ivory : .plain) {
            VStack(alignment: .leading, spacing: V.Space.s) {
                HStack(alignment: .firstTextBaseline, spacing: V.Space.s) {
                    Text(workflow.title)
                        .font(V.Font.cardTitle).foregroundStyle(V.text)
                    Spacer()
                    riskPill(workflow)
                }
                if let description = workflow.description, !description.isEmpty {
                    Text(description)
                        .font(V.Font.body)
                        .foregroundStyle(V.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }

                ForEach(workflow.fields ?? []) { field in
                    fieldRow(workflow: workflow, field: field)
                }

                if let output = results[workflow.key] {
                    resultView(output)
                }
                if let message = resultErrors[workflow.key] {
                    Notice(kind: .error, message: message, actionTitle: "Dismiss") {
                        resultErrors[workflow.key] = nil
                    }
                }

                HStack(spacing: V.Space.s) {
                    Button {
                        run(workflow, confirmed: false)
                    } label: {
                        if runningKey == workflow.key {
                            HStack(spacing: V.Space.xs) {
                                ProgressView().controlSize(.small)
                                Text("Running…")
                            }
                        } else {
                            Text(workflow.requires_confirmation == true
                                 ? "Review and run" : "Run")
                        }
                    }
                    .buttonStyle(workflow.requires_confirmation == true
                                 ? AnyButtonStyle(VSecondaryButtonStyle())
                                 : AnyButtonStyle(VPrimaryButtonStyle()))
                    .disabled(workflow.enabled == false || runningKey != nil
                              || !requiredInputsFilled(workflow))
                    .help(workflow.enabled == false
                          ? "This workflow is not on the Vantage allowlist."
                          : (requiredInputsFilled(workflow)
                             ? "Run this workflow"
                             : "Fill in the required inputs first"))

                    if let output = results[workflow.key],
                       output.retry_safe == true, output.ok == false {
                        Button("Retry") { run(workflow, confirmed: true) }
                            .buttonStyle(VQuietButtonStyle())
                            .disabled(runningKey != nil)
                            .help("Repeat this run — the backend reports it is safe to repeat")
                    }
                    if let correlation = results[workflow.key]?.correlation_id,
                       !correlation.isEmpty {
                        Text(correlation)
                            .font(V.Font.mono)
                            .foregroundStyle(V.textTertiary)
                            .textSelection(.enabled)
                    }
                    Spacer()
                }
                .padding(.top, V.Space.xs)
            }
        }
    }

    private func riskPill(_ workflow: APIModels.N8NWorkflow) -> some View {
        let (kind, label): (StatusKind, String) = {
            switch workflow.risk {
            case "read": return (.connected, "Read-only")
            case "mutating": return (.pending, "Changes data")
            case "external": return (.pending, "Leaves Vantage")
            default: return (.idle, workflow.risk ?? "Unknown")
            }
        }()
        return StatusPill(kind: kind, text: label)
    }

    @ViewBuilder
    private func fieldRow(workflow: APIModels.N8NWorkflow, field: APIModels.N8NField) -> some View {
        let binding = Binding<String>(
            get: { inputs[workflow.key]?[field.name] ?? "" },
            set: { value in
                var forWorkflow = inputs[workflow.key] ?? [:]
                forWorkflow[field.name] = value
                inputs[workflow.key] = forWorkflow
            })
        if let choices = field.choices, !choices.isEmpty {
            Picker(fieldLabel(field), selection: binding) {
                Text("Choose…").tag("")
                ForEach(choices, id: \.self) { Text($0).tag($0) }
            }
            .frame(maxWidth: 260)
        } else {
            HStack(spacing: V.Space.s) {
                VField(label: fieldLabel(field),
                       placeholder: field.description ?? field.name,
                       text: binding)
                if field.kind == "integer" {
                    Text("number").font(V.Font.caption).foregroundStyle(V.textTertiary)
                }
            }
        }
    }

    private func fieldLabel(_ field: APIModels.N8NField) -> String {
        let name = field.name.replacingOccurrences(of: "_", with: " ").capitalized
        return (field.required == true) ? "\(name) *" : name
    }

    private func requiredInputsFilled(_ workflow: APIModels.N8NWorkflow) -> Bool {
        (workflow.fields ?? []).allSatisfy { field in
            guard field.required == true else { return true }
            let value = inputs[workflow.key]?[field.name] ?? ""
            return !value.trimmingCharacters(in: .whitespaces).isEmpty
        }
    }

    // MARK: - Result

    @ViewBuilder
    private func resultView(_ result: APIModels.N8NRunResult) -> some View {
        if result.ok == true {
            Notice(kind: .success, message: result.message ?? "The workflow completed.")
        } else {
            // The specific outcome, because the fixes differ.
            VStack(alignment: .leading, spacing: V.Space.xs) {
                Notice(kind: .error,
                       message: "\(Self.failureLabel(result.status)): \(result.message ?? "")")
                if result.retry_safe == false {
                    Text("Repeating this run may repeat what it already did, so retry is "
                         + "not offered here.")
                        .font(V.Font.caption)
                        .foregroundStyle(V.textTertiary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }

    /// The specific outcome word, because the fixes differ.
    static func failureLabel(_ status: String?) -> String {
        switch status {
        case "timeout": return "Timed out"
        case "unreachable": return "n8n unreachable"
        case "failed": return "Workflow failed"
        case "refused": return "Refused"
        default: return "Did not complete"
        }
    }

    // MARK: - Confirmation

    private func confirmSheet(_ request: ConfirmRequest) -> some View {
        VStack(alignment: .leading, spacing: V.Space.l) {
            Text("Run “\(request.workflow.title)”?")
                .font(V.Font.pageTitle).foregroundStyle(V.text)
            Text(request.promise)
                .font(V.Font.body).foregroundStyle(V.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
            if !request.inputs.isEmpty {
                VStack(alignment: .leading, spacing: V.Space.xs) {
                    SectionHeader("It will receive")
                    ForEach(request.inputs.sorted(by: { $0.key < $1.key }), id: \.key) { pair in
                        DetailRow(label: pair.key, value: pair.value)
                    }
                }
            }
            HStack(spacing: V.Space.s) {
                Button("Run it") {
                    pendingConfirm = nil
                    perform(request.workflow, confirmed: true)
                }
                .buttonStyle(VPrimaryButtonStyle())
                Button("Cancel") { pendingConfirm = nil }
                    .buttonStyle(VQuietButtonStyle(tint: V.textSecondary))
                Spacer()
            }
        }
        .padding(V.Space.xl)
        .frame(width: 520)
        .background(V.background)
    }

    // MARK: - Actions

    private func load() async {
        isLoading = true
        errorMessage = nil
        defer { isLoading = false }
        async let statusCall: APIModels.N8NStatus? = try? api.n8nStatus(probe: false)
        async let workflowsCall: APIModels.N8NWorkflowList? = try? api.n8nWorkflows()
        let (s, list) = await (statusCall, workflowsCall)
        if s == nil && list == nil {
            errorMessage = "Could not read the automation state from the backend."
            return
        }
        status = s
        workflows = list?.workflows ?? []
        for workflow in workflows {
            await refreshStatus(workflow.key)
        }
        // The list read proves the backend is answering; the health probe is a
        // separate, deliberate act so opening the screen does not spend a
        // round trip against a server that is off.
        await probeIfEnabled()
    }

    private func probeIfEnabled() async {
        guard status?.state != "disabled", status?.state != "not_configured" else { return }
        isTesting = true
        defer { isTesting = false }
        do { status = try await api.n8nStatus(probe: true) }
        catch { /* keep the non-probed state; the banner already says what failed */ }
    }

    private func testConnection() async {
        isTesting = true
        errorMessage = nil
        defer { isTesting = false }
        do {
            status = try await api.n8nStatus(probe: true)
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func run(_ workflow: APIModels.N8NWorkflow, confirmed: Bool) {
        var values: [String: String] = [:]
        for (key, value) in inputs[workflow.key] ?? [:] where !value.isEmpty {
            values[key] = value
        }
        if workflow.requires_confirmation == true && !confirmed {
            pendingConfirm = ConfirmRequest(workflow: workflow, inputs: values)
            return
        }
        perform(workflow, confirmed: confirmed, inputs: values)
    }

    /// Enable or disable one workflow **in Vantage**. The backend never touches
    /// n8n for this, so no workflow or history is affected.
    private func setEnabled(_ workflow: APIModels.N8NWorkflow, _ enabled: Bool) async {
        guard togglingKey == nil else { return }
        togglingKey = workflow.key
        errorMessage = nil
        defer { togglingKey = nil }
        do {
            _ = try await api.n8nSetWorkflowEnabled(workflow.key, enabled: enabled)
            await load()
        } catch {
            errorMessage = error.localizedDescription
            await load()      // put the switch back where the backend says it is
        }
    }

    private func refreshStatus(_ key: String) async {
        if let fresh = try? await api.n8nWorkflowStatus(key) {
            statuses[key] = fresh
        }
    }

    private func perform(_ workflow: APIModels.N8NWorkflow, confirmed: Bool,
                         inputs values: [String: String]? = nil) {
        guard runningKey == nil else { return }
        runningKey = workflow.key
        resultErrors[workflow.key] = nil
        let payload = values ?? (inputs[workflow.key] ?? [:]).filter { !$0.value.isEmpty }
        Task {
            defer { runningKey = nil }
            do {
                results[workflow.key] = try await api.n8nRun(
                    workflow.key, inputs: payload, confirmed: confirmed)
                await refreshStatus(workflow.key)
            } catch {
                // A 409 here is a confirmation the backend still wants; saying
                // so is more useful than the generic HTTP text.
                results[workflow.key] = nil
                resultErrors[workflow.key] = error.localizedDescription
            }
        }
    }

    private var footnote: some View {
        Text("Every run is recorded with a correlation id so it can be matched to n8n's "
             + "own execution log. Inputs are validated against the workflow's declared "
             + "schema before anything is sent, and a workflow Vantage does not list "
             + "cannot be run from here or from chat.")
            .font(V.Font.caption)
            .foregroundStyle(V.textTertiary)
            .fixedSize(horizontal: false, vertical: true)
    }
}
