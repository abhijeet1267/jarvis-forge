import SwiftUI

/// The application operator — the native counterpart of `/operator`'s
/// Applications section.
///
/// Every row is a real check rather than a catalogue entry: an application that
/// is not installed offers no action at all, an installed one offers *Open*, and
/// one that is already running offers *Bring forward* instead. A launch that did
/// not happen is reported as a failure, because the underlying tool reports the
/// result of the request, not merely that a command was issued.
struct OperatorView: View {

    @State private var apps: [APIModels.AppStatus] = []
    @State private var isLoading = true
    @State private var errorMessage: String?
    @State private var note = ""
    @State private var busy: String?
    @State private var messages: [APIModels.InboxMessage] = []
    @State private var searched = false
    @State private var openMessage: APIModels.EmailBody?

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
                    VStack(alignment: .leading, spacing: 18) {
                        applications
                        inboxSection
                        Text("Vantage asks the application to come forward and then reads back which one is in front, so a switch that did not take effect is reported as unverified rather than as done.")
                            .font(.caption2).foregroundStyle(V.textSecondary)
                    }
                    .padding(24)
                }
            }
        }
        .task { await load() }
    }

    private var header: some View {
        PageHeader(title: "Operator", subtitle: "Applications Vantage may open, and your inbox.") {
            HStack(spacing: V.Space.s) {
                if !note.isEmpty {
                    Text(note).font(V.Font.caption).foregroundStyle(V.textTertiary)
                }
                Button { Task { await load() } } label: {
                    Label("Refresh", systemImage: "arrow.clockwise")
                }
                .buttonStyle(VSecondaryButtonStyle())
            }
        }
        .padding(.horizontal, V.Space.gutter)
        .padding(.top, V.Space.section)
        .padding(.bottom, V.Space.l)
    }

    private var applications: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Applications").font(V.Font.caption.weight(.semibold)).tracking(0.6).foregroundStyle(V.textTertiary).textCase(.uppercase)
            Text("\(installed) of \(apps.count) known applications are installed here.")
                .font(.caption2).foregroundStyle(V.textSecondary)
            ForEach(apps) { app in
                HStack(alignment: .center) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(app.name)
                            .foregroundStyle(app.installed ? .primary : .secondary)
                        Text(app.category ?? "")
                            .font(.caption2).foregroundStyle(V.textSecondary)
                    }
                    Spacer()
                    Text(app.installed ? (app.running ? "running" : "not running")
                                       : "not installed")
                        .font(.caption2)
                        .foregroundStyle(app.installed ? .secondary : .tertiary)
                    if app.installed {
                        Button(app.running ? "Bring forward" : "Open") {
                            Task { await act(app) }
                        }
                        .buttonStyle(.borderless)
                        .disabled(busy == app.key)
                    }
                }
                .padding(.vertical, 3)
                Divider()
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// The mailbox Vantage can already search and read, which the native app
    /// could not show at all — the web operator has listed messages since the
    /// email work and this screen only ever showed applications.
    private var inboxSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Inbox").font(V.Font.caption.weight(.semibold)).tracking(0.6).foregroundStyle(V.textTertiary).textCase(.uppercase)
            Button("Load recent messages") { Task { await loadInbox() } }
                .buttonStyle(.borderless)
            if searched && messages.isEmpty {
                Text("No messages matched.").font(.caption2)
                    .foregroundStyle(V.textSecondary)
            }
            ForEach(messages) { m in
                VStack(alignment: .leading, spacing: 2) {
                    Text(m.subject ?? "(no subject)").font(.caption)
                    Text((m.from ?? "") + (m.date.map { " · \($0)" } ?? ""))
                        .font(.caption2).foregroundStyle(V.textSecondary)
                }
                .contentShape(Rectangle())
                .onTapGesture { Task { await openMail(m) } }
                .padding(.vertical, 2)
            }
            if let body = openMessage {
                Text(body.subject ?? "(no subject)").font(.caption).bold()
                Text(body.from ?? "").font(.caption2).foregroundStyle(V.textSecondary)
                Text((body.body ?? "").prefix(1200))
                    .font(.caption2)
                    .textSelection(.enabled)
                    .padding(8)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(RoundedRectangle(cornerRadius: V.Radius.control)
                        .fill(V.fill))
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func loadInbox() async {
        do {
            messages = try await api.inbox()
            searched = true
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func openMail(_ message: APIModels.InboxMessage) async {
        do { openMessage = try await api.emailBody(message.uid) }
        catch { errorMessage = error.localizedDescription }
    }

    private var installed: Int { apps.filter(\.installed).count }

    // MARK: - Actions

    private func load() async {
        isLoading = true; errorMessage = nil
        do {
            apps = try await api.appStatus()
        } catch { errorMessage = error.localizedDescription }
        isLoading = false
    }

    private func act(_ app: APIModels.AppStatus) async {
        busy = app.key
        note = app.running ? "Bringing \(app.name) forward…" : "Opening \(app.name)…"
        do {
            if app.running { try await api.focusApp(app.name) } else { try await api.openApp(app.name) }
            await load()
            note = "Asked \(app.name)."
        } catch {
            errorMessage = error.localizedDescription
            note = ""
        }
        busy = nil
    }
}