import AppKit
import SwiftUI

/// Every external connection the Mac holds, on one page.
///
/// Google and MCP each had a screen of their own, which meant a person asking
/// "what is connected?" had to know to look in two places. This screen answers
/// that one question and nothing else: for each connection it shows the state
/// the backend reports, the backend's own detail sentence, and only the actions
/// the backend actually serves.
///
/// Rules this screen follows, the same ones the Google and MCP screens follow:
/// * a state is drawn from `/google/status` and `/mcp/status`, never assumed;
/// * a failed status read is shown as an error notice, never as "not connected";
/// * no Connect button appears for a provider the backend says is unconfigured
///   (the fix there is setup, so the setup steps are shown instead);
/// * MCP has no connect/disconnect route — its endpoint and allowlist are
///   edited in place (`PUT /mcp/config`), and every save is followed by a
///   fresh `probe=true` read so the pill never outruns the handshake.
struct ConnectionsView: View {

    @State private var google: APIModels.GoogleStatus?
    @State private var mcp: APIModels.MCPStatus?

    @State private var isLoading = true
    @State private var isRefreshing = false
    @State private var lastRefreshed: Date?
    @State private var isWorking = false

    /// Per-connection failures, kept apart so a Google failure never blanks the
    /// MCP card (or is reported as its reason).
    @State private var googleError: String?
    @State private var mcpError: String?

    /// What each Google service's last *real* read reported, and when.
    /// Nil means "not tested since this screen opened" — which is drawn as
    /// Not tested, never as healthy.
    @State private var calendarHealth: IntegrationHealth.State?
    @State private var tasksHealth: IntegrationHealth.State?
    @State private var calendarTestedAt: Date?
    @State private var tasksTestedAt: Date?
    @State private var testing: IntegrationHealth.Service?

    @State private var banner: String?
    @State private var confirmDisconnect = false
    /// Google setup card feedback. Separate from `banner` so a clipboard copy
    /// is never drawn as a connection success.
    @State private var copiedRedirect: String?
    @State private var copyNote: String?

    private let api = APIClient.shared

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: V.Space.section) {
                header

                if let banner {
                    Notice(kind: .success, message: banner, actionTitle: "Dismiss") {
                        self.banner = nil
                    }
                }

                googleSection
                mcpSection
                footnote
            }
            .padding(.horizontal, V.Space.gutter)
            .padding(.vertical, V.Space.section)
            .frame(maxWidth: 760)
            .frame(maxWidth: .infinity, alignment: .center)
        }
        .background(V.background)
        .navigationTitle("Connections")
        .task { await refresh() }
        .vConfirm(
            "Disconnect Google?",
            message: "Vantage will forget the Google token. Calendar and Tasks stop "
                + "being readable until you connect again.",
            confirm: "Disconnect",
            isPresented: $confirmDisconnect) {
                Task { await disconnectGoogle() }
            }
    }

    // MARK: - Header

    private var header: some View {
        PageHeader(
            title: "Connections",
            subtitle: "What Vantage is allowed to use."
        ) {
            HStack(spacing: V.Space.s) {
                if isRefreshing { ProgressView().controlSize(.small) }
                if let stamp = lastRefreshed, !isRefreshing {
                    Text("Updated \(relative(stamp))")
                        .font(V.Font.caption)
                        .foregroundStyle(V.textTertiary)
                }
                Button {
                    Task { await refresh() }
                } label: {
                    Label("Refresh", systemImage: "arrow.clockwise")
                }
                .buttonStyle(VSecondaryButtonStyle())
                .disabled(isLoading || isRefreshing || isWorking)
            }
        }
    }

    private func relative(_ date: Date) -> String {
        let formatter = RelativeDateTimeFormatter()
        formatter.unitsStyle = .abbreviated
        return formatter.localizedString(for: date, relativeTo: Date())
    }

    // MARK: - Google

    private var googleSection: some View {
        let connection = google?.connection
        let setup = google?.setup

        return VStack(alignment: .leading, spacing: V.Space.m) {
            googleCard(connection: connection)
            serviceCard(
                symbol: "calendar",
                name: "Google Calendar",
                capability: calendarCapability,
                state: calendarRowState,
                note: calendarDetail(setup),
                service: .calendar,
                health: calendarHealth,
                testedAt: calendarTestedAt)
            serviceCard(
                symbol: "checklist",
                name: "Google Tasks",
                capability: tasksCapability,
                state: tasksRowState,
                note: tasksDetail(setup),
                service: .tasks,
                health: tasksHealth,
                testedAt: tasksTestedAt)

            if setup?.configured == false {
                googleSetupCard(setup: setup)
            } else if let steps = setup?.setup, !steps.isEmpty {
                setupGuide(title: "To connect Google", steps: steps,
                           footer: setup?.redirect_uri)
            }
        }
    }

    private func googleCard(connection: APIModels.GoogleConnection?) -> some View {
        Card {
            VStack(alignment: .leading, spacing: V.Space.m) {
                HStack(alignment: .top, spacing: V.Space.m) {
                    identity(symbol: "globe",
                             name: "Google",
                             capability: "Calendar (read-only) and Tasks (read/write) on "
                                + "one account.")
                    Spacer(minLength: V.Space.m)
                    googleActions
                }

                if let error = googleError {
                    // A failed read is unknown, never "Not connected".
                    Notice(kind: .error,
                           message: "Could not read the Google connection. \(error)",
                           actionTitle: "Retry") {
                        Task { await readGoogle() }
                    }
                } else {
                    StatusPill(kind: googleRowState.statusKind,
                               text: googleRowState.label)
                    if let detail = connection?.detail, !detail.isEmpty {
                        Text(detail)
                            .font(V.Font.body)
                            .foregroundStyle(V.textSecondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    VStack(alignment: .leading, spacing: V.Space.xs) {
                        DetailRow(label: "Account",
                                  value: nonEmpty(connection?.account) ?? "—")
                        DetailRow(label: "Calendar", value: calendarCapability,
                                  tone: scopeProblem == nil ? nil : V.warning)
                        DetailRow(label: "Tasks", value: tasksCapability,
                                  tone: scopeProblem == nil ? nil : V.warning)
                        if let scopes = connection?.scopes, !scopes.isEmpty {
                            // "Scopes" while disconnected would read as granted;
                            // the backend sends the *requested* set in that state.
                            DetailRow(label: connection?.isConnected == true
                                        ? "Scopes" : "Requested scopes",
                                      value: scopes.map(shortScope).joined(separator: " · "),
                                      monospaced: true)
                        }
                    }
                    .padding(.top, V.Space.xs)
                    if let problem = scopeProblem {
                        Text(problem)
                            .font(V.Font.caption)
                            .foregroundStyle(V.warning)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
        }
    }

    /// Scopes are URLs in the payload; show the trailing capability only.
    private func shortScope(_ scope: String) -> String {
        if let tail = scope.split(separator: "/").last, !tail.isEmpty {
            return String(tail)
        }
        return scope
    }

    private func nonEmpty(_ value: String?) -> String? {
        guard let value, !value.isEmpty else { return nil }
        return value
    }

    private var googleRowState: RowState {
        guard google?.connection != nil else {
            if isLoading || isRefreshing { return .checking }
            return .unknown
        }
        if google?.connection.isConnected == true { return .connected }
        switch google?.connection.state {
        case "not_configured": return .needsSetup
        case "disconnected": return .notConnected
        case "expired": return .expired
        case "error": return .error
        default: return .unknown
        }
    }

    private func calendarDetail(_ setup: APIModels.GoogleSetup?) -> String {
        if setup?.calendar_enabled == false {
            return "Calendar is disabled in the backend settings. No read-only calendar "
                + "access is requested."
        }
        if googleRowState == .connected, let scopeNote = missingScope(Self.calendarScope) {
            return scopeNote
        }
        return "Same Google account as Tasks. Read-only access only."
    }

    private func tasksDetail(_ setup: APIModels.GoogleSetup?) -> String {
        if setup?.tasks_enabled == false {
            return "Tasks are disabled in the backend settings. No task access is requested."
        }
        if googleRowState == .connected, let scopeNote = missingScope(Self.tasksScope) {
            return scopeNote
        }
        return "Same Google account as Calendar. Read and write; Google stays the "
            + "source of truth."
    }

    // MARK: Google capabilities, from the real scopes

    private static let calendarScope = "calendar.readonly"
    private static let tasksScope = "/auth/tasks"

    private var grantedScopes: [String] {
        if let scopes = google?.connection.scopes, !scopes.isEmpty { return scopes }
        return google?.setup?.scopes ?? []
    }

    private func hasScope(_ fragment: String) -> Bool {
        grantedScopes.contains { $0.contains(fragment) }
    }

    private var calendarCapability: String {
        guard google?.connection.isConnected == true else {
            return "Read-only. Vantage reads events and never changes them."
        }
        return hasScope(Self.calendarScope)
            ? "Read-only — granted. Vantage reads events and never changes them."
            : "Not granted. The connected token has no calendar.readonly scope."
    }

    private var tasksCapability: String {
        guard google?.connection.isConnected == true else {
            return google?.setup?.tasks_enabled == false
                ? "Disabled in the backend settings."
                : "Read and write. Tasks can be added and completed here."
        }
        return hasScope(Self.tasksScope)
            ? "Read and write — granted. Tasks can be added and completed here; "
                + "Google stays the source of truth."
            : "Not granted. The connected token has no tasks scope."
    }

    /// The first scope problem worth naming, if any. Only checked when the
    /// backend reported a scope list; an older payload must not become a
    /// false alarm.
    private var scopeProblem: String? {
        if let note = missingScope(Self.calendarScope) { return note }
        return missingScope(Self.tasksScope)
    }

    /// A connected token whose granted scopes lack a service is a real problem
    /// and should be named. Only checked when the backend reported scopes; an
    /// older payload with no list must not be turned into a false alarm.
    private func missingScope(_ fragment: String) -> String? {
        guard let scopes = google?.connection.scopes, !scopes.isEmpty else { return nil }
        if scopes.contains(where: { $0.contains(fragment) }) { return nil }
        return "The connected token does not include the \(fragment) scope. "
            + "Disconnect and reconnect to grant it."
    }

    private var calendarRowState: RowState {
        if google?.setup?.calendar_enabled == false { return .needsSetup }
        if googleRowState == .connected, missingScope(Self.calendarScope) != nil { return .error }
        return googleRowState
    }

    private var tasksRowState: RowState {
        if google?.setup?.tasks_enabled == false { return .needsSetup }
        if googleRowState == .connected, missingScope(Self.tasksScope) != nil { return .error }
        return googleRowState
    }

    /// The actionable setup card for `setup.configured == false`: the
    /// backend's own steps, the exact redirect URI with a copy button, and a
    /// way to the console. The steps and URI are the server's, not invented.
    private func googleSetupCard(setup: APIModels.GoogleSetup?) -> some View {
        Card(tone: .gold) {
            VStack(alignment: .leading, spacing: V.Space.s) {
                Text("To connect Google").font(V.Font.cardTitle).foregroundStyle(V.text)
                Text("Vantage needs an OAuth client id from your own Google Cloud "
                     + "project. These are the backend's own steps, in its order.")
                    .font(V.Font.body)
                    .foregroundStyle(V.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)

                let steps = setup?.setup ?? []
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

                if let redirect = nonEmpty(setup?.redirect_uri) {
                    VStack(alignment: .leading, spacing: V.Space.xs) {
                        Text("Redirect URI (add it to the OAuth client)")
                            .font(V.Font.label)
                            .foregroundStyle(V.textSecondary)
                        HStack(alignment: .firstTextBaseline, spacing: V.Space.s) {
                            Text(redirect)
                                .font(V.Font.mono)
                                .foregroundStyle(V.text)
                                .textSelection(.enabled)
                                .fixedSize(horizontal: false, vertical: true)
                            Spacer(minLength: V.Space.s)
                            Button {
                                copyRedirectURI(redirect)
                            } label: {
                                Label(copiedRedirect == redirect ? "Copied" : "Copy redirect URI",
                                      systemImage: copiedRedirect == redirect
                                        ? "checkmark" : "doc.on.doc")
                            }
                            .buttonStyle(VQuietButtonStyle())
                            .help("Copy the redirect URI to the clipboard")
                        }
                    }
                    .padding(.top, V.Space.xs)
                }

                HStack(spacing: V.Space.s) {
                    Button {
                        openConsole()
                    } label: {
                        Label("Open Google Cloud console", systemImage: "arrow.up.right.square")
                    }
                    .buttonStyle(VSecondaryButtonStyle())
                    if let note = copyNote {
                        Text(note).font(V.Font.caption).foregroundStyle(V.textTertiary)
                    }
                }
                .padding(.top, V.Space.xs)
            }
        }
    }

    private func copyRedirectURI(_ value: String) {
        let board = NSPasteboard.general
        board.clearContents()
        board.setString(value, forType: .string)
        copiedRedirect = value
        copyNote = "Copied to the clipboard."
    }

    private func openConsole() {
        guard let url = URL(string: "https://console.cloud.google.com") else { return }
        if !NSWorkspace.shared.open(url) {
            copyNote = "Could not open a browser. Visit https://console.cloud.google.com"
        }
    }

    @ViewBuilder
    private var googleActions: some View {
        if let connection = google?.connection {
            HStack(spacing: V.Space.s) {
                if isWorking { ProgressView().controlSize(.small) }
                if connection.isConnected {
                    Button("Disconnect") { confirmDisconnect = true }
                        .buttonStyle(VDestructiveButtonStyle())
                        .disabled(isWorking)
                } else if google?.setup?.configured == true {
                    Button("Connect") { Task { await connectGoogle() } }
                        .buttonStyle(VPrimaryButtonStyle())
                        .disabled(isWorking)
                }
            }
        }
    }

    // MARK: - MCP

    private var mcpSection: some View {
        let connection = mcp?.connection
        let configured = connection?.configured == true || mcp?.setup?.configured == true

        return VStack(alignment: .leading, spacing: V.Space.m) {
            Card {
                VStack(alignment: .leading, spacing: V.Space.m) {
                    identity(symbol: "puzzlepiece.extension",
                             name: mcpName(configured: configured),
                             capability: "One remote MCP server. Only allowlisted tools may "
                                + "be called, and every call goes through Approvals.")

                    if let error = mcpError {
                        Notice(kind: .error,
                               message: "Could not read the MCP server state. \(error)",
                               actionTitle: "Retry") {
                            Task { await readMCP() }
                        }
                        // The editor stays below: a status read that failed is
                        // a reason to check the settings, not to hide them.
                    } else {
                        HStack(spacing: V.Space.s) {
                            StatusPill(kind: mcpRowState.statusKind, text: mcpRowState.label)
                            if let checksum = mcpCounts {
                                Text(checksum)
                                    .font(V.Font.caption)
                                    .foregroundStyle(V.textTertiary)
                            }
                            Spacer()
                        }
                        if let detail = connection?.detail, !detail.isEmpty {
                            Text(detail)
                                .font(V.Font.body)
                                .foregroundStyle(V.textSecondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        VStack(alignment: .leading, spacing: V.Space.xs) {
                            if let server = mcpSubtitle {
                                DetailRow(label: "Server", value: server)
                            }
                            if let url = connection?.url ?? mcp?.setup?.url, !url.isEmpty {
                                DetailRow(label: "Endpoint", value: url, monospaced: true)
                            }
                            if connection?.state == "connected" {
                                DetailRow(label: "Tools",
                                          value: "\(callableCount) callable · "
                                            + "\(advertisedCount) advertised")
                            }
                        }
                        .padding(.top, V.Space.xs)

                        if connection?.state == "connected" {
                            Text(mcpCallableNote)
                                .font(V.Font.body)
                                .foregroundStyle(V.textSecondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }
            }

            // The same editor the MCP screen shows: one implementation, so the
            // two screens cannot disagree about what is saved.
            MCPConfigEditor(status: mcp, isBusy: isRefreshing) { fresh in
                mcp = fresh
                mcpError = nil
                lastRefreshed = Date()
            }

            if let steps = mcp?.setup?.setup, !steps.isEmpty {
                setupGuide(title: "To connect an MCP server", steps: steps, footer: nil)
            }

            Text("Zapier and n8n connect through MCP. When one is configured, its "
                 + "name is shown above; there is no separate Zapier or n8n setting.")
                .font(V.Font.caption)
                .foregroundStyle(V.textTertiary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    /// Name the row after the server that is actually configured. Zapier and
    /// n8n are MCP servers, so the name comes from what the backend reports —
    /// never from a hard-coded guess. An unconfigured row is just "MCP server".
    private func mcpName(configured: Bool) -> String {
        guard configured else { return "MCP server" }
        let hay = [
            mcp?.setup?.name, mcp?.setup?.url,
            mcp?.connection.name, mcp?.connection.url,
            mcp?.connection.server?.name,
        ].compactMap { $0?.lowercased() }.joined(separator: " ")
        if hay.contains("zapier") { return "Zapier (via MCP)" }
        if hay.contains("n8n") { return "n8n (via MCP)" }
        if let name = mcp?.connection.server?.name ?? mcp?.setup?.name,
           !name.isEmpty, name != "mcp" {
            return "MCP server · \(name)"
        }
        return "MCP server"
    }

    private var mcpRowState: RowState {
        guard let state = mcp?.connection.state else {
            if isLoading || isRefreshing { return .checking }
            return .unknown
        }
        switch state {
        case "connected": return .connected
        case "not_configured": return .needsSetup
        case "disconnected": return .notConnected
        case "error": return .error
        default: return .unknown
        }
    }

    private var mcpSubtitle: String? {
        if let server = mcp?.connection.server, let name = server.name, !name.isEmpty {
            let version = server.version ?? ""
            return version.isEmpty ? name : "\(name) \(version)"
        }
        if let url = mcp?.connection.url, !url.isEmpty {
            return url
        }
        if let url = mcp?.setup?.url, !url.isEmpty {
            return url
        }
        return nil
    }

    private var callableCount: Int { mcp?.connection.callable?.count ?? 0 }

    /// Advertised vs callable, straight from the payload. `tool_count` is the
    /// server's real total; `tools` is truncated, so counting that array would
    /// understate the server.
    private var advertisedCount: Int {
        mcp?.connection.tool_count ?? mcp?.connection.tools?.count ?? 0
    }

    private var mcpCounts: String? {
        guard mcp?.connection.state == "connected" else { return nil }
        return "\(callableCount) callable · \(advertisedCount) advertised"
    }

    private var mcpCallableNote: String {
        if callableCount == 0 {
            return "The server advertises tools, but none is on JARVIS_MCP_ALLOWED_TOOLS, "
                + "so none can be called. Add names there and restart the backend."
        }
        return "Only allowlisted names can be called. Every MCP call is an external write "
            + "and goes through Approvals."
    }

    // MARK: - Shared pieces

    private enum RowState {
        case connected, notConnected, needsSetup, expired, error, checking, unknown

        var label: String {
            switch self {
            case .connected: return "Connected"
            case .notConnected: return "Not connected"
            case .needsSetup: return "Needs setup"
            case .expired: return "Expired"
            case .error: return "Error"
            case .checking: return "Checking…"
            case .unknown: return "Could not read"
            }
        }

        var statusKind: StatusKind {
            switch self {
            case .connected: return .connected
            case .notConnected: return .idle
            case .needsSetup: return .pending
            case .expired, .error, .unknown: return .error
            case .checking: return .setup
            }
        }
    }

    /// Icon + name + a one-line capability statement. Used by both cards so the
    /// two services read as peers.
    private func identity(symbol: String, name: String, capability: String) -> some View {
        HStack(alignment: .top, spacing: V.Space.m) {
            Image(systemName: symbol)
                .font(.system(size: 15, weight: .medium))
                .foregroundStyle(V.accent)
                .frame(width: 30, height: 30)
                .background(V.accentSoft, in: RoundedRectangle(
                    cornerRadius: V.Radius.control, style: .continuous))
            VStack(alignment: .leading, spacing: 2) {
                Text(name).font(V.Font.cardTitle).foregroundStyle(V.text)
                Text(capability)
                    .font(V.Font.body)
                    .foregroundStyle(V.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    /// One Calendar/Tasks row: its own pill, its own permission sentence, and its
    /// own real test.
    ///
    /// The card carries *two* pills on purpose. The first is the connection's
    /// verdict (a token exists); the second is this service's last real read.
    /// They can disagree, and when they do, the disagreement is the useful
    /// information — that is exactly the state this machine was in for two days.
    private func serviceCard(symbol: String, name: String, capability: String,
                             state: RowState, note: String,
                             service: IntegrationHealth.Service,
                             health: IntegrationHealth.State?,
                             testedAt: Date?) -> some View {
        Card(tone: .ivory) {
            VStack(alignment: .leading, spacing: V.Space.s) {
                HStack(alignment: .firstTextBaseline, spacing: V.Space.s) {
                    Image(systemName: symbol)
                        .font(.system(size: 12, weight: .medium))
                        .foregroundStyle(V.textSecondary)
                    Text(name).font(V.Font.cardTitle).foregroundStyle(V.text)
                    Spacer()
                    StatusPill(kind: state.statusKind, text: state.label)
                }
                Text(capability)
                    .font(V.Font.body)
                    .foregroundStyle(V.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
                if note != capability {
                    Text(note)
                        .font(V.Font.caption)
                        .foregroundStyle(state == .error ? V.danger : V.textTertiary)
                        .fixedSize(horizontal: false, vertical: true)
                }

                Divider().overlay(V.divider)

                testRow(service: service, health: health, testedAt: testedAt)
            }
        }
    }

    /// The real test for one service, and what it found.
    @ViewBuilder
    private func testRow(service: IntegrationHealth.Service,
                         health: IntegrationHealth.State?,
                         testedAt: Date?) -> some View {
        VStack(alignment: .leading, spacing: V.Space.xs) {
            HStack(alignment: .firstTextBaseline, spacing: V.Space.s) {
                if testing == service {
                    ProgressView().controlSize(.small)
                    Text("Reading \(service.title)…")
                        .font(V.Font.caption).foregroundStyle(V.textSecondary)
                } else if let health {
                    StatusPill(kind: healthPillKind(health), text: health.label)
                    if let testedAt {
                        Text("tested \(relative(testedAt))")
                            .font(V.Font.caption).foregroundStyle(V.textTertiary)
                    }
                } else {
                    // The honest default. A connection state is not a test.
                    StatusPill(kind: .setup, text: IntegrationHealth.State
                        .unknown("").label)
                    Text("Not tested on this screen yet")
                        .font(V.Font.caption).foregroundStyle(V.textTertiary)
                }
                Spacer(minLength: V.Space.s)
                Button("Test") { Task { await runTest(service) } }
                    .buttonStyle(VQuietButtonStyle())
                    .disabled(testing != nil || google?.connection.isConnected != true)
                    .help(google?.connection.isConnected == true
                          ? "Read \(service.title) now and report exactly what happened"
                          : "Connect Google first")
            }
            if let health, let guidance = health.guidance {
                Text(guidance)
                    .font(V.Font.caption)
                    .foregroundStyle(health.isHealthy ? V.textTertiary : V.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private func healthPillKind(_ health: IntegrationHealth.State) -> StatusKind {
        switch health {
        case .working, .workingEmpty: return .connected
        case .authRequired, .permissionDenied, .unavailable, .timedOut: return .error
        case .readOnly: return .pending
        case .unknown: return .setup
        }
    }

    /// Run one service's real read and keep its outcome for this screen.
    private func runTest(_ service: IntegrationHealth.Service) async {
        guard testing == nil else { return }
        testing = service
        defer { testing = nil }
        switch service {
        case .calendar:
            do {
                calendarHealth = IntegrationHealth.calendar(try await api.testGoogleCalendar())
            } catch {
                calendarHealth = IntegrationHealth.transport(error)
            }
            calendarTestedAt = Date()
        case .tasks:
            do {
                tasksHealth = IntegrationHealth.tasks(try await api.testGoogleTasks())
            } catch {
                tasksHealth = IntegrationHealth.transport(error)
            }
            tasksTestedAt = Date()
        }
    }

    private func setupGuide(title: String, steps: [String], footer: String?) -> some View {
        Card(tone: .gold) {
            VStack(alignment: .leading, spacing: V.Space.s) {
                Text(title).font(V.Font.cardTitle).foregroundStyle(V.text)
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
                if let footer, !footer.isEmpty {
                    Text("Redirect URI: \(footer)")
                        .font(V.Font.mono)
                        .foregroundStyle(V.textTertiary)
                        .textSelection(.enabled)
                }
            }
        }
    }

    private var footnote: some View {
        Text("These states are read from the backend each time this page loads; nothing "
             + "here is assumed. A status read that fails is shown as unknown, not as "
             + "\u{201C}not connected\u{201D}. Google's connect and disconnect actions ask the "
             + "backend; MCP's endpoint and allowlist are saved through it, and the pill "
             + "still comes from a live handshake.")
            .font(V.Font.caption)
            .foregroundStyle(V.textTertiary)
            .fixedSize(horizontal: false, vertical: true)
    }

    // MARK: - Actions

    /// Read both statuses. Each failure is recorded against its own section, so
    /// one unreachable service does not make the other look broken.
    private func refresh() async {
        if google == nil && mcp == nil {
            isLoading = true
        } else {
            isRefreshing = true
        }
        googleError = nil
        mcpError = nil
        async let g: Void = readGoogle()
        async let m: Void = readMCP()
        _ = await (g, m)
        isLoading = false
        isRefreshing = false
        // The stamp only moves when at least one read actually succeeded; a
        // failed refresh must not be labelled "Updated just now".
        if googleError == nil || mcpError == nil {
            lastRefreshed = Date()
        }
    }

    private func readGoogle() async {
        do {
            google = try await api.googleStatus()
            googleError = nil
        } catch {
            // A stale "Connected" must not survive a failed read: it is a claim
            // this load could not confirm.
            google = nil
            googleError = error.localizedDescription
        }
    }

    private func readMCP() async {
        do {
            mcp = try await api.mcpStatus()
            mcpError = nil
        } catch {
            mcp = nil
            mcpError = error.localizedDescription
        }
    }

    private func connectGoogle() async {
        isWorking = true
        googleError = nil
        defer { isWorking = false }
        do {
            let begin = try await api.googleConnectBegin()
            guard let url = URL(string: begin.url) else {
                googleError = "The backend returned an authorization URL that could not be opened."
                return
            }
            guard NSWorkspace.shared.open(url) else {
                googleError = "Could not open a browser for the Google sign-in page. "
                    + "Open this address yourself to finish connecting: \(begin.url)"
                return
            }
            // Connecting happens in the browser; this call only opened a page.
            // Nothing may say "Connected" until a status read reports it.
            banner = "The browser is open. Finish connecting there; Vantage shows Connected "
                + "once the callback reaches it — press Refresh if it does not."
        } catch {
            googleError = error.localizedDescription
        }
    }

    private func disconnectGoogle() async {
        isWorking = true
        googleError = nil
        defer { isWorking = false }
        do {
            let result = try await api.googleDisconnect()
            banner = result.detail ?? "Disconnected."
            // What the screen shows next is the re-read, not this reply.
            await readGoogle()
            lastRefreshed = Date()
        } catch {
            // A failed disconnect leaves the account connected; saying otherwise
            // would be a lie.
            googleError = error.localizedDescription
        }
    }
}