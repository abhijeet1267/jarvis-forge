import SwiftUI

/// The remote MCP connector: one server, its real state, and what it may do.
///
/// This screen exists to make one distinction visible that a status boolean
/// usually hides: **listed is not callable.** A server can advertise three
/// hundred tools; Vantage can call only the names on the allowlist, and
/// `callable` is what the backend actually permits. Drawing the whole
/// catalogue as if it were available would be the same class of lie as an
/// empty calendar for a failed read.
///
/// The endpoint and allowlist are editable here (`PUT /mcp/config`). A save
/// only writes configuration; the pill beside it is drawn from a live
/// handshake, and every save is followed by a fresh `probe=true` read so the
/// screen never implies a connection the save did not prove.
///
/// Nothing can be called from here. An MCP tool is an EXTERNAL write and goes
/// through the Approvals screen, where the confirmation workflow lives; this
/// screen is a truthful report plus configuration, not a second path around
/// the gate.
struct MCPView: View {

    @State private var status: APIModels.MCPStatus?
    @State private var isLoading = true
    @State private var errorMessage: String?
    @State private var showAllListed = false

    private let api = APIClient.shared

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: V.Space.section) {
                header
                if let errorMessage {
                    Notice(kind: .error, message: errorMessage, actionTitle: "Retry") {
                        Task { await load() }
                    }
                }
                connectionSection
                MCPConfigEditor(status: status, isBusy: isLoading) { fresh in
                    status = fresh
                    errorMessage = nil
                }
                if status?.connection.state == "connected" {
                    toolsSection
                }
                if let steps = status?.setup?.setup, !steps.isEmpty {
                    setupGuide(steps)
                }
                footnote
            }
            .padding(.horizontal, V.Space.gutter)
            .padding(.vertical, V.Space.section)
            .frame(maxWidth: 760)
            .frame(maxWidth: .infinity, alignment: .center)
        }
        .background(V.background)
        .navigationTitle("MCP")
        .task { await load() }
        .refreshable { await load() }
    }

    // MARK: - Header

    private var header: some View {
        PageHeader(
            title: "MCP",
            subtitle: "One remote tool server, and the tools Vantage may call."
        ) {
            Button {
                Task { await load() }
            } label: {
                Label("Refresh", systemImage: "arrow.clockwise")
            }
            .buttonStyle(VSecondaryButtonStyle())
            .disabled(isLoading)
        }
    }

    // MARK: - Connection

    private var connectionSection: some View {
        Card {
            VStack(alignment: .leading, spacing: V.Space.m) {
                HStack(alignment: .top, spacing: V.Space.m) {
                    Image(systemName: "puzzlepiece.extension")
                        .font(.system(size: 15, weight: .medium))
                        .foregroundStyle(V.accent)
                        .frame(width: 30, height: 30)
                        .background(V.accentSoft, in: RoundedRectangle(
                            cornerRadius: V.Radius.control, style: .continuous))
                    VStack(alignment: .leading, spacing: 2) {
                        Text("MCP server").font(V.Font.cardTitle).foregroundStyle(V.text)
                        Text("Configured below (or with JARVIS_MCP_URL). The pill is drawn "
                             + "from a live handshake, not from the saved setting.")
                            .font(V.Font.body)
                            .foregroundStyle(V.textSecondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Spacer(minLength: V.Space.m)
                }

                StatusPill(kind: connectionKind, text: connectionTitle)
                if let detail = status?.connection.detail, !detail.isEmpty {
                    Text(detail)
                        .font(V.Font.body)
                        .foregroundStyle(V.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                VStack(alignment: .leading, spacing: V.Space.xs) {
                    if let url = status?.connection.url ?? status?.setup?.url, !url.isEmpty {
                        // Already redacted by the backend; still not selectable
                        // for copy-paste into a chat, because it is an endpoint.
                        DetailRow(label: "Endpoint", value: url, monospaced: true)
                    }
                    if let name = status?.connection.server?.name, !name.isEmpty {
                        let version = status?.connection.server?.version ?? ""
                        DetailRow(label: "Server",
                                  value: version.isEmpty ? name : "\(name) \(version)")
                    }
                }
                .padding(.top, V.Space.xs)
            }
        }
    }

    private var connectionKind: StatusKind {
        guard let connection = status?.connection else {
            return isLoading ? .setup : .error
        }
        switch connection.state {
        case "connected": return .connected
        case "not_configured": return .pending
        case "error": return .error
        default: return .idle
        }
    }

    private var connectionTitle: String {
        guard let connection = status?.connection else {
            return isLoading ? "Checking…" : "Could not read the connection state"
        }
        switch connection.state {
        case "connected": return "Connected to the MCP server"
        case "not_configured": return "No MCP server is configured"
        case "disconnected": return "Configured but not connected"
        case "error": return "Could not reach the MCP server"
        default: return connection.state
        }
    }

    // MARK: - Tools

    private var toolsSection: some View {
        let listed = status?.connection.tools ?? []
        let callable = Set(status?.connection.callable ?? [])
        // The backend lists at most 50 names but reports the real total in
        // `tool_count`. Counting the array would understate a large server.
        let advertisedCount = status?.connection.tool_count ?? listed.count
        let shown = showAllListed ? listed : listed.filter { callable.contains($0.name) }

        return Card {
            VStack(alignment: .leading, spacing: V.Space.s) {
                SectionBlock(title: "Tools", trailing: toolsTrailing(listed: listed,
                                                                     callable: callable,
                                                                     advertised: advertisedCount)) {
                    VStack(alignment: .leading, spacing: V.Space.s) {
                        if listed.count < advertisedCount {
                            Text("The server advertises \(advertisedCount) tools; the first "
                                 + "\(listed.count) are listed here.")
                                .font(V.Font.caption)
                                .foregroundStyle(V.textTertiary)
                        }

                        if callable.isEmpty {
                            Notice(kind: .warning,
                                   message: "No tool is on the allowlist, so none can be called. "
                                    + "Add names in the editor above and save.")
                        } else if shown.isEmpty {
                            // Allowlisted names the server did not advertise: say so rather
                            // than drawing nothing, which reads as "fine".
                            Notice(kind: .warning,
                                   message: "The allowlist names tools this server did not "
                                    + "advertise. Check the spelling against the advertised list.")
                        } else {
                            VStack(alignment: .leading, spacing: 0) {
                                ForEach(shown) { tool in
                                    HStack(alignment: .top, spacing: V.Space.s) {
                                        Image(systemName: callable.contains(tool.name)
                                              ? "checkmark.seal.fill" : "circle.dashed")
                                            .foregroundStyle(callable.contains(tool.name)
                                                             ? V.accent : V.textTertiary)
                                            .font(.system(size: 11, weight: .semibold))
                                        VStack(alignment: .leading, spacing: 2) {
                                            Text(tool.name).font(V.Font.mono)
                                                .foregroundStyle(V.text)
                                            if let description = tool.description, !description.isEmpty {
                                                Text(description)
                                                    .font(V.Font.caption)
                                                    .foregroundStyle(V.textSecondary)
                                                    .fixedSize(horizontal: false, vertical: true)
                                            }
                                        }
                                        Spacer()
                                        if !callable.contains(tool.name) {
                                            StatusPill(kind: .idle, text: "not allowed")
                                        }
                                    }
                                    .padding(.vertical, V.Space.s)
                                    if tool.id != shown.last?.id {
                                        Divider().overlay(V.divider)
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    private func toolsTrailing(listed: [APIModels.MCPTool],
                               callable: Set<String>,
                               advertised: Int) -> AnyView {
        AnyView(
            HStack(spacing: V.Space.s) {
                Text("\(callable.count) callable · \(advertised) advertised")
                    .font(V.Font.caption)
                    .foregroundStyle(V.textSecondary)
                if listed.count > callable.count {
                    Toggle("Show advertised", isOn: $showAllListed)
                        .toggleStyle(.switch).controlSize(.mini)
                        .tint(V.accent)
                        .font(V.Font.caption)
                }
            })
    }

    // MARK: - Setup

    private func setupGuide(_ steps: [String]) -> some View {
        Card(tone: .gold) {
            VStack(alignment: .leading, spacing: V.Space.s) {
                Text("To connect a server").font(V.Font.cardTitle).foregroundStyle(V.text)
                ForEach(Array(steps.enumerated()), id: \.offset) { index, step in
                    HStack(alignment: .top, spacing: V.Space.s) {
                        Text("\(index + 1).")
                            .font(V.Font.caption.weight(.semibold))
                            .foregroundStyle(V.gold)
                            .frame(width: 14, alignment: .trailing)
                        Text(step)
                            .font(V.Font.body)
                            .foregroundStyle(V.textSecondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
        }
    }

    private var footnote: some View {
        Text("Every MCP tool is treated as an external write: calling one asks for "
             + "approval on the Approvals screen, and a phone cannot approve its own "
             + "request. Nothing is called from this screen.")
            .font(V.Font.caption)
            .foregroundStyle(V.textTertiary)
            .fixedSize(horizontal: false, vertical: true)
    }

    // MARK: - Data

    private func load() async {
        isLoading = true
        errorMessage = nil
        do { status = try await api.mcpStatus() }
        catch {
            errorMessage = error.localizedDescription
            // A stale "Connected" must not survive a status read that failed:
            // the screen would then claim a live handshake it could not
            // confirm this time.
            status = nil
        }
        isLoading = false
    }
}

// MARK: - Configuration editor

/// The real "Configure MCP server" editor, shared by the MCP screen and the
/// MCP card on Connections so the two can never disagree about what is saved.
///
/// The rules it follows:
/// * the server is the authority: a rejection is shown with its own `detail`,
///   never replaced by a generic sentence;
/// * a save writes configuration only. The handshake is re-read afterwards and
///   reported as what it is;
/// * the bearer token has no field. The API does not accept one, so offering a
///   box for it would only teach people to type a secret into a form that
///   cannot use it;
/// * an endpoint whose credential the backend masks (`[redacted]`) is never
///   written back masked: Save stays disabled until the field is retyped, or
///   Clear is used to remove it.
struct MCPConfigEditor: View {

    /// The last status the parent read. Used to pre-fill the fields once.
    let status: APIModels.MCPStatus?
    /// The parent is reading; nothing may be submitted over it.
    var isBusy = false
    /// Called with a fresh `probe=true` status after a save or a probe, so the
    /// parent's pill and tool list are updated from the same answer.
    var onStatus: (APIModels.MCPStatus) -> Void = { _ in }

    @State private var endpoint = ""
    @State private var allowedTools = ""
    @State private var serverName = ""
    @State private var prefilled = false
    @State private var savedEndpointRedacted = false
    @State private var maskedEndpoint = ""
    @State private var isSaving = false
    @State private var isProbing = false
    @State private var saveError: String?
    @State private var savedNote: String?
    @State private var probeNote: String?
    @State private var probeKind: Notice.Kind = .info
    @State private var confirmClear = false

    private let api = APIClient.shared
    /// `MASK` from the backend's redaction (`security.privacy`).
    private let mask = "[redacted]"

    /// The explanation shown when the saved URL came back masked. Broken out
    /// of `body` because the inline concatenation was too much for the type
    /// checker to resolve in reasonable time.
    private var maskedEndpointNote: String {
        "The saved endpoint carries a credential, so the backend reports it masked: "
            + "\(maskedEndpoint). Paste the full endpoint to replace it. Save stays "
            + "disabled while this field is empty, so a masked URL is never written "
            + "back; use Clear to remove the configuration."
    }

    var body: some View {
        Card {
            VStack(alignment: .leading, spacing: V.Space.m) {
                SectionBlock(title: "Configure MCP server") {
                    VStack(alignment: .leading, spacing: V.Space.m) {
                        VStack(alignment: .leading, spacing: V.Space.xs) {
                            VField(label: "Endpoint URL",
                                   placeholder: "https://mcp.example.com/mcp",
                                   text: $endpoint,
                                   monospaced: true)
                            if savedEndpointRedacted {
                                Text(maskedEndpointNote)
                                    .font(V.Font.caption)
                                    .foregroundStyle(V.gold)
                                    .fixedSize(horizontal: false, vertical: true)
                            } else if let hint = clientURLBlock {
                                Text(hint)
                                    .font(V.Font.caption)
                                    .foregroundStyle(V.danger)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }

                        VField(label: "Allowed tools (comma-separated)",
                               placeholder: "send_email, create_task",
                               text: $allowedTools,
                               monospaced: true)
                        VField(label: "Name (optional)",
                               placeholder: "mcp",
                               text: $serverName)

                        HStack(spacing: V.Space.s) {
                            Button {
                                Task { await save() }
                            } label: {
                                HStack(spacing: V.Space.xs) {
                                    if isSaving { ProgressView().controlSize(.small) }
                                    Text(isSaving ? "Saving…" : "Save")
                                }
                            }
                            .buttonStyle(VPrimaryButtonStyle())
                            .disabled(!canSave)

                            Button {
                                Task { await probe(capabilities: false) }
                            } label: {
                                HStack(spacing: V.Space.xs) {
                                    if isProbing { ProgressView().controlSize(.small) }
                                    Label("Test connection", systemImage: "bolt.horizontal.circle")
                                }
                            }
                            .buttonStyle(VSecondaryButtonStyle())
                            .disabled(busy)

                            Button {
                                Task { await probe(capabilities: true) }
                            } label: {
                                Label("Refresh capabilities",
                                      systemImage: "arrow.triangle.2.circlepath")
                            }
                            .buttonStyle(VSecondaryButtonStyle())
                            .disabled(busy)

                            Spacer(minLength: V.Space.s)

                            if configuredNow {
                                Button("Clear") { confirmClear = true }
                                    .buttonStyle(VQuietButtonStyle(tint: V.textSecondary))
                                    .disabled(busy)
                            }
                        }

                        if let saveError {
                            Notice(kind: .error, message: saveError)
                        }
                        if let savedNote {
                            Notice(kind: .success, message: savedNote, actionTitle: "Dismiss") {
                                self.savedNote = nil
                            }
                        }
                        if let probeNote {
                            Notice(kind: probeKind, message: probeNote, actionTitle: "Dismiss") {
                                self.probeNote = nil
                            }
                        }

                        Text("Token: stored in the Keychain / set via JARVIS_MCP_TOKEN — "
                             + "not editable here. The API does not accept a token; an endpoint "
                             + "that carries one in its query string is stored and reported masked.")
                            .font(V.Font.caption)
                            .foregroundStyle(V.textTertiary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
        }
        .onAppear { prefillIfNeeded() }
        // The parent's read lands after the first layout, so re-run when a
        // status arrives. `task(id:)` runs on appear and again whenever the id
        // changes, which covers both cases without the deprecated/14-only
        // `onChange` forms this package's macOS 13 target cannot use.
        .task(id: hasStatus) { prefillIfNeeded() }
        .vConfirm(
            "Clear the MCP configuration?",
            message: "The endpoint and allowlist saved in ~/.jarvis-forge/ai_selection.json are "
                + "removed. The token in the Keychain is not touched. Nothing can be called "
                + "until a server is configured again.",
            confirm: "Clear",
            isPresented: $confirmClear) {
                Task { await clear() }
            }
    }

    // MARK: Editor state

    private var hasStatus: Bool { status != nil }

    private var busy: Bool { isSaving || isProbing || isBusy }

    private var configuredNow: Bool {
        status?.connection.configured == true || status?.setup?.configured == true
    }

    private var canSave: Bool {
        guard !busy else { return false }
        // A masked URL must not be saved back as if it were the endpoint.
        if savedEndpointRedacted
            && endpoint.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return false
        }
        // Client-side guard for the one rule a person can see: http(s) only.
        // The server still decides; a 422 it returns is shown verbatim below.
        return clientURLBlock == nil
    }

    /// Non-nil when the typed endpoint cannot be http(s). Empty is allowed
    /// (it clears the endpoint, which the server also accepts).
    private var clientURLBlock: String? {
        let text = endpoint.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return nil }
        guard let components = URLComponents(string: text),
              let scheme = components.scheme?.lowercased(), !scheme.isEmpty else {
            return "An endpoint must begin with http:// or https://."
        }
        if scheme != "http" && scheme != "https" {
            return "Only http:// and https:// endpoints can be saved."
        }
        return nil
    }

    private func prefillIfNeeded() {
        guard !prefilled, let status else { return }
        prefilled = true
        let raw = (status.setup?.url ?? status.connection.url ?? "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        maskedEndpoint = raw
        savedEndpointRedacted = raw.contains(mask)
        endpoint = savedEndpointRedacted ? "" : raw
        let tools = status.setup?.allowed_tools ?? status.connection.allowed_tools ?? []
        allowedTools = tools.joined(separator: ", ")
        let name = status.setup?.name ?? status.connection.name ?? ""
        // "mcp" is the backend's default, not a name a person chose.
        serverName = name == "mcp" ? "" : name
    }

    // MARK: Actions

    private func save() async {
        guard canSave else { return }
        let url = endpoint.trimmingCharacters(in: .whitespacesAndNewlines)
        let tools = allowedTools.trimmingCharacters(in: .whitespacesAndNewlines)
        let name = serverName.trimmingCharacters(in: .whitespacesAndNewlines)

        isSaving = true
        saveError = nil
        savedNote = nil
        probeNote = nil
        defer { isSaving = false }

        do {
            let result = try await api.mcpConfigure(url: url, allowedTools: tools,
                                                    name: Self.nameToSave(name))
            // The parsed, de-duplicated list the server actually saved is the
            // authority, not the text that was typed.
            let allowed = result.allowed_tools ?? []
            allowedTools = allowed.joined(separator: ", ")
            savedEndpointRedacted = false
            maskedEndpoint = url
            if url.isEmpty {
                savedNote = "Saved. No MCP endpoint is configured, so no tool can be called."
            } else if allowed.isEmpty {
                savedNote = "Saved. The allowlist is empty, so no tool can be called."
            } else {
                savedNote = "Saved. Allowed tools: \(allowed.joined(separator: ", "))."
            }
            // A save writes configuration; it proves no connection. Re-read the
            // status with a probe so the pill reports a real handshake.
            await refreshStatus(failureLead: "Saved, but the status could not be refreshed")
        } catch {
            saveError = Self.serverMessage(error)
        }
    }

    private func clear() async {
        guard !busy else { return }
        isSaving = true
        saveError = nil
        savedNote = nil
        probeNote = nil
        defer { isSaving = false }
        do {
            _ = try await api.mcpConfigure(url: "", allowedTools: "",
                                           name: Self.nameToSave(""))
            endpoint = ""
            allowedTools = ""
            serverName = ""
            savedEndpointRedacted = false
            maskedEndpoint = ""
            savedNote = "Cleared. No MCP endpoint or allowlist is configured."
            await refreshStatus(failureLead: "Cleared, but the status could not be refreshed")
        } catch {
            saveError = Self.serverMessage(error)
        }
    }

    private func probe(capabilities: Bool) async {
        guard !busy else { return }
        isProbing = true
        saveError = nil
        probeNote = nil
        defer { isProbing = false }
        do {
            let fresh = try await api.mcpStatus(probe: true)
            onStatus(fresh)
            probeNote = report(for: fresh.connection, capabilities: capabilities)
            switch fresh.connection.state {
            case "connected": probeKind = .success
            case "not_configured": probeKind = .info
            default: probeKind = .warning
            }
        } catch {
            probeKind = .error
            probeNote = "The status check failed: \(error.localizedDescription)"
        }
    }

    private func refreshStatus(failureLead: String) async {
        do {
            let fresh = try await api.mcpStatus(probe: true)
            onStatus(fresh)
        } catch {
            probeKind = .warning
            probeNote = "\(failureLead): \(error.localizedDescription)"
        }
    }

    // MARK: Reporting

    private func report(for connection: APIModels.MCPConnection,
                        capabilities: Bool) -> String {
        let callable = connection.callable ?? []
        let listed = connection.tools ?? []
        // `tool_count` is the server's real total; `tools` is truncated.
        let advertised = connection.tool_count ?? listed.count
        switch connection.state {
        case "connected":
            let versioned = [connection.server?.name, connection.server?.version]
                .compactMap { $0 }
                .filter { !$0.isEmpty }
                .joined(separator: " ")
            let who = versioned.isEmpty ? (connection.name ?? "the server") : versioned
            guard capabilities else {
                return "Connected to \(who). \(advertised) tools advertised, "
                    + "\(callable.count) callable."
            }
            var parts = ["Connected to \(who).",
                         "\(advertised) advertised · \(callable.count) callable."]
            if listed.count < advertised {
                parts.append("Only the first \(listed.count) advertised names were listed.")
            }
            if callable.isEmpty {
                parts.append("No advertised tool is on the allowlist, so none can be called.")
            }
            let notAllowed = listed.map { $0.name }.filter { !callable.contains($0) }
            if !notAllowed.isEmpty {
                parts.append("Advertised but not on the allowlist: \(summarize(notAllowed)).")
            }
            return parts.joined(separator: " ")
        case "disconnected":
            let detail = connection.detail ?? "No detail was returned."
            return "Configured but not connected. \(detail)"
        case "not_configured":
            return connection.detail ?? "No MCP server is configured."
        default:
            return "\(Self.stateLabel(connection.state)): "
                + (connection.detail ?? "No detail was returned.")
        }
    }

    private func summarize(_ names: [String], limit: Int = 6) -> String {
        guard names.count > limit else { return names.joined(separator: ", ") }
        return names.prefix(limit).joined(separator: ", ")
            + " and \(names.count - limit) more"
    }

    private static func stateLabel(_ state: String) -> String {
        switch state {
        case "connected": return "Connected"
        case "not_configured": return "No server configured"
        case "disconnected": return "Not connected"
        case "error": return "Could not reach the server"
        default: return state
        }
    }

    /// The `name` value to send for a possibly-empty field.
    ///
    /// The route treats JSON `""` as "leave this field alone", so sending an
    /// empty name would keep a previously saved one while the field on screen
    /// shows nothing — the screen would disagree with the saved config. A
    /// whitespace string survives the route's `or None` and is stripped by
    /// `save_mcp_config`, which pops the key: the documented way to remove it.
    private static func nameToSave(_ trimmed: String) -> String {
        trimmed.isEmpty ? " " : trimmed
    }

    /// The server's own sentence when it rejected the request. `APIError`
    /// preserves the `detail` field on a 4xx/5xx; a generic "Server returned
    /// 422" would hide the one thing worth reading.
    private static func serverMessage(_ error: Error) -> String {
        if case APIError.httpError(_, let detail) = error, !detail.isEmpty {
            return detail
        }
        return error.localizedDescription
    }
}