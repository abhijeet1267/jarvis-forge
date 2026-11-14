import SwiftUI

/// The daily planner (Epic 1) in the native app.
///
/// Reads the same `/planner/*` and `/schedules` endpoints the web planner uses,
/// so the two surfaces agree about the list and the day.
///
/// The screen is deliberate about honesty in three places:
/// * an unread calendar is shown as **could not read**, never as "nothing today";
/// * a blocked task is labelled, and is not offered as "next";
/// * the next action explains *why* it was chosen.
struct PlannerView: View {

    /// The backend's polled health, for naming which calendar provider
    /// `/planner/today` actually read (Google, CalDAV, or the local calendar).
    /// Observed, never owned — see ContentView for why @StateObject is wrong
    /// for this singleton.
    @ObservedObject private var backend = BackendManager.shared

    @State private var tasks: [APIModels.PlannerTask] = []
    @State private var agenda: APIModels.PlannerAgenda?
    @State private var schedules: [APIModels.ScheduledJob] = []
    @State private var isLoading = true
    /// A refresh after the first load: the page stays readable and says it is
    /// working, rather than blanking to a spinner on every action.
    @State private var isRefreshing = false
    @State private var lastLoaded: Date?
    @State private var errorMessage: String?
    /// The last *read* failed, so the lists below are unknown rather than empty.
    ///
    /// Separate from `errorMessage`, which any action also sets: a failed
    /// "complete task" must not make the task list deny it has anything in it.
    @State private var readFailed = false

    // Google Tasks: read live from Google, never copied into the planner.
    @State private var googleStatus: APIModels.GoogleStatus?
    @State private var googleTasks: [APIModels.GoogleTask] = []
    /// The Google task read failed (transport or a refused read). Distinct from
    /// "not connected", which is a state, not a failure.
    @State private var googleReadFailed = false
    @State private var googleDetail: String?
    /// A Google write (add/complete) failed or was refused.
    @State private var googleWriteError: String?
    @State private var newGoogleTask = ""
    @State private var googleWorking = false
    @State private var newTitle = ""
    @State private var newPriority = "normal"
    @State private var goalText = ""
    @State private var attention: [APIModels.AttentionItem] = []
    /// Job id -> its runs, once History has been opened for it.
    @State private var histories: [String: [APIModels.ScheduleRun]] = [:]
    /// The job currently being edited, and the in-progress values.
    @State private var editingJob: String?
    @State private var editName = ""
    @State private var editWhen = ""
    @State private var meetingName = ""
    @State private var meetingBrief: APIModels.MeetingBrief?
    @State private var meetingError = ""
    @State private var meetingBusy = false
    @State private var beginOutcome = ""
    @State private var beginBusy = false
    @State private var taskSearch = ""
    @State private var taskFilter = "all"
    @State private var eventTitle = ""
    @State private var eventWhen = ""
    @State private var eventNote = ""
    @State private var confirmingCancel: String?

    private let api = APIClient.shared

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header

            if let error = errorMessage {
                Notice(kind: .error, message: error, actionTitle: "Retry") {
                    Task { await load() }
                }
                .padding(.horizontal, V.Space.gutter)
                .padding(.bottom, V.Space.s)
            }

            if isLoading {
                Spacer()
                LoadingRow(text: "Reading your planner…")
                    .frame(maxWidth: .infinity)
                Spacer()
            } else {
                ScrollView {
                    VStack(alignment: .leading, spacing: V.Space.l) {
                        nextUp
                        if !attentionItems.isEmpty { attentionSection }
                        composer
                        goalComposer
                        taskList
                        googleTasksSection
                        agendaSection
                        meetingSection
                        scheduleSection
                    }
                    .padding(.horizontal, V.Space.gutter)
                    .padding(.bottom, V.Space.section)
                    .frame(maxWidth: 820)
                    .frame(maxWidth: .infinity, alignment: .center)
                }
            }
        }
        .background(V.background)
        .task { await load() }
        .vConfirm(
            "Cancel this event?",
            message: "\u{201C}\(confirmingCancel ?? "This event")\u{201D} will be removed from "
                + "your calendar. This cannot be undone.",
            confirm: "Cancel event",
            isPresented: Binding(
                get: { confirmingCancel != nil },
                set: { if !$0 { confirmingCancel = nil } })) {
                if let title = confirmingCancel,
                   let uid = pendingCancelUID[title] {
                    Task { await cancelEvent(uid) }
                }
                confirmingCancel = nil
            }
        .onChange(of: agenda?.events?.count) { _ in pendingCancelUID = [:] }
    }

    /// Title -> uid for events on screen. The confirm alert holds the title
    /// while the event list refreshes underneath it.
    @State private var pendingCancelUID: [String: String] = [:]

    // MARK: - Header

    private var header: some View {
        PageHeader(title: "Planner", subtitle: "Today, its tasks and its appointments.") {
            HStack(spacing: V.Space.s) {
                if isRefreshing { ProgressView().controlSize(.small) }
                if let stamp = lastLoaded, !isRefreshing {
                    Text("Updated \(relative(stamp))")
                        .font(V.Font.caption)
                        .foregroundStyle(V.textTertiary)
                }
                Button {
                    Task { await load() }
                } label: {
                    Label("Refresh", systemImage: "arrow.clockwise")
                }
                .buttonStyle(VSecondaryButtonStyle())
                .disabled(isLoading || isRefreshing)
            }
        }
        .padding(.horizontal, V.Space.gutter)
        .padding(.top, V.Space.section)
        .padding(.bottom, V.Space.l)
        // Same column as the cards below, so the title is not 24pt to their left.
        .frame(maxWidth: 820)
        .frame(maxWidth: .infinity, alignment: .center)
    }

    private func relative(_ date: Date) -> String {
        let formatter = RelativeDateTimeFormatter()
        formatter.unitsStyle = .abbreviated
        return formatter.localizedString(for: date, relativeTo: Date())
    }

    private var nextUp: some View {
        Card {
            VStack(alignment: .leading, spacing: V.Space.s) {
                SectionBlock(title: "Next up") {
                    VStack(alignment: .leading, spacing: V.Space.xs) {
                        if let next = nextTask {
                            Text(next.title).font(.system(size: 16, weight: .semibold))
                                .foregroundStyle(V.text)
                            Text(nextReason)
                                .font(V.Font.caption)
                                .foregroundStyle(V.textSecondary)
                        } else if readFailed {
                            Text("I could not read your tasks, so this is not an empty list.")
                                .font(V.Font.body)
                                .foregroundStyle(V.textSecondary)
                        } else {
                            Text("Nothing to do right now.")
                                .font(V.Font.body)
                                .foregroundStyle(V.textSecondary)
                        }
                    }
                }
                // Acting on the plan, not just reading it: opens the next task's
                // project and offers the agent prompt. It never starts the agent.
                HStack(spacing: V.Space.s) {
                    Button("Begin work") { Task { await beginWork() } }
                        .buttonStyle(VPrimaryButtonStyle())
                        .disabled(beginBusy)
                    if beginBusy { ProgressView().controlSize(.small) }
                    if !beginOutcome.isEmpty {
                        Text(beginOutcome)
                            .font(V.Font.caption)
                            .foregroundStyle(V.textSecondary)
                            .textSelection(.enabled)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
        }
    }

    private func addEvent() async {
        let title = eventTitle.trimmingCharacters(in: .whitespacesAndNewlines)
        let when = eventWhen.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !title.isEmpty, !when.isEmpty else { return }
        do {
            eventNote = try await api.createEvent(title: title, start: when)
            eventTitle = ""; eventWhen = ""
            await load()
        } catch {
            // The backend refuses a time it cannot parse, and that refusal is the
            // message — better than booking the wrong hour.
            eventNote = error.localizedDescription
        }
    }

    private func cancelEvent(_ uid: String) async {
        do {
            eventNote = try await api.cancelEvent(uid)
            await load()
        } catch {
            eventNote = error.localizedDescription
        }
    }

    private func beginWork() async {
        beginBusy = true
        beginOutcome = "Planning…"
        do {
            let result = try await api.beginWork()
            var parts: [String] = []
            if let opened = result.opened_project, opened.attempted == true {
                parts.append((opened.ok == true ? "Opened: " : "Could not open: ")
                             + (opened.message ?? ""))
            } else {
                parts.append("No project to open for the next task.")
            }
            if let prompt = result.agent?.prompt, !prompt.isEmpty {
                parts.append("Next prompt: " + prompt)
            }
            beginOutcome = parts.joined(separator: "  ·  ")
            await load()
        } catch {
            beginOutcome = error.localizedDescription
        }
        beginBusy = false
    }

    /// Attention items minus the one the "Next up" card already names. An
    /// overdue task is chosen as the next action *because* it is overdue, so
    /// showing it twice reads as two problems where there is one. The Next-up
    /// card is not dropped for it — it carries the reason and Begin work.
    private var attentionItems: [APIModels.AttentionItem] {
        guard let next = nextTask?.title else { return attention }
        return attention.filter { item in
            !(item.title.contains(next) && item.kind != "approval")
        }
    }

    private var attentionSection: some View {
        Card(tone: .warning) {
            VStack(alignment: .leading, spacing: V.Space.s) {
                SectionBlock(title: "Needs attention") {
                    VStack(alignment: .leading, spacing: V.Space.s) {
                        ForEach(attentionItems) { item in
                            HStack(alignment: .top, spacing: V.Space.s) {
                                Image(systemName: "exclamationmark.triangle.fill")
                                    .foregroundStyle(V.warning)
                                    .font(.system(size: 11, weight: .semibold))
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(item.title).font(V.Font.body).foregroundStyle(V.text)
                                    if let d = item.detail, !d.isEmpty {
                                        Text(d).font(V.Font.caption)
                                            .foregroundStyle(V.textSecondary)
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    private var goalComposer: some View {
        Card {
            VStack(alignment: .leading, spacing: V.Space.s) {
                SectionBlock(title: "Plan a goal") {
                    VStack(alignment: .leading, spacing: V.Space.s) {
                        HStack(spacing: V.Space.s) {
                            VField(placeholder: "Ship the AMR demo", text: $goalText)
                            Button("Plan") { Task { await planGoal() } }
                                .buttonStyle(VPrimaryButtonStyle())
                                .disabled(goalText.trimmingCharacters(in: .whitespaces).isEmpty)
                        }
                        Text("Creates ordered tasks. Nothing is added to your calendar.")
                            .font(V.Font.caption)
                            .foregroundStyle(V.textTertiary)
                    }
                }
            }
        }
    }

    private var composer: some View {
        Card {
            HStack(spacing: V.Space.s) {
                VField(placeholder: "What needs doing?", text: $newTitle)
                Picker("", selection: $newPriority) {
                    Text("Normal").tag("normal")
                    Text("High").tag("high")
                    Text("Urgent").tag("urgent")
                    Text("Low").tag("low")
                }
                .labelsHidden()
                .frame(width: 110)
                Button("Add") { Task { await addTask() } }
                    .buttonStyle(VPrimaryButtonStyle())
                    .disabled(newTitle.trimmingCharacters(in: .whitespaces).isEmpty)
            }
        }
    }

    /// Filter modes, matching the planner page's own list.
    private static let filterModes: [(id: String, label: String)] = [
        ("all", "All open"), ("priority", "Urgent & high"),
        ("overdue", "Overdue"), ("blocked", "Blocked"), ("project", "With a project"),
    ]

    /// The tasks that survive the current search and filter.
    ///
    /// Filtering is local because the list is already fetched — the same choice the
    /// web planner makes. Empty and no-match are different states, and saying
    /// "no match" when the real problem is an empty list would be a small lie.
    private var visibleTasks: [APIModels.PlannerTask] {
        let needle = taskSearch.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        return tasks.filter { task in
            if !needle.isEmpty {
                let hay = "\(task.title) \(task.detail ?? "") \(task.project ?? "")".lowercased()
                if !hay.contains(needle) { return false }
            }
            switch taskFilter {
            case "priority": return task.priority == "urgent" || task.priority == "high"
            case "overdue": return task.overdue == true
            case "blocked": return task.blocked == true
            case "project": return !(task.project ?? "").isEmpty
            default: return true
            }
        }
    }

    private var taskList: some View {
        Card {
            VStack(alignment: .leading, spacing: V.Space.s) {
                SectionBlock(title: "Tasks", trailing: taskCountTrailing) {
                    VStack(alignment: .leading, spacing: V.Space.s) {
                        HStack(spacing: V.Space.s) {
                            VField(placeholder: "Search tasks…", text: $taskSearch)
                            Picker("", selection: $taskFilter) {
                                ForEach(Self.filterModes, id: \.id) { mode in
                                    Text(mode.label).tag(mode.id)
                                }
                            }
                            .labelsHidden()
                            .frame(maxWidth: 160)
                        }
                        if tasks.isEmpty {
                            Text(readFailed
                                 ? "I could not read your tasks, so this is not an empty list."
                                 : "No open tasks. Add one above.")
                                .font(V.Font.body)
                                .foregroundStyle(V.textSecondary)
                        } else if visibleTasks.isEmpty {
                            Text("No tasks match your search or filter.")
                                .font(V.Font.body)
                                .foregroundStyle(V.textSecondary)
                        } else {
                            VStack(alignment: .leading, spacing: 0) {
                                ForEach(visibleTasks) { task in
                                    taskRow(task)
                                    if task.id != visibleTasks.last?.id {
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

    private var taskCountTrailing: AnyView? {
        guard !tasks.isEmpty else { return nil }
        return AnyView(
            Text("\(visibleTasks.count) of \(tasks.count)")
                .font(V.Font.caption)
                .foregroundStyle(V.textTertiary))
    }

    private func taskRow(_ task: APIModels.PlannerTask) -> some View {
        HStack(alignment: .top, spacing: V.Space.m) {
            VStack(alignment: .leading, spacing: V.Space.xs) {
                Text(task.title)
                    .font(V.Font.body)
                    .foregroundStyle(V.text)
                HStack(spacing: V.Space.xs + 1) {
                    if let p = task.priority, p != "normal" {
                        StatusPill(kind: p == "urgent" ? .error : .pending, text: p)
                    }
                    if task.overdue == true {
                        StatusPill(kind: .error, text: "overdue")
                    }
                    if task.blocked == true {
                        StatusPill(kind: .pending, text: "blocked")
                    }
                    if let due = task.due_at, !due.isEmpty {
                        Text(dueShort(due))
                            .font(V.Font.caption)
                            .foregroundStyle(task.overdue == true ? V.danger : V.textTertiary)
                    }
                    if let project = task.project, !project.isEmpty {
                        Text(project).font(V.Font.caption).foregroundStyle(V.textTertiary)
                    }
                }
            }
            Spacer(minLength: V.Space.s)
            Button("Done") { Task { await complete(task) } }
                .buttonStyle(VQuietButtonStyle())
                .help("Mark this task complete")
        }
        .padding(.vertical, V.Space.s)
    }

    /// Prepare for a meeting: the event, its related mail and tasks, and any
    /// clash. Vantage prepares — it does not attend — and the card says so.
    private var meetingSection: some View {
        Card {
            VStack(alignment: .leading, spacing: V.Space.s) {
                SectionBlock(title: "Prepare for a meeting") {
                    VStack(alignment: .leading, spacing: V.Space.s) {
                        HStack(spacing: V.Space.s) {
                            VField(placeholder: "Meeting name (blank for the next one)",
                                   text: $meetingName)
                            Button("Prepare") { Task { await prepareMeeting() } }
                                .buttonStyle(VPrimaryButtonStyle())
                                .disabled(meetingBusy)
                            if meetingBusy { ProgressView().controlSize(.small) }
                        }
                        if !meetingError.isEmpty {
                            Notice(kind: .warning, message: meetingError)
                        }
                        if let b = meetingBrief {
                            if b.ok, let m = b.meeting {
                                VStack(alignment: .leading, spacing: V.Space.xs) {
                                    Text(m.title ?? "(untitled)")
                                        .font(V.Font.cardTitle).foregroundStyle(V.text)
                                    Text([m.start.map { formatWhen($0) } ?? "", m.location ?? ""]
                                        .filter { !$0.isEmpty }.joined(separator: "  ·  "))
                                        .font(V.Font.caption)
                                        .foregroundStyle(V.textSecondary)
                                    if let c = b.conflict, let t = c.title {
                                        Text("Overlaps ‘\(t)’.")
                                            .font(V.Font.caption).foregroundStyle(V.warning)
                                    }
                                    ForEach(Array((b.talking_points ?? []).enumerated()), id: \.offset) { _, p in
                                        Text("• \(p)").font(V.Font.body).foregroundStyle(V.text)
                                    }
                                    if let e = b.email_error, !e.isEmpty {
                                        Text("Note: \(e)")
                                            .font(V.Font.caption).foregroundStyle(V.warning)
                                    }
                                    if let steps = b.workflow, !steps.isEmpty {
                                        Text(steps.joined(separator: "  ·  "))
                                            .font(V.Font.caption)
                                            .foregroundStyle(V.textSecondary)
                                    }
                                    if let note = b.note {
                                        Text(note).font(V.Font.caption)
                                            .foregroundStyle(V.textSecondary)
                                    }
                                }
                            } else if let reason = b.reason {
                                Text(reason).font(V.Font.body).foregroundStyle(V.warning)
                            }
                        }
                    }
                }
            }
        }
    }

    private func formatWhen(_ iso: String) -> String {
        let parser = ISO8601DateFormatter()
        guard let date = parser.date(from: iso) else { return iso }
        return date.formatted(date: .abbreviated, time: .shortened)
    }

    private func prepareMeeting() async {
        meetingBusy = true
        meetingError = ""
        do {
            meetingBrief = try await api.prepareMeeting(
                meetingName.trimmingCharacters(in: .whitespacesAndNewlines))
        } catch {
            meetingBrief = nil
            meetingError = error.localizedDescription
        }
        meetingBusy = false
    }

    private var agendaSection: some View {
        Card {
            VStack(alignment: .leading, spacing: V.Space.s) {
                SectionBlock(title: "Today’s agenda", trailing: agendaTrailing) {
                    VStack(alignment: .leading, spacing: V.Space.s) {
                        if agenda?.calendar_read_failed == true {
                            Notice(kind: .warning,
                                   message: "Your calendar could not be read — this is not an empty day.")
                        } else if agenda == nil && readFailed {
                            // The whole planner read failed, so there is no agenda object
                            // at all. That is not the same as an agenda with no events, and
                            // "Nothing scheduled today" would be a claim nothing checked.
                            Notice(kind: .warning,
                                   message: "I could not read today's agenda, so this is not an empty day.")
                        } else if let events = agenda?.events, !events.isEmpty {
                            VStack(alignment: .leading, spacing: 0) {
                                ForEach(Array(events.enumerated()), id: \.offset) { index, anyEvent in
                                    let event = anyEvent.dictionary ?? [:]
                                    HStack(spacing: V.Space.m) {
                                        Text(timeOf(event["start"]?.stringValue ?? ""))
                                            .font(V.Font.mono.monospacedDigit())
                                            .foregroundStyle(V.textSecondary)
                                            .frame(width: 48, alignment: .leading)
                                        Text(event["title"]?.stringValue ?? "(untitled)")
                                            .font(V.Font.body).foregroundStyle(V.text)
                                        Spacer()
                                        if let uid = event["uid"]?.stringValue, !uid.isEmpty {
                                            let title = event["title"]?.stringValue ?? "This event"
                                            Button("Cancel") {
                                                pendingCancelUID[title] = uid
                                                confirmingCancel = title
                                            }
                                            .buttonStyle(VQuietButtonStyle(tint: V.danger))
                                            .help("Cancel this event")
                                        }
                                    }
                                    .padding(.vertical, V.Space.s)
                                    if index != events.count - 1 {
                                        Divider().overlay(V.divider)
                                    }
                                }
                            }
                        } else {
                            Text("Nothing scheduled today.")
                                .font(V.Font.body).foregroundStyle(V.textSecondary)
                        }

                        // Creating and cancelling were added to the web planner and not
                        // here, so the app could read its calendar and never change it.
                        HStack(spacing: V.Space.s) {
                            VField(placeholder: "Event title", text: $eventTitle)
                            VField(placeholder: "tomorrow at 3 PM", text: $eventWhen)
                                .frame(maxWidth: 230)
                            Button("Add") { Task { await addEvent() } }
                                .buttonStyle(VSecondaryButtonStyle())
                                .disabled(eventTitle.trimmingCharacters(in: .whitespaces).isEmpty
                                          || eventWhen.trimmingCharacters(in: .whitespaces).isEmpty)
                        }
                        if !eventNote.isEmpty {
                            Text(eventNote).font(V.Font.caption)
                                .foregroundStyle(V.textSecondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        Text("Cancelling an event asks first — it cannot be undone.")
                            .font(V.Font.caption).foregroundStyle(V.textTertiary)

                        if let conflicts = agenda?.conflicts, !conflicts.isEmpty {
                            ForEach(Array(conflicts.enumerated()), id: \.offset) { _, c in
                                let d = c.dictionary ?? [:]
                                Notice(kind: .warning,
                                       message: "‘\(d["first"]?.stringValue ?? "?")’ overlaps "
                                        + "‘\(d["second"]?.stringValue ?? "?")’")
                            }
                        }
                    }
                }
            }
        }
    }

    private var agendaTrailing: AnyView? {
        // Which calendar answered. `/planner/today` reads the active provider;
        // naming it means "nothing scheduled" is attributable to the calendar
        // the person actually expects to be read.
        guard let provider = calendarProviderName else { return nil }
        return AnyView(
            Text(provider).font(V.Font.caption).foregroundStyle(V.textTertiary))
    }

    private var scheduleSection: some View {
        Card {
            VStack(alignment: .leading, spacing: V.Space.s) {
                SectionBlock(title: "Scheduled automations") {
                    VStack(alignment: .leading, spacing: 0) {
                        if schedules.isEmpty {
                            Text(readFailed
                                 ? "I could not read your schedules, so this is not an empty list."
                                 : "No scheduled automations.")
                                .font(V.Font.body).foregroundStyle(V.textSecondary)
                        } else {
                            ForEach(schedules) { job in
                                VStack(alignment: .leading, spacing: V.Space.s) {
                                    HStack(alignment: .top) {
                                        VStack(alignment: .leading, spacing: 2) {
                                            Text("\(job.name) · \(job.action ?? "")")
                                                .font(V.Font.body).foregroundStyle(V.text)
                                            Text(job.schedule ?? "")
                                                .font(V.Font.caption)
                                                .foregroundStyle(V.textSecondary)
                                            Text("next \(short(job.next_run)) · status \(job.status ?? "?")")
                                                .font(V.Font.caption)
                                                .foregroundStyle(V.textTertiary)
                                            if let err = job.last_error, !err.isEmpty {
                                                Text("last error: \(err)")
                                                    .font(V.Font.caption).foregroundStyle(V.danger)
                                            }
                                        }
                                        Spacer()
                                        HStack(spacing: 2) {
                                            if job.status == "active" {
                                                Button("Pause") { Task { await schedule(job, "pause") } }
                                            } else if job.status == "paused" {
                                                Button("Resume") { Task { await schedule(job, "resume") } }
                                            }
                                            Button("Run now") { Task { await schedule(job, "run") } }
                                            Button("History") { Task { await loadHistory(job) } }
                                            Button("Edit") { beginEdit(job) }
                                            Button("Cancel") { Task { await schedule(job, "cancel") } }
                                        }
                                        .buttonStyle(VQuietButtonStyle())
                                        .font(V.Font.caption)
                                    }

                                    if editingJob == job.schedule_id {
                                        HStack(spacing: V.Space.s) {
                                            VField(placeholder: "name", text: $editName)
                                            VField(placeholder: "every morning at 8", text: $editWhen)
                                            Button("Save") { Task { await saveEdit(job) } }
                                                .buttonStyle(VPrimaryButtonStyle())
                                        }
                                        .font(V.Font.caption)
                                    }

                                    if let runs = histories[job.schedule_id] {
                                        VStack(alignment: .leading, spacing: 2) {
                                            if runs.isEmpty {
                                                Text("No runs yet.")
                                                    .font(V.Font.caption)
                                                    .foregroundStyle(V.textTertiary)
                                            }
                                            ForEach(runs.prefix(8)) { run in
                                                HStack(spacing: V.Space.s) {
                                                    Text(run.outcome ?? "?")
                                                        .font(V.Font.caption)
                                                        .foregroundStyle(outcomeColour(run.outcome))
                                                    Text(short(run.started_at))
                                                        .font(V.Font.caption)
                                                        .foregroundStyle(V.textSecondary)
                                                    if let d = run.detail, !d.isEmpty {
                                                        Text(d)
                                                            .font(V.Font.caption)
                                                            .foregroundStyle(V.textSecondary)
                                                    }
                                                }
                                            }
                                        }
                                        .padding(.leading, V.Space.m)
                                    }
                                }
                                .padding(.vertical, V.Space.s)
                                if job.id != schedules.last?.id {
                                    Divider().overlay(V.divider)
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    // MARK: - Derived

    /// The calendar adapter `/health` reports as active: "google", "caldav", or
    /// "local_calendar". Nil when health has not been read — in which case the
    /// agenda simply does not claim a provider.
    private var calendarProviderName: String? {
        guard let raw = backend.health?.integrations?["calendar"], !raw.isEmpty else {
            return nil
        }
        switch raw {
        case "google": return "Google Calendar"
        case "caldav": return "CalDAV calendar"
        case "local_calendar": return "Local calendar"
        default: return raw
        }
    }

    private var nextTask: APIModels.PlannerTask? {
        guard let raw = agenda?.next_action?.dictionary,
              let taskRaw = raw["task"]?.dictionary else { return nil }
        // Rebuild from the fields we display.
        return APIModels.PlannerTask(
            task_ref: taskRaw["task_ref"]?.stringValue ?? "",
            title: taskRaw["title"]?.stringValue ?? "",
            detail: taskRaw["detail"]?.stringValue,
            status: taskRaw["status"]?.stringValue,
            priority: taskRaw["priority"]?.stringValue,
            due_at: taskRaw["due_at"]?.stringValue,
            start_at: taskRaw["start_at"]?.stringValue,
            estimate_minutes: taskRaw["estimate_minutes"]?.intValue,
            depends_on: nil, project: taskRaw["project"]?.stringValue,
            done_at: nil, evidence: nil,
            overdue: taskRaw["overdue"]?.boolValue,
            blocked: taskRaw["blocked"]?.boolValue)
    }

    private var nextReason: String {
        guard let raw = agenda?.next_action?.dictionary,
              let rule = raw["rule"]?.stringValue else { return "" }
        return "chosen as the \(rule)"
    }

    private func dueShort(_ iso: String) -> String {
        // An unparseable timestamp is shown as the backend sent it rather than
        // as a bare "—", which would look like no due date at all.
        guard ISO8601DateFormatter().date(from: iso) != nil else { return "due \(iso)" }
        return "due " + short(iso)
    }

    private func short(_ iso: String?) -> String {
        guard let iso, let date = ISO8601DateFormatter().date(from: iso) else { return "—" }
        let f = DateFormatter()
        f.dateFormat = "d MMM HH:mm"
        return f.string(from: date)
    }

    private func timeOf(_ iso: String) -> String {
        guard let date = ISO8601DateFormatter().date(from: iso) else { return "" }
        let f = DateFormatter()
        f.dateFormat = "HH:mm"
        return f.string(from: date)
    }

    // MARK: - Actions

    private func load() async {
        // The first load may show a spinner; a refresh keeps what is on screen
        // readable and marks itself in the header, so acting on a task does not
        // make the whole page vanish and come back.
        if tasks.isEmpty && agenda == nil && schedules.isEmpty {
            isLoading = true
        } else {
            isRefreshing = true
        }
        errorMessage = nil
        readFailed = false
        do {
            async let t = api.plannerTasks()
            async let a = api.plannerToday()
            async let s = api.scheduledJobs()
            async let n = api.attention()
            // The assignments used to happen one at a time, so a failure on the
            // second read left the first list populated, every later list
            // empty, and the screen saying "No scheduled automations" next to a
            // banner that said the server could not be reached. Read them all
            // first, then publish them together: either the screen shows what
            // was read, or it says it could not read.
            let (loadedTasks, loadedAgenda, loadedSchedules, loadedAttention) =
                try await (t, a, s, n)
            tasks = loadedTasks
            agenda = loadedAgenda
            schedules = loadedSchedules
            attention = loadedAttention
            lastLoaded = Date()
        } catch {
            errorMessage = error.localizedDescription
            readFailed = true
        }
        isLoading = false
        isRefreshing = false
        // Google is a separate read with its own state. Read after the planner
        // read so a planner failure does not also hide the Google section.
        await loadGoogleTasks()
    }

    // MARK: - Google Tasks (live, not copied)

    /// Google Tasks, read from the account. The brief for this screen is that
    /// tasks are Google's and stay there: this list is a view of Google, not a
    /// second store. Add and complete go straight to Google's routes.
    private var googleTasksSection: some View {
        Card {
            VStack(alignment: .leading, spacing: V.Space.s) {
                SectionBlock(title: "Google Tasks", trailing: AnyView(
                    Group {
                        if googleStatus?.connection.isConnected == true {
                            Text("\(googleVisibleTasks.count) open")
                                .font(V.Font.caption)
                                .foregroundStyle(V.textTertiary)
                        }
                    }
                )) {
                    VStack(alignment: .leading, spacing: V.Space.s) {
                        googleTasksBody

                        if googleStatus?.connection.isConnected == true {
                            HStack(spacing: V.Space.s) {
                                VField(placeholder: "Add a Google task", text: $newGoogleTask,
                                       onSubmit: { Task { await addGoogleTask() } })
                                Button("Add") { Task { await addGoogleTask() } }
                                    .buttonStyle(VSecondaryButtonStyle())
                                    .disabled(newGoogleTask.trimmingCharacters(in: .whitespaces).isEmpty
                                              || googleWorking)
                                if googleWorking { ProgressView().controlSize(.small) }
                            }
                            Text("Added straight to Google Tasks; Vantage keeps no copy.")
                                .font(V.Font.caption)
                                .foregroundStyle(V.textTertiary)
                        }
                        if let googleWriteError, !googleWriteError.isEmpty {
                            Notice(kind: .error, message: googleWriteError)
                        }
                    }
                }
            }
        }
    }

    @ViewBuilder
    private var googleTasksBody: some View {
        if let connection = googleStatus?.connection {
            if !connection.isConnected {
                // Not connected is a state, not an empty list. Say which one,
                // and point at Connections where connecting happens.
                Notice(kind: .info, message: googleDisconnectedLine(connection))
            } else if googleReadFailed {
                VStack(alignment: .leading, spacing: V.Space.xs) {
                    Notice(kind: .error,
                           message: "I could not read your Google tasks, so this is not an empty list.")
                    if let detail = googleDetail, !detail.isEmpty {
                        Text(detail).font(V.Font.caption)
                            .foregroundStyle(V.textSecondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            } else if googleVisibleTasks.isEmpty {
                Text("No open Google tasks.")
                    .font(V.Font.body).foregroundStyle(V.textSecondary)
            } else {
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(googleVisibleTasks) { task in
                        HStack(alignment: .top, spacing: V.Space.s) {
                            Button {
                                guard task.done != true else { return }
                                Task { await completeGoogleTask(task) }
                            } label: {
                                Image(systemName: task.done == true
                                      ? "checkmark.circle.fill" : "circle")
                                    .foregroundStyle(task.done == true ? V.accent : V.textTertiary)
                            }
                            .buttonStyle(.plain)
                            .disabled(task.done == true || googleWorking)
                            .help(task.done == true ? "Already completed" : "Mark completed")
                            VStack(alignment: .leading, spacing: 2) {
                                Text(task.title).font(V.Font.body)
                                    .foregroundStyle(V.text)
                                if let due = task.due {
                                    Text("due \(due)").font(V.Font.caption)
                                        .foregroundStyle(V.textTertiary)
                                }
                            }
                            Spacer()
                        }
                        .padding(.vertical, V.Space.xs + 1)
                        if task.id != googleVisibleTasks.last?.id {
                            Divider().overlay(V.divider)
                        }
                    }
                }
            }
        } else if isLoading || isRefreshing {
            LoadingRow(text: "Reading Google…")
        } else {
            Notice(kind: .error,
                   message: "I could not read the Google connection, so I cannot say whether "
                    + "it is connected.")
        }
    }

    private var googleVisibleTasks: [APIModels.GoogleTask] {
        googleTasks.filter { $0.done != true }
    }

    private func googleDisconnectedLine(_ connection: APIModels.GoogleConnection) -> String {
        switch connection.state {
        case "not_configured":
            return "Google is not set up on this Mac. Open Connections for the setup steps."
        case "disconnected":
            return "Google is not connected. Open Connections to connect it."
        case "expired":
            return "The Google connection expired. Open Connections to reconnect."
        default:
            return "Google is not connected (\(connection.state)). Open Connections for its state."
        }
    }

    private func loadGoogleTasks() async {
        do { googleStatus = try await api.googleStatus() }
        catch {
            // A failed status read is not "not connected": clear the status so
            // the section says it could not ask, rather than inventing a state.
            googleStatus = nil
            googleTasks = []
            googleReadFailed = true
            googleDetail = error.localizedDescription
            return
        }
        guard googleStatus?.connection.isConnected == true else {
            googleTasks = []
            googleReadFailed = false
            googleDetail = nil
            return
        }
        do {
            let reply = try await api.googleTasks()
            if reply.read_succeeded == true {
                googleTasks = reply.tasks ?? []
                googleReadFailed = false
                googleDetail = nil
            } else {
                // The route answered but Google did not; status says why.
                googleTasks = []
                googleReadFailed = true
                googleDetail = reply.detail ?? "Google Tasks could not be read."
            }
        } catch {
            googleTasks = []
            googleReadFailed = true
            googleDetail = error.localizedDescription
        }
    }

    private func addGoogleTask() async {
        let title = newGoogleTask.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !title.isEmpty else { return }
        googleWorking = true
        googleWriteError = nil
        defer { googleWorking = false }
        do {
            let result = try await api.googleTaskCreate(
                APIModels.GoogleTaskCreateRequest(
                    title: title, notes: nil, due: nil, list_id: nil, source: "Vantage Planner"))
            guard result.ok == true else {
                googleWriteError = result.error ?? "Google did not confirm the task was created."
                return
            }
            newGoogleTask = ""
            await loadGoogleTasks()
        } catch {
            googleWriteError = error.localizedDescription
        }
    }

    private func completeGoogleTask(_ task: APIModels.GoogleTask) async {
        googleWorking = true
        googleWriteError = nil
        defer { googleWorking = false }
        do {
            let result = try await api.googleTaskComplete(task.id, listId: task.list_id ?? "")
            guard result.ok == true else {
                googleWriteError = result.error ?? "Google did not confirm the task was completed."
                return
            }
            await loadGoogleTasks()
        } catch {
            googleWriteError = error.localizedDescription
        }
    }

    private func planGoal() async {
        let goal = goalText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !goal.isEmpty else { return }
        do {
            _ = try await api.planGoal(goal)
            goalText = ""
            await load()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func addTask() async {
        let title = newTitle.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !title.isEmpty else { return }
        do {
            _ = try await api.createPlannerTask(title: title, priority: newPriority)
            newTitle = ""
            await load()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func complete(_ task: APIModels.PlannerTask) async {
        do {
            _ = try await api.completePlannerTask(task.task_ref)
            await load()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func outcomeColour(_ outcome: String?) -> Color {
        switch outcome {
        case "executed": return V.accent
        case "failed", "blocked": return V.danger
        default: return V.warning
        }
    }

    private func beginEdit(_ job: APIModels.ScheduledJob) {
        editingJob = job.schedule_id
        editName = job.name
        editWhen = job.schedule ?? ""
    }

    private func saveEdit(_ job: APIModels.ScheduledJob) async {
        do {
            try await api.editSchedule(job.schedule_id, name: editName, schedule: editWhen)
            editingJob = nil
            await load()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func loadHistory(_ job: APIModels.ScheduledJob) async {
        if histories[job.schedule_id] != nil {
            histories[job.schedule_id] = nil
            return
        }
        do {
            histories[job.schedule_id] = try await api.scheduleHistory(job.schedule_id)
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func schedule(_ job: APIModels.ScheduledJob, _ action: String) async {
        do {
            _ = try await api.scheduleAction(job.schedule_id, action: action)
            await load()
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}