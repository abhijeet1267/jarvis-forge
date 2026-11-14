import AppKit
import SwiftUI

/// Google Calendar and Google Tasks: connection, and the data itself.
///
/// The screen is built around one rule the rest of this app learned the hard
/// way: **a failed read is not an empty result.** The backend sends a `status`
/// with every response, and this view branches on it — `has_results` and
/// `real_empty` are the only two that may be drawn as a calendar or a task
/// list. Everything else is drawn as the failure it is, with the reason
/// underneath, because an unreachable Google that renders as "nothing
/// scheduled" is worse than an error.
///
/// Nothing here is mocked. When no account is connected the screen shows the
/// setup steps the backend reports, not sample events.
struct GoogleView: View {

    @State private var status: APIModels.GoogleStatus?
    @State private var events: APIModels.GoogleEventsResponse?
    @State private var tasks: APIModels.GoogleTasksResponse?
    @State private var lists: [APIModels.GoogleTaskList] = []

    @State private var isLoading = true
    @State private var isWorking = false
    @State private var errorMessage: String?
    /// A section's own read failure, kept apart from `errorMessage` so a
    /// calendar failure is never shown as the task list's reason (or vice
    /// versa), and neither disappears when the other section retries.
    @State private var eventsError: String?
    @State private var tasksError: String?
    @State private var banner: String?

    @State private var newTask = ""
    @State private var newTaskDue = ""
    @State private var isAddingTask = false
    @State private var showCompleted = false
    @State private var confirmDisconnect = false
    /// The setup card's copy feedback. Kept apart from the connection banner so
    /// a clipboard write never reads as "connected".
    @State private var copiedRedirect: String?
    @State private var copyNote: String?
    /// The task whose deletion is being confirmed. Deletion is permanent, so it
    /// takes an explicit second act rather than an undo-less click.
    @State private var taskPendingDelete: APIModels.GoogleTask?

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
                if let banner {
                    Notice(kind: .success, message: banner, actionTitle: "Dismiss") {
                        self.banner = nil
                    }
                }
                connectionSection
                if status?.connection.isConnected == true {
                    calendarSection
                    tasksSection
                }
                footnote
            }
            .padding(.horizontal, V.Space.gutter)
            .padding(.vertical, V.Space.section)
            .frame(maxWidth: 760)
            .frame(maxWidth: .infinity, alignment: .center)
        }
        .background(V.background)
        .navigationTitle("Google")
        .task { await load() }
        .refreshable { await load() }
        .vConfirm(
            "Delete this Google task?",
            message: "“\(taskPendingDelete?.title ?? "")” will be removed from Google Tasks. "
                + "This cannot be undone. Completing it instead keeps the record.",
            confirm: "Delete",
            isPresented: Binding(get: { taskPendingDelete != nil },
                                 set: { if !$0 { taskPendingDelete = nil } })) {
                Task { await deletePendingTask() }
            }
        .vConfirm(
            "Disconnect Google?",
            message: "Vantage will forget the Google token. Calendar and Tasks stop "
                + "being readable until you connect again.",
            confirm: "Disconnect",
            isPresented: $confirmDisconnect) {
                Task { await disconnect() }
            }
    }

    // MARK: - Header

    private var header: some View {
        PageHeader(
            title: "Google",
            subtitle: "Calendar, read-only, and Tasks, read and write."
        ) {
            HStack(spacing: V.Space.s) {
                if isWorking { ProgressView().controlSize(.small) }
                Button {
                    Task { await load() }
                } label: {
                    Label("Refresh", systemImage: "arrow.clockwise")
                }
                .buttonStyle(VSecondaryButtonStyle())
                .disabled(isLoading || isWorking)
            }
        }
    }

    // MARK: - Connection

    private var connectionSection: some View {
        VStack(alignment: .leading, spacing: V.Space.m) {
            Card {
                VStack(alignment: .leading, spacing: V.Space.m) {
                    HStack(alignment: .top, spacing: V.Space.m) {
                        identity()
                        Spacer(minLength: V.Space.m)
                        connectionActions
                    }
                    StatusPill(kind: connectionKind, text: connectionTitle)
                    if let detail = status?.connection.detail, !detail.isEmpty {
                        Text(detail)
                            .font(V.Font.body)
                            .foregroundStyle(V.textSecondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    VStack(alignment: .leading, spacing: V.Space.xs) {
                        DetailRow(label: "Account",
                                  value: optional(status?.connection.account) ?? "—")
                        // Derived from the granted `scopes`, never a constant:
                        // a token without the calendar scope must not be
                        // described as if calendar access were granted.
                        DetailRow(label: "Calendar", value: calendarCapability,
                                  tone: scopeNote(Self.calendarScope) == nil ? nil : V.warning)
                        DetailRow(label: "Tasks", value: tasksCapability,
                                  tone: scopeNote(Self.tasksScope) == nil ? nil : V.warning)
                        if let granted = status?.connection.scopes, !granted.isEmpty {
                            // "Scopes" while disconnected would read as granted;
                            // the backend sends the *requested* set in that state.
                            DetailRow(label: status?.connection.isConnected == true
                                        ? "Scopes" : "Requested scopes",
                                      value: granted.map(shortScope).joined(separator: " · "),
                                      monospaced: true)
                        }
                    }
                    .padding(.top, V.Space.xs)

                    VStack(alignment: .leading, spacing: V.Space.xxs) {
                        Text(capabilityLine)
                            .font(V.Font.caption.weight(.medium))
                            .foregroundStyle(V.textSecondary)
                        if let problem = scopeProblem {
                            Text(problem)
                                .font(V.Font.caption)
                                .foregroundStyle(V.warning)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }
            }

            if status?.setup?.configured == false {
                setupSection
            } else if let steps = status?.setup?.setup, !steps.isEmpty {
                setupGuide(steps)
            }
        }
    }

    private func identity() -> some View {
        HStack(alignment: .top, spacing: V.Space.m) {
            Image(systemName: "globe")
                .font(.system(size: 15, weight: .medium))
                .foregroundStyle(V.accent)
                .frame(width: 30, height: 30)
                .background(V.accentSoft, in: RoundedRectangle(
                    cornerRadius: V.Radius.control, style: .continuous))
            VStack(alignment: .leading, spacing: 2) {
                Text("Google").font(V.Font.cardTitle).foregroundStyle(V.text)
                Text("One account serves both services below.")
                    .font(V.Font.body)
                    .foregroundStyle(V.textSecondary)
            }
        }
    }

    private func optional(_ value: String?) -> String? {
        guard let value, !value.isEmpty else { return nil }
        return value
    }

    private var connectionKind: StatusKind {
        guard let connection = status?.connection else {
            // A read that has not succeeded is an error, not "not connected".
            return isLoading ? .setup : .error
        }
        if connection.isConnected { return .connected }
        switch connection.state {
        case "not_configured": return .pending
        case "expired", "error": return .error
        default: return .idle
        }
    }

    private var connectionTitle: String {
        // "Checking…" is only true while a read is in flight. Once it failed,
        // saying "Checking…" forever is the same defect this project keeps
        // fixing elsewhere: a placeholder that doubles as the unknown state.
        guard let connection = status?.connection else {
            return isLoading ? "Checking…" : "Could not read the connection state"
        }
        switch connection.state {
        case "connected": return "Connected to Google"
        case "not_configured":
            // The backend distinguishes "never configured" from "authorised
            // earlier, but no client id now". The pill says which, and the
            // server's own `detail` sentence underneath says why.
            return connection.has_token == true
                ? "Not configured — an earlier authorisation can no longer be used"
                : "Google is not set up"
        case "disconnected": return "Not connected"
        case "expired": return "The connection expired"
        case "error": return "Could not reach Google"
        default: return connection.state
        }
    }

    @ViewBuilder
    private var connectionActions: some View {
        if status?.connection.isConnected == true {
            Button("Disconnect") { confirmDisconnect = true }
                .buttonStyle(VDestructiveButtonStyle())
                .disabled(isWorking)
        } else if status?.setup?.configured == true {
            Button("Connect") { Task { await connect() } }
                .buttonStyle(VPrimaryButtonStyle())
                .disabled(isWorking)
        }
    }

    /// What is missing, in the order it must be done. Only shown when the
    /// backend says Google is unconfigured — it is the fix, not decoration.
    private func setupGuide(_ steps: [String]) -> some View {
        Card(tone: .gold) {
            VStack(alignment: .leading, spacing: V.Space.s) {
                Text("To connect Google").font(V.Font.cardTitle).foregroundStyle(V.text)
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
                if let redirect = status?.setup?.redirect_uri, !redirect.isEmpty {
                    Text("Redirect URI: \(redirect)")
                        .font(V.Font.mono)
                        .foregroundStyle(V.textTertiary)
                        .textSelection(.enabled)
                }
            }
        }
    }

    /// The actionable setup card, drawn when `setup.configured == false`: the
    /// backend's own numbered steps, the exact redirect URI with a copy
    /// button, and a way to the Google Cloud console. Nothing here is a
    /// guess — the steps and the URI come from `describe_google()`.
    private var setupSection: some View {
        Card(tone: .gold) {
            VStack(alignment: .leading, spacing: V.Space.s) {
                Text("To connect Google").font(V.Font.cardTitle).foregroundStyle(V.text)
                Text("Vantage needs an OAuth client id from your own Google Cloud project. "
                     + "These are the backend's own steps, in the order it requires them.")
                    .font(V.Font.body)
                    .foregroundStyle(V.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)

                let steps = status?.setup?.setup ?? []
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

                if let redirect = optional(status?.setup?.redirect_uri) {
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

    /// Calendar and Tasks capability sentences, derived from the scopes the
    /// token was actually granted. Falls back to the requested scopes from
    /// `setup` only when nothing has been granted yet.
    private static let calendarScope = "calendar.readonly"
    private static let tasksScope = "/auth/tasks"

    private var grantedScopes: [String] {
        if let scopes = status?.connection.scopes, !scopes.isEmpty { return scopes }
        return status?.setup?.scopes ?? []
    }

    private func hasScope(_ fragment: String) -> Bool {
        grantedScopes.contains { $0.contains(fragment) }
    }

    /// Non-nil when the connected token is missing a scope the backend intends
    /// to use. Only meaningful while connected and only when a scope list was
    /// sent; an older payload with no list is not a false alarm.
    private func scopeNote(_ fragment: String) -> String? {
        guard status?.connection.isConnected == true else { return nil }
        guard let scopes = status?.connection.scopes, !scopes.isEmpty else { return nil }
        if scopes.contains(where: { $0.contains(fragment) }) { return nil }
        return "The connected token does not include the \(fragment) scope. "
            + "Disconnect and reconnect to grant it."
    }

    private var calendarCapability: String {
        guard status?.connection.isConnected == true else { return "Read-only (not granted yet)" }
        return hasScope(Self.calendarScope) ? "Read-only" : "Not granted"
    }

    private var tasksCapability: String {
        guard status?.connection.isConnected == true else {
            return status?.setup?.tasks_enabled == false
                ? "Disabled in settings" : "Read & write (not granted yet)"
        }
        return hasScope(Self.tasksScope) ? "Read & write" : "Not granted"
    }

    private var capabilityLine: String {
        guard status?.connection.isConnected == true else {
            return "Calendar is read-only; Tasks can be created and completed once connected."
        }
        // Read straight from the granted scopes, never a constant: a token
        // without calendar.readonly must not claim calendar access.
        var parts: [String] = []
        parts.append(hasScope(Self.calendarScope)
                     ? "Calendar is read-only"
                     : "Calendar access was not granted")
        parts.append(hasScope(Self.tasksScope)
                     ? "Tasks can be created and completed"
                     : "Tasks access was not granted")
        return parts.joined(separator: "; ") + "."
    }

    private var scopeProblem: String? {
        if let note = scopeNote(Self.calendarScope) { return note }
        return scopeNote(Self.tasksScope)
    }

    private func shortScope(_ scope: String) -> String {
        if let tail = scope.split(separator: "/").last, !tail.isEmpty { return String(tail) }
        return scope
    }

    private func copyRedirectURI(_ value: String) {
        let board = NSPasteboard.general
        board.clearContents()
        board.setString(value, forType: .string)
        copiedRedirect = value
        copyNote = "Copied to the clipboard."
    }

    /// The console link the backend's own first step names. `Link` would be
    /// the SwiftUI spelling, but this app has no default browser guarantee in
    /// an offscreen harness; `NSWorkspace` is what Connect already uses, so
    /// both paths fail the same way (reported, not silent).
    private func openConsole() {
        guard let url = URL(string: "https://console.cloud.google.com") else { return }
        if !NSWorkspace.shared.open(url) {
            copyNote = "Could not open a browser. Visit https://console.cloud.google.com"
        }
    }

    // MARK: - Calendar

    private var calendarSection: some View {
        Card {
            VStack(alignment: .leading, spacing: V.Space.s) {
                SectionBlock(title: "Today", trailing: calendarTrailing) {
                    calendarBody
                }
            }
        }
    }

    private var calendarTrailing: AnyView? {
        guard events?.read_succeeded == false || (events == nil && eventsError != nil) else {
            return nil
        }
        return AnyView(
            Button("Retry") { Task { await loadEvents() } }
                .buttonStyle(VQuietButtonStyle()))
    }

    @ViewBuilder
    private var calendarBody: some View {
        if isLoading && events == nil {
            LoadingRow(text: "Reading your calendar…")
        } else if let events {
            if events.read_succeeded != true {
                // Not an empty day. The status says what went wrong.
                readFailure(events.detail ?? "Google Calendar could not be read.",
                            status: events.status)
            } else if let items = events.events, !items.isEmpty {
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(items) { event in
                        eventRow(event)
                        if event.id != items.last?.id {
                            Divider().overlay(V.divider)
                        }
                    }
                }
            } else {
                Text("Nothing scheduled today.")
                    .font(V.Font.body).foregroundStyle(V.textSecondary)
            }
        } else if let eventsError {
            // The request itself failed (transport, 4xx, malformed body).
            // Saying nothing here would draw an empty section, which reads as
            // "nothing scheduled" -- the defect this screen exists to avoid.
            readFailure(eventsError, status: nil)
        }
    }

    private func eventRow(_ event: APIModels.GoogleEvent) -> some View {
        HStack(alignment: .top, spacing: V.Space.m) {
            Text(event.all_day == true ? "all day" : timeOf(event.start))
                .font(V.Font.mono.monospacedDigit())
                .foregroundStyle(V.textSecondary)
                .frame(width: 60, alignment: .leading)
            VStack(alignment: .leading, spacing: 2) {
                Text(event.title).font(V.Font.body).foregroundStyle(V.text)
                if let location = event.location, !location.isEmpty {
                    Text(location).font(V.Font.caption).foregroundStyle(V.textSecondary)
                }
            }
            Spacer()
            if let minutes = event.duration_minutes, minutes > 0, event.all_day != true {
                Text("\(minutes)m").font(V.Font.caption).foregroundStyle(V.textTertiary)
            }
        }
        .padding(.vertical, V.Space.s)
    }

    // MARK: - Tasks

    private var tasksSection: some View {
        Card {
            VStack(alignment: .leading, spacing: V.Space.s) {
                SectionBlock(title: "Tasks", trailing: tasksTrailing) {
                    VStack(alignment: .leading, spacing: V.Space.s) {
                        if isAddingTask {
                            HStack(spacing: V.Space.s) {
                                VField(placeholder: "What needs doing?", text: $newTask,
                                       onSubmit: { Task { await createTask() } })
                                VField(placeholder: "due (optional, 2026-11-01)",
                                       text: $newTaskDue)
                                    .frame(width: 210)
                                Button("Add") { Task { await createTask() } }
                                    .buttonStyle(VPrimaryButtonStyle())
                                    .disabled(newTask.trimmingCharacters(in: .whitespaces).isEmpty
                                              || isWorking)
                                Button("Cancel") {
                                    isAddingTask = false; newTask = ""; newTaskDue = ""
                                }
                                .buttonStyle(VQuietButtonStyle(tint: V.textSecondary))
                            }
                        } else {
                            Button { isAddingTask = true } label: {
                                Label("Add a task", systemImage: "plus")
                            }
                            .buttonStyle(VQuietButtonStyle())
                        }

                        tasksBody
                    }
                }
            }
        }
    }

    private var tasksTrailing: AnyView {
        AnyView(
            HStack(spacing: V.Space.s) {
                Toggle("Show completed", isOn: $showCompleted)
                    .toggleStyle(.switch)
                    .controlSize(.mini)
                    .tint(V.accent)
                    .font(V.Font.caption)
                    .onChange(of: showCompleted) { _ in Task { await loadTasks() } }
                if tasks?.read_succeeded == false || (tasks == nil && tasksError != nil) {
                    Button("Retry") { Task { await loadTasks() } }
                        .buttonStyle(VQuietButtonStyle())
                }
            })
    }

    @ViewBuilder
    private var tasksBody: some View {
        if isLoading && tasks == nil {
            LoadingRow(text: "Reading Google Tasks…")
        } else if let tasks {
            if tasks.read_succeeded != true {
                readFailure(tasks.detail ?? "Google Tasks could not be read.",
                            status: tasks.status)
            } else if let items = tasks.tasks, !visibleTasks(items).isEmpty {
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(visibleTasks(items)) { task in
                        taskRow(task)
                        if task.id != visibleTasks(items).last?.id {
                            Divider().overlay(V.divider)
                        }
                    }
                }
            } else {
                Text(showCompleted
                     ? "No tasks in this list."
                     : "No open tasks. Turn on Completed to see finished ones.")
                    .font(V.Font.body).foregroundStyle(V.textSecondary)
            }
        } else if let tasksError {
            // The request itself failed. An empty section here would read as
            // "no tasks", which is not what happened.
            readFailure(tasksError, status: nil)
        }
    }

    /// What the toggle says should be visible.
    ///
    /// The backend already filters completed tasks when asked to, so this is
    /// normally a no-op. It exists because the screen must not contradict its
    /// own control: if a completed task arrives while the toggle is off, the
    /// toggle -- not the data -- decides. (Google, like most APIs, is not
    /// guaranteed to honour a filter exactly.)
    private func visibleTasks(_ items: [APIModels.GoogleTask]) -> [APIModels.GoogleTask] {
        showCompleted ? items : items.filter { $0.done != true }
    }

    private func taskRow(_ task: APIModels.GoogleTask) -> some View {
        HStack(alignment: .top, spacing: V.Space.m) {
            Button {
                guard task.done != true else { return }
                Task { await complete(task) }
            } label: {
                Image(systemName: task.done == true
                      ? "checkmark.circle.fill" : "circle")
                    .foregroundStyle(task.done == true ? V.accent : V.textTertiary)
            }
            .buttonStyle(.plain)
            .disabled(task.done == true || isWorking)
            .help(task.done == true ? "Already completed" : "Mark completed")

            VStack(alignment: .leading, spacing: 2) {
                Text(task.title)
                    .font(V.Font.body)
                    .strikethrough(task.done == true)
                    .foregroundStyle(task.done == true ? V.textTertiary : V.text)
                if let notes = task.notes, !notes.isEmpty {
                    Text(notes).font(V.Font.caption).foregroundStyle(V.textSecondary)
                        .lineLimit(2)
                }
                if let due = task.due {
                    // Google Tasks stores a date, not a time; show it as one.
                    Text("due \(due)").font(V.Font.caption).foregroundStyle(V.textTertiary)
                }
            }
            Spacer()
            if task.done != true {
                Button {
                    taskPendingDelete = task
                } label: {
                    Image(systemName: "trash")
                        .font(.system(size: 11))
                }
                .buttonStyle(.plain)
                .foregroundStyle(V.textTertiary)
                .help("Delete this task from Google Tasks")
                .accessibilityLabel("Delete task")
                .disabled(isWorking)
            }
        }
        .padding(.vertical, V.Space.s)
    }

    // MARK: - Shared failure row

    /// One place that draws "this read failed", so no section can invent its
    /// own softer wording — or forget to say anything at all.
    private func readFailure(_ detail: String, status: String?) -> some View {
        VStack(alignment: .leading, spacing: V.Space.xs) {
            Notice(kind: .error, message: detail)
            if let status, !status.isEmpty {
                Text(status).font(V.Font.mono).foregroundStyle(V.textTertiary)
            }
        }
    }

    private var footnote: some View {
        Text("Google is the source of truth for what it holds. Vantage reads it and "
             + "does not copy events or tasks into its own database, so the same item "
             + "never becomes two reminders. Calendar editing is not offered: the "
             + "connection is read-only. The capability lines above are read from the "
             + "scopes the token actually holds.")
            .font(V.Font.caption)
            .foregroundStyle(V.textTertiary)
            .fixedSize(horizontal: false, vertical: true)
    }

    // MARK: - Actions

    private func load() async {
        isLoading = true
        errorMessage = nil
        eventsError = nil
        tasksError = nil
        do {
            status = try await api.googleStatus()
        } catch {
            // A status read that failed must not leave the previous state on
            // screen: an old "Connected" drawn next to an error banner is a
            // claim this refresh could not confirm.
            errorMessage = error.localizedDescription
            status = nil
            events = nil
            tasks = nil
        }
        if status?.connection.isConnected == true {
            async let e: () = loadEvents()
            async let t: () = loadTasks()
            _ = await (e, t)
        }
        isLoading = false
    }

    private func loadEvents() async {
        eventsError = nil
        do { events = try await api.googleEvents() }
        catch {
            // Clearing, not keeping, the previous answer: an old calendar
            // drawn beside "could not refresh" reads as today's schedule.
            events = nil
            eventsError = error.localizedDescription
        }
    }

    private func loadTasks() async {
        tasksError = nil
        do { tasks = try await api.googleTasks(showCompleted: showCompleted) }
        catch {
            tasks = nil
            tasksError = error.localizedDescription
        }
    }

    private func connect() async {
        isWorking = true
        errorMessage = nil
        defer { isWorking = false }
        do {
            let begin = try await api.googleConnectBegin()
            guard let url = URL(string: begin.url) else {
                errorMessage = "The backend returned an authorization URL that could not be opened."
                return
            }
            // The authorization happens in the browser; the callback lands on
            // the local backend, which stores the token. This call only asked for
            // a URL -- nothing is connected yet, so nothing here may say it is.
            // The banner says exactly that, and the button stays "Connect"
            // until a status read reports `connected`.
            //
            // `open` returns false when the URL cannot be handed to anything.
            // It used to be ignored, so a Mac with no browser (or a blocked
            // handler) was told "The browser is open" about a window that never
            // appeared, and the person was left waiting for a callback that
            // could not come.
            guard NSWorkspace.shared.open(url) else {
                // The URL is still valid, so offering it as copyable text is a
                // real recovery rather than a dead end.
                errorMessage = "Could not open a browser for the Google sign-in page. "
                    + "Open this address yourself to finish connecting: \(begin.url)"
                return
            }
            banner = "The browser is open. Finish connecting there; Vantage will show "
                + "Connected once the callback reaches it — press Refresh if it does not."
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func disconnect() async {
        isWorking = true
        errorMessage = nil
        defer { isWorking = false }
        do {
            let result = try await api.googleDisconnect()
            banner = result.detail ?? "Disconnected."
            events = nil
            tasks = nil
            eventsError = nil
            tasksError = nil
            // Local state is cleared only after the backend answered; the
            // re-read is what decides what the screen shows next. If it turns
            // out the token could not be forgotten, the status says so.
            await load()
        } catch {
            // A failed disconnect changes nothing locally: the account is
            // still connected, and drawing it otherwise would be a lie.
            errorMessage = error.localizedDescription
        }
    }

    private func createTask() async {
        let title = newTask.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !title.isEmpty else { return }
        isWorking = true
        errorMessage = nil
        defer { isWorking = false }
        do {
            let due = newTaskDue.trimmingCharacters(in: .whitespacesAndNewlines)
            let result = try await api.googleTaskCreate(
                APIModels.GoogleTaskCreateRequest(
                    title: title, notes: nil,
                    due: due.isEmpty ? nil : due,
                    list_id: nil, source: nil))
            guard result.ok == true else {
                errorMessage = result.error ?? "Google did not confirm the task was created."
                return
            }
            newTask = ""
            newTaskDue = ""
            isAddingTask = false
            banner = "Added “\(title)” to Google Tasks."
            await loadTasks()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func complete(_ task: APIModels.GoogleTask) async {
        isWorking = true
        errorMessage = nil
        defer { isWorking = false }
        do {
            let result = try await api.googleTaskComplete(task.id, listId: task.list_id ?? "")
            guard result.ok == true else {
                errorMessage = result.error ?? "Google did not confirm the task was completed."
                return
            }
            // Re-read from Google rather than marking the row locally: the
            // list that matters is the one Google reports afterwards.
            await loadTasks()
            NotificationCenter.default.post(name: .vantageGoogleDidChange, object: nil)
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    /// Delete the task the person confirmed. Same rule as `complete`: the
    /// screen keeps what Google reports after the write, not what the write
    /// claimed.
    private func deletePendingTask() async {
        guard let task = taskPendingDelete else { return }
        taskPendingDelete = nil
        isWorking = true
        errorMessage = nil
        defer { isWorking = false }
        do {
            _ = try await api.googleTaskDelete(task.id, listId: task.list_id ?? "")
            banner = "Deleted “\(task.title)” from Google Tasks."
            await loadTasks()
            NotificationCenter.default.post(name: .vantageGoogleDidChange, object: nil)
        } catch {
            errorMessage = "The task was not deleted. \(error.localizedDescription)"
        }
    }

    private func timeOf(_ iso: String?) -> String {
        guard let iso, !iso.isEmpty else { return "—" }
        // The backend sends RFC3339; show the local clock time.
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        var date = formatter.date(from: iso)
        if date == nil {
            formatter.formatOptions = [.withInternetDateTime]
            date = formatter.date(from: iso)
        }
        guard let date else { return iso }
        let out = DateFormatter()
        out.dateFormat = "HH:mm"
        return out.string(from: date)
    }
}