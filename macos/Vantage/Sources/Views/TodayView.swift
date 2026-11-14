import SwiftUI

/// The unified Today workspace: what is happening, from every source that
/// actually answered.
///
/// The brief asked for one screen combining Google Calendar, Google Tasks, the
/// planner, Quick Capture and failed work. The hard part is not the layout, it
/// is the honesty rules, and they are decided in `TodayModel`/`TodayMapping`:
///
/// * a source that failed is drawn as failed, with the provider's own reason
///   and a Retry — never as an empty list;
/// * a source that is not connected says so, and offers the way to connect it;
/// * a source that answered with nothing says "nothing", which is a fact;
/// * every row keeps its origin, so an action is sent back to the source that
///   owns the record rather than to a copy of it.
///
/// This screen owns no data. It reads the same routes the Google, Planner,
/// Capture and Home screens read, and mutations go through the same routes they
/// use. There is no second calendar or task database here.
struct TodayView: View {

    @ObservedObject private var captureStore = CaptureStore.shared
    @State private var board = TodayModel.Board.initial()
    @State private var isLoading = true
    @State private var isWorking = false
    @State private var lastLoaded: Date?

    /// The task id currently being completed, so its row shows progress and a
    /// second press cannot start a duplicate write.
    @State private var completingTaskID: String?
    /// Actions report here. Kept apart from a source's own failure so a
    /// successful refresh does not silently clear "that write was refused".
    @State private var banner: String?
    @State private var actionError: String?

    @State private var newTaskTitle = ""
    @State private var newTaskDue = ""
    @State private var isAddingTask = false

    /// A schedule request the person typed here. Shown as a *proposal* until
    /// they confirm it.
    @State private var scheduling = false
    @State private var scheduleText = ""
    @State private var proposal: CalendarProposal.CalendarProposal?
    @State private var proposalQuestion: String?
    /// The idempotency guard for the event write: set while a create is in
    /// flight, so a double press cannot create two events.
    @State private var creatingEvent = false

    private let api = APIClient.shared

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: V.Space.section) {
                header

                if let actionError {
                    Notice(kind: .error, message: actionError, actionTitle: "Dismiss") {
                        self.actionError = nil
                    }
                }
                if let banner {
                    Notice(kind: .success, message: banner, actionTitle: "Dismiss") {
                        self.banner = nil
                    }
                }
                // One summary line for source failures. The detail stays in
                // each section, where its own Retry lives.
                if let summary = board.failureSummary {
                    Notice(kind: .warning, message: summary, actionTitle: "Retry all") {
                        Task { await reload() }
                    }
                }

                calendarSection
                googleTasksSection
                plannerSection
                captureSection
                attentionSection

                footnote
            }
            .padding(.horizontal, V.Space.gutter)
            .padding(.vertical, V.Space.section)
            .frame(maxWidth: 860)
            .frame(maxWidth: .infinity, alignment: .center)
        }
        .background(V.background)
        .navigationTitle("Today")
        .task { await reload() }
        .refreshable { await reload() }
        .onChange(of: captureStore.items) { _ in refreshCapture() }
        .onReceive(NotificationCenter.default.publisher(for: .vantageGoogleDidChange)) { _ in
            // A write on the Google screen (or a flow step) makes this screen's
            // cached rows stale. Re-read rather than leaving yesterday's answer.
            Task { await reload() }
        }
        .onReceive(NotificationCenter.default.publisher(for: .vantageCalendarProposalReady)) { note in
            // The Command Center understood a calendar sentence. Show the same
            // proposal here — still nothing written, still one button.
            guard let text = note.userInfo?["text"] as? String else { return }
            scheduleText = text
            if case .ready(let ready) = CalendarProposal.interpret(text) {
                proposal = ready
                proposalQuestion = nil
                scheduling = false
            }
        }
    }

    // MARK: - Header

    private var header: some View {
        PageHeader(
            title: "Today",
            subtitle: greetingLine
        ) {
            HStack(spacing: V.Space.s) {
                if isLoading { ProgressView().controlSize(.small) }
                if let stamp = lastLoaded, !isLoading {
                    Text("Updated \(relative(stamp))")
                        .font(V.Font.caption)
                        .foregroundStyle(V.textTertiary)
                }
                Button {
                    Task { await reload() }
                } label: {
                    Label("Refresh", systemImage: "arrow.clockwise")
                }
                .buttonStyle(VSecondaryButtonStyle())
                .disabled(isLoading || isWorking)
            }
        }
    }

    private var greetingLine: String {
        let formatter = DateFormatter()
        formatter.dateFormat = "EEEE d MMMM"
        let today = formatter.string(from: Date())
        let openTasks = board.googleTasks.rows.count
        let events = board.calendar.rows.count
        var parts = [today]
        if board.calendar.state == .ready {
            parts.append(events == 1 ? "1 event" : "\(events) events")
        }
        if board.googleTasks.state == .ready, openTasks > 0 {
            parts.append(openTasks == 1 ? "1 Google task" : "\(openTasks) Google tasks")
        }
        return parts.joined(separator: " · ")
    }

    private func relative(_ date: Date) -> String {
        let formatter = RelativeDateTimeFormatter()
        formatter.unitsStyle = .abbreviated
        return formatter.localizedString(for: date, relativeTo: Date())
    }

    // MARK: - Calendar

    private var calendarSection: some View {
        Card {
            VStack(alignment: .leading, spacing: V.Space.s) {
                SectionBlock(title: "Calendar", trailing: calendarTrailing) {
                    VStack(alignment: .leading, spacing: V.Space.s) {
                        schedulingControls
                        sourceBody(board.calendar,
                                   loading: "Reading your calendar…",
                                   open: { V.navigate(.planner) })
                    }
                }
            }
        }
    }

    @ViewBuilder
    private var calendarTrailing: AnyView? {
        AnyView(HStack(spacing: V.Space.s) {
            if board.calendar.state.isFailure || board.calendar.state.isUnavailable {
                Button("Retry") { Task { await reloadCalendarAndTasks() } }
                    .buttonStyle(VQuietButtonStyle())
            }
            Button("Open Planner") { V.navigate(.planner) }
                .buttonStyle(VQuietButtonStyle())
        })
    }

    /// Propose an event from a sentence. Nothing is written until the person
    /// presses Create on a proposal that states the exact day, time and title.
    @ViewBuilder
    private var schedulingControls: some View {
        if let proposal {
            proposalCard(proposal)
        } else if scheduling {
            VStack(alignment: .leading, spacing: V.Space.s) {
                HStack(spacing: V.Space.s) {
                    VField(placeholder: "e.g. schedule a review meeting tomorrow at 10 AM",
                           text: $scheduleText,
                           onSubmit: { propose() })
                    Button("Review") { propose() }
                        .buttonStyle(VPrimaryButtonStyle())
                        .disabled(scheduleText.trimmingCharacters(in: .whitespaces).isEmpty)
                    Button("Cancel") {
                        scheduling = false; scheduleText = ""; proposalQuestion = nil
                    }
                    .buttonStyle(VQuietButtonStyle(tint: V.textSecondary))
                }
                if let proposalQuestion {
                    Text(proposalQuestion)
                        .font(V.Font.caption)
                        .foregroundStyle(V.warning)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        } else {
            HStack(spacing: V.Space.s) {
                Button {
                    isAddingTask = false
                    scheduling = true
                } label: {
                    Label("Schedule an event", systemImage: "calendar.badge.plus")
                }
                .buttonStyle(VQuietButtonStyle())
                Button {
                    isAddingTask = true
                    scheduling = false
                } label: {
                    Label("Add a Google task", systemImage: "plus.circle")
                }
                .buttonStyle(VQuietButtonStyle())
            }
            if isAddingTask { addTaskControls }
        }
    }

    private var addTaskControls: some View {
        VStack(alignment: .leading, spacing: V.Space.s) {
            HStack(spacing: V.Space.s) {
                VField(placeholder: "What needs doing?",
                       text: $newTaskTitle,
                       onSubmit: { Task { await createTask() } })
                VField(placeholder: "due (optional, 2026-11-01)",
                       text: $newTaskDue)
                    .frame(width: 200)
                Button("Add") { Task { await createTask() } }
                    .buttonStyle(VPrimaryButtonStyle())
                    .disabled(newTaskTitle.trimmingCharacters(in: .whitespaces).isEmpty || isWorking)
                Button("Cancel") {
                    isAddingTask = false; newTaskTitle = ""; newTaskDue = ""
                }
                .buttonStyle(VQuietButtonStyle(tint: V.textSecondary))
            }
            Text("Created in Google Tasks on the connected account — not in the local planner.")
                .font(V.Font.caption)
                .foregroundStyle(V.textTertiary)
        }
    }

    /// The readable summary required before a calendar write. It names the day,
    /// the times and the title, and says where the date came from.
    private func proposalCard(_ proposal: CalendarProposal.CalendarProposal) -> some View {
        Card(tone: .gold) {
            VStack(alignment: .leading, spacing: V.Space.s) {
                Text("Create this event?")
                    .font(V.Font.cardTitle).foregroundStyle(V.text)
                Text(proposal.summary())
                    .font(V.Font.body).foregroundStyle(V.text)
                    .fixedSize(horizontal: false, vertical: true)
                Text("Nothing has been written yet. Creating it writes to the connected "
                     + "calendar; a read-only calendar will refuse and say so.")
                    .font(V.Font.caption).foregroundStyle(V.textTertiary)
                    .fixedSize(horizontal: false, vertical: true)
                HStack(spacing: V.Space.s) {
                    Button {
                        Task { await createEvent(proposal) }
                    } label: {
                        if creatingEvent {
                            HStack(spacing: V.Space.xs) {
                                ProgressView().controlSize(.small)
                                Text("Creating…")
                            }
                        } else {
                            Text("Create event")
                        }
                    }
                    .buttonStyle(VPrimaryButtonStyle())
                    .disabled(creatingEvent)
                    Button("Discard") {
                        self.proposal = nil
                        scheduleText = ""
                        scheduling = false
                    }
                    .buttonStyle(VQuietButtonStyle(tint: V.textSecondary))
                    .disabled(creatingEvent)
                }
            }
        }
    }

    // MARK: - Google Tasks

    private var googleTasksSection: some View {
        Card {
            VStack(alignment: .leading, spacing: V.Space.s) {
                SectionBlock(title: "Google Tasks", trailing: googleTasksTrailing) {
                    sourceBody(board.googleTasks,
                               loading: "Reading Google Tasks…",
                               open: { V.navigate(.google) })
                }
            }
        }
    }

    private var googleTasksTrailing: AnyView {
        AnyView(HStack(spacing: V.Space.s) {
            if board.googleTasks.state.isFailure {
                Button("Retry") { Task { await reloadCalendarAndTasks() } }
                    .buttonStyle(VQuietButtonStyle())
            }
            Button("Open Google") { V.navigate(.google) }
                .buttonStyle(VQuietButtonStyle())
        })
    }

    // MARK: - Planner, Capture, Attention

    private var plannerSection: some View {
        Card {
            SectionBlock(title: "Planner", trailing: AnyView(
                Button("Open Planner") { V.navigate(.planner) }
                    .buttonStyle(VQuietButtonStyle()))) {
                sourceBody(board.planner, loading: "Reading planner tasks…",
                           open: { V.navigate(.planner) })
            }
        }
    }

    private var captureSection: some View {
        Card {
            SectionBlock(title: "Quick Capture", trailing: AnyView(
                Button("Open Inbox") { V.navigate(.capture) }
                    .buttonStyle(VQuietButtonStyle()))) {
                sourceBody(board.capture, loading: "Reading the inbox…",
                           open: { V.navigate(.capture) })
            }
        }
    }

    private var attentionSection: some View {
        Card {
            SectionBlock(title: "Needs attention", trailing: AnyView(
                Button("Open Activity") { V.navigate(.activity) }
                    .buttonStyle(VQuietButtonStyle()))) {
                sourceBody(board.attention, loading: "Checking what needs attention…",
                           open: { V.navigate(.work) })
            }
        }
    }

    private var footnote: some View {
        Text("Every row keeps the identity it has at its source: a Google event or task is "
             + "still Google's, a planner task is still the planner's, and a capture is "
             + "still in your inbox. Nothing is copied into a second database.")
            .font(V.Font.caption)
            .foregroundStyle(V.textTertiary)
            .fixedSize(horizontal: false, vertical: true)
    }

    // MARK: - Section rendering

    /// The five states a source can be in, drawn differently on purpose.
    ///
    /// A failed read never falls through to the empty text: that is the defect
    /// this screen exists to avoid, so the branches cannot share an arm.
    @ViewBuilder
    private func sourceBody(_ section: TodayModel.Section,
                            loading: String,
                            open: @escaping () -> Void) -> some View {
        switch section.state {
        case .loading:
            LoadingRow(text: loading)

        case .failed(let reason):
            VStack(alignment: .leading, spacing: V.Space.xs) {
                Notice(kind: .error, message: reason)
                Text("This is a failed read, not an empty list — the items may exist "
                     + "and simply could not be retrieved.")
                    .font(V.Font.caption).foregroundStyle(V.textTertiary)
                    .fixedSize(horizontal: false, vertical: true)
            }

        case .unavailable(let reason):
            Notice(kind: .info, message: reason)

        case .empty:
            Text(section.emptyText)
                .font(V.Font.body).foregroundStyle(V.textSecondary)

        case .ready:
            VStack(alignment: .leading, spacing: 0) {
                ForEach(section.rows) { row in
                    rowView(row)
                    if row.id != section.rows.last?.id {
                        Divider().overlay(V.divider)
                    }
                }
            }
        }
    }

    @ViewBuilder
    private func rowView(_ row: TodayModel.Row) -> some View {
        HStack(alignment: .top, spacing: V.Space.m) {
            sourceGlyph(row)
            VStack(alignment: .leading, spacing: 2) {
                Button {
                    open(row)
                } label: {
                    Text(row.title)
                        .font(V.Font.body)
                        .foregroundStyle(V.text)
                        .multilineTextAlignment(.leading)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .help(rowHelp(row))
                .accessibilityHint("Opens this item in the screen that owns it.")
                if let detail = row.detail {
                    Text(detail)
                        .font(V.Font.caption)
                        .foregroundStyle(V.textSecondary)
                        .lineLimit(2)
                }
            }
            Spacer(minLength: V.Space.s)
            if let trailing = row.trailing {
                Text(trailing)
                    .font(V.Font.caption.monospacedDigit())
                    .foregroundStyle(trailingColour(row.emphasis))
                    .lineLimit(1)
            }
            rowAction(row)
        }
        .padding(.vertical, V.Space.s)
    }

    private func sourceGlyph(_ row: TodayModel.Row) -> some View {
        let symbol: String
        switch row.origin {
        case .googleEvent: symbol = TodayModel.Source.calendar.symbol
        case .googleTask: symbol = TodayModel.Source.googleTasks.symbol
        case .plannerTask: symbol = TodayModel.Source.planner.symbol
        case .capture: symbol = TodayModel.Source.capture.symbol
        case .attention: symbol = TodayModel.Source.attention.symbol
        }
        return Image(systemName: symbol)
            .font(.system(size: 11, weight: .medium))
            .foregroundStyle(V.textTertiary)
            .frame(width: 18, alignment: .center)
            .padding(.top, 1)
    }

    private func trailingColour(_ emphasis: TodayModel.Row.Emphasis) -> Color {
        switch emphasis {
        case .normal: return V.textTertiary
        case .urgent: return V.warning
        case .done: return V.accent
        }
    }

    private func rowHelp(_ row: TodayModel.Row) -> String {
        switch row.origin {
        case .googleEvent: return "Open this event in Planner"
        case .googleTask: return "Open Google Tasks"
        case .plannerTask: return "Open this task in Planner"
        case .capture: return "Open this item in Quick Capture"
        case .attention: return "Open where this needs attention"
        }
    }

    /// A row's own action, drawn only where one exists. Never the whole row:
    /// a card or a row that completes a task when clicked in the wrong place
    /// is how an accidental write happens.
    @ViewBuilder
    private func rowAction(_ row: TodayModel.Row) -> some View {
        switch row.origin {
        case .googleTask(let id, _):
            if completingTaskID == id {
                ProgressView().controlSize(.small)
                    .frame(width: 22)
            } else {
                Button {
                    Task { await complete(taskID: id) }
                } label: {
                    Image(systemName: "checkmark.circle")
                        .font(.system(size: 13))
                }
                .buttonStyle(.plain)
                .foregroundStyle(V.accent)
                .help("Mark this Google task complete")
                .accessibilityLabel("Mark complete")
                .disabled(isWorking || completingTaskID != nil)
            }
        case .attention(let surface, let ref):
            Button("Open") { openAttention(surface: surface, ref: ref) }
                .buttonStyle(VQuietButtonStyle())
        case .googleEvent, .plannerTask, .capture:
            EmptyView()
        }
    }

    // MARK: - Navigation

    private func open(_ row: TodayModel.Row) {
        switch row.origin {
        case .googleEvent:
            V.navigate(.planner)
        case .googleTask:
            V.navigate(.google)
        case .plannerTask:
            V.navigate(.planner)
        case .capture(let id):
            AppNavigation.shared.go(.capture, captureID: id)
        case .attention(let surface, let ref):
            openAttention(surface: surface, ref: ref)
        }
    }

    private func openAttention(surface: String, ref: String) {
        switch surface {
        case "work":
            V.navigate(.work)
        case "flows":
            V.navigate(.flows)
        default:
            V.navigate(.activity)
        }
    }

    // MARK: - Loading

    /// One refresh of everything. Each source is fetched independently so one
    /// failure cannot prevent the others from loading, and each result is
    /// applied to its own section as it lands.
    private func reload() async {
        isLoading = true
        async let calendar: () = reloadCalendar()
        async let tasks: () = reloadTasks()
        async let planner: () = reloadPlanner()
        async let attention: () = reloadAttention()
        refreshCapture()
        _ = await (calendar, tasks, planner, attention)
        lastLoaded = Date()
        isLoading = false
    }

    /// The pair the Google reads share: both go to Google, so Retry re-reads
    /// both rather than leaving one half-stale.
    private func reloadCalendarAndTasks() async {
        isWorking = true
        defer { isWorking = false }
        async let calendar: () = reloadCalendar()
        async let tasks: () = reloadTasks()
        _ = await (calendar, tasks)
    }

    private func reloadCalendar() async {
        do {
            let response = try await api.googleEvents()
            board.calendar = TodayMapping.calendarSection(response, error: nil)
        } catch {
            board.calendar = TodayMapping.calendarSection(nil, error: fetchReason(error))
        }
    }

    private func reloadTasks() async {
        do {
            let response = try await api.googleTasks()
            board.googleTasks = TodayMapping.googleTaskSplit(
                response, error: nil, now: Date(), calendar: .current).section
        } catch {
            board.googleTasks = TodayMapping.googleTaskSplit(
                nil, error: fetchReason(error), now: Date(), calendar: .current).section
        }
    }

    private func reloadPlanner() async {
        do {
            board.planner = TodayMapping.plannerSection(try await api.plannerTasks(),
                                                        error: nil, now: Date())
        } catch {
            board.planner = TodayMapping.plannerSection(nil, error: fetchReason(error),
                                                        now: Date())
        }
    }

    private func reloadAttention() async {
        do {
            board.attention = TodayMapping.attentionSection(try await api.attention(),
                                                            error: nil)
        } catch {
            board.attention = TodayMapping.attentionSection(nil, error: fetchReason(error))
        }
    }

    /// Capture lives in this app, not the backend. A store that could not be
    /// read is a failure, not an empty inbox.
    private func refreshCapture() {
        if captureStore.isReadOnly || captureStore.loadError != nil {
            board.capture = TodayMapping.captureSection(nil)
            board.capture.state = .failed(captureStore.loadError
                ?? "The capture inbox could not be read, so writes to it are paused.")
            return
        }
        board.capture = TodayMapping.captureSection(captureStore.items)
    }

    private func fetchReason(_ error: Error) -> String {
        if let apiError = error as? APIError {
            return apiError.localizedDescription
        }
        return error.localizedDescription
    }

    // MARK: - Actions

    /// Propose, never write. The person sees the summary first.
    private func propose() {
        let text = scheduleText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        switch CalendarProposal.interpret(text) {
        case .ready(let proposal):
            self.proposal = proposal
            self.proposalQuestion = nil
        case .needsDetail(let question):
            self.proposal = nil
            self.proposalQuestion = question
        case .notACalendarRequest:
            self.proposal = nil
            self.proposalQuestion = "That does not read as a calendar request. Try "
                + "“schedule a review meeting tomorrow at 10 AM”."
        }
    }

    private func createEvent(_ proposal: CalendarProposal.CalendarProposal) async {
        guard !creatingEvent else { return }   // double-submit guard
        creatingEvent = true
        actionError = nil
        defer { creatingEvent = false }

        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        let start = formatter.string(from: proposal.start)
        do {
            let result = try await api.createCalendarEvent(
                title: proposal.title, start: start,
                durationMinutes: proposal.durationMinutes)
            guard result.ok == true else {
                // The tool's own sentence: a read-only calendar says so here,
                // and repeating a generic failure would hide the real cause.
                actionError = result.message ?? "The event was not created."
                return
            }
            banner = result.verified == true
                ? "Created “\(proposal.title)”. The calendar was read back to confirm it."
                : "Wrote “\(proposal.title)”. The calendar could not be read back, so "
                    + "this is not confirmed yet."
            self.proposal = nil
            scheduleText = ""
            scheduling = false
            await reloadCalendar()
            NotificationCenter.default.post(name: .vantageGoogleDidChange, object: nil)
        } catch {
            actionError = fetchReason(error)
        }
    }

    private func createTask() async {
        let title = newTaskTitle.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !title.isEmpty, !isWorking else { return }
        isWorking = true
        actionError = nil
        defer { isWorking = false }
        let due = newTaskDue.trimmingCharacters(in: .whitespacesAndNewlines)
        do {
            let result = try await api.googleTaskCreate(
                APIModels.GoogleTaskCreateRequest(
                    title: title, notes: nil,
                    due: due.isEmpty ? nil : due,
                    list_id: nil, source: "Vantage"))
            // "ok" is the backend's verdict; the id is Google's. A task is only
            // reported as created when Google returned the record.
            guard result.ok == true, let task = result.task, !task.id.isEmpty else {
                actionError = result.error ?? "Google did not confirm the task was created."
                return
            }
            newTaskTitle = ""
            newTaskDue = ""
            isAddingTask = false
            banner = "Added “\(task.title)” to Google Tasks."
            await reloadTasks()
            NotificationCenter.default.post(name: .vantageGoogleDidChange, object: nil)
        } catch {
            actionError = fetchReason(error)
        }
    }

    private func complete(taskID: String) async {
        guard completingTaskID == nil else { return }
        completingTaskID = taskID
        actionError = nil
        defer { completingTaskID = nil }
        do {
            let result = try await api.googleTaskComplete(taskID)
            guard result.ok == true, result.task?.done == true else {
                actionError = result.error
                    ?? "Google did not confirm the task was completed."
                return
            }
            banner = "Completed “\(result.task?.title ?? "the task")”."
            // Re-read so the row disappears from *Google's* answer rather than
            // from a local guess about what completing it meant.
            await reloadTasks()
            NotificationCenter.default.post(name: .vantageGoogleDidChange, object: nil)
        } catch {
            actionError = fetchReason(error)
        }
    }
}

// MARK: - Cross-screen notification

extension Notification.Name {
    /// Posted after a Google write succeeds, so screens showing Google data
    /// re-read instead of keeping an answer the write just invalidated.
    static let vantageGoogleDidChange = Notification.Name("vantage.google.didChange")

    /// Posted by the Command Center when it understood a calendar request.
    /// It carries the sentence; Today re-parses it into a proposal so the
    /// screen shows the *same* summary the reply named, and still writes
    /// nothing until the person presses Create.
    static let vantageCalendarProposalReady =
        Notification.Name("vantage.calendar.proposalReady")
}

private extension V {
    /// Navigation through the one router the app already has. Kept as a helper
    /// so this screen cannot grow a second way to change screens.
    @MainActor
    static func navigate(_ item: NavItem) {
        AppNavigation.shared.go(item)
    }
}