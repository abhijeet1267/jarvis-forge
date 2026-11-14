import SwiftUI

/// The Daily Briefing — assembled from what Vantage can actually read.
///
/// It reads once (agenda, tasks, inbox, flow history, schedules, approvals, and
/// the provider state the app already polls), then hands the values to
/// `BriefingBuilder`, which owns every "is this empty or failed" decision. The
/// screen therefore draws three states per section — loaded-empty, loaded, and
/// failed — and a failed section never looks like a clear day.
struct BriefingView: View {

    @ObservedObject private var backend = BackendManager.shared
    @ObservedObject private var capture = CaptureRuntime.shared.store
    @ObservedObject private var flows = FlowRuntime.shared.store
    @ObservedObject private var preferences = PreferenceRuntime.shared.store

    @State private var briefing: Briefing?
    @State private var isLoading = true
    @State private var isRefreshing = false
    @State private var lastLoaded: Date?
    @State private var completing: String?
    @State private var pendingComplete: APIModels.PlannerTask?
    @State private var actionMessage: String?

    private let api = APIClient.shared

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            ScrollView {
                VStack(alignment: .leading, spacing: V.Space.l) {
                    if let actionMessage {
                        Notice(kind: .info, message: actionMessage, actionTitle: "Dismiss") {
                            self.actionMessage = nil
                        }
                    }
                    if isLoading {
                        LoadingRow(text: "Reading your day…")
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, V.Space.xxl)
                    } else if let briefing {
                        headlineCard(briefing)
                        if briefing.allSourcesFailed {
                            Notice(kind: .error,
                                   message: "Every source failed to load, so this is not a clear "
                                     + "day. Check that the backend is running, then refresh.",
                                   actionTitle: "Refresh") { Task { await load() } }
                        }
                        ForEach(orderedSections(briefing)) { section in
                            sectionCard(section)
                        }
                        suggestionsCard(briefing)
                        footer(briefing)
                    }
                }
                .padding(.horizontal, V.Space.gutter)
                .padding(.bottom, V.Space.section)
                .frame(maxWidth: 820, alignment: .leading)
                .frame(maxWidth: .infinity, alignment: .center)
            }
        }
        .background(V.background)
        .task { await load() }
        .onReceive(capture.objectWillChange) { _ in
            // The inbox is local and cheap: a capture added in the palette shows
            // up here without a full refresh.
            if !isLoading { Task { await load() } }
        }
        .vConfirm("Mark this task complete?",
                  message: pendingComplete.map {
                      "“\($0.title)” will be marked done in your planner."
                  } ?? "",
                  confirm: "Mark done",
                  isPresented: Binding(get: { pendingComplete != nil },
                                       set: { if !$0 { pendingComplete = nil } }),
                  destructive: false) {
            if let task = pendingComplete { Task { await complete(task) } }
            pendingComplete = nil
        }
    }

    // MARK: - Header

    private var header: some View {
        PageHeader(title: "Daily Briefing",
                   subtitle: briefing.map { $0.day }
                     ?? "Today, from the sources Vantage can read.") {
            HStack(spacing: V.Space.s) {
                if isRefreshing { ProgressView().controlSize(.small) }
                if let stamp = lastLoaded, !isRefreshing {
                    Text("Generated \(stamp.formatted(date: .omitted, time: .shortened))")
                        .font(V.Font.caption).foregroundStyle(V.textTertiary)
                }
                Button {
                    Task { await load(refresh: true) }
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
        .frame(maxWidth: 820)
        .frame(maxWidth: .infinity, alignment: .center)
    }

    // MARK: - Cards

    private func headlineCard(_ briefing: Briefing) -> some View {
        Card(tone: briefing.allSourcesFailed ? .danger : .ivory) {
            VStack(alignment: .leading, spacing: V.Space.s) {
                HStack(spacing: V.Space.s) {
                    Image(systemName: "sun.max")
                        .font(.system(size: 14, weight: .light))
                        .foregroundStyle(V.gold)
                    Text(briefing.headline)
                        .font(V.Font.message)
                        .foregroundStyle(V.text)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if !briefing.failedSources.isEmpty {
                    Label("Could not read: " + briefing.failedSources.joined(separator: ", "),
                          systemImage: "exclamationmark.triangle")
                        .font(V.Font.caption)
                        .foregroundStyle(V.warning)
                }
                if !briefing.unavailableSources.isEmpty {
                    Label("Not configured: " + briefing.unavailableSources.joined(separator: ", "),
                          systemImage: "minus.circle")
                        .font(V.Font.caption)
                        .foregroundStyle(V.textTertiary)
                }
            }
        }
    }

    /// The sections in the person's preferred order, but only *reordered*: a
    /// preference cannot hide a section.
    private func orderedSections(_ briefing: Briefing) -> [Briefing.Section<Briefing.Card>] {
        let order = preferences.briefingOrder()
        return briefing.sections.sorted {
            (order.firstIndex(of: $0.id) ?? Int.max) < (order.firstIndex(of: $1.id) ?? Int.max)
        }
    }

    private func sectionCard(_ section: Briefing.Section<Briefing.Card>) -> some View {
        let failed: Bool = {
            if case .failed = section.state { return true }
            return false
        }()
        return Card(tone: failed ? .warning : .plain) {
            VStack(alignment: .leading, spacing: V.Space.s) {
                HStack(spacing: V.Space.s) {
                    Image(systemName: section.symbol)
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(V.textSecondary)
                    Text(section.title)
                        .font(V.Font.sectionTitle).foregroundStyle(V.text)
                    Spacer()
                    if section.isLoaded, !section.items.isEmpty {
                        Text("\(section.items.count)")
                            .font(V.Font.caption)
                            .foregroundStyle(V.textTertiary)
                    }
                }
                switch section.state {
                case .failed(let why):
                    Label(why, systemImage: "exclamationmark.octagon")
                        .font(V.Font.body)
                        .foregroundStyle(V.text)
                        .fixedSize(horizontal: false, vertical: true)
                case .unavailable(let why):
                    Label(why, systemImage: "minus.circle")
                        .font(V.Font.body)
                        .foregroundStyle(V.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                case .loaded:
                    if section.items.isEmpty {
                        Text(section.emptyMessage)
                            .font(V.Font.body).foregroundStyle(V.textSecondary)
                    } else {
                        VStack(spacing: 0) {
                            ForEach(section.items) { card in
                                row(card)
                                if card.id != section.items.last?.id {
                                    Divider().overlay(V.divider)
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    /// One briefing row. The row itself is never a button — the action is a named
    /// control, so a stray click cannot complete a task.
    private func row(_ card: Briefing.Card) -> some View {
        HStack(alignment: .top, spacing: V.Space.s) {
            Image(systemName: symbol(for: card))
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(colour(for: card))
                .frame(width: 14)
                .padding(.top, 2)
            VStack(alignment: .leading, spacing: 2) {
                Text(card.title)
                    .font(V.Font.body).foregroundStyle(V.text)
                    .fixedSize(horizontal: false, vertical: true)
                if !card.detail.isEmpty {
                    Text(card.detail)
                        .font(V.Font.caption).foregroundStyle(V.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if !card.dateLabel.isEmpty {
                    Text(card.dateLabel)
                        .font(V.Font.caption).foregroundStyle(V.textTertiary)
                }
                if card.isSuggestion {
                    Text("Suggestion")
                        .font(.system(size: 9, weight: .semibold))
                        .foregroundStyle(V.gold)
                        .padding(.horizontal, 5).padding(.vertical, 1)
                        .background(V.goldSoft, in: Capsule())
                }
            }
            Spacer(minLength: V.Space.s)
            actionButton(for: card.action)
        }
        .padding(.vertical, V.Space.s)
    }

    @ViewBuilder
    private func actionButton(for action: Briefing.Card.Action?) -> some View {
        switch action {
        case .openTask(let ref):
            HStack(spacing: V.Space.xs) {
                Button("Open") { openTask(ref) }
                    .buttonStyle(VQuietButtonStyle(tint: V.textSecondary))
                Button("Done") {
                    if let task = currentTasks.first(where: { $0.task_ref == ref }) {
                        pendingComplete = task
                    } else {
                        actionMessage = "That task is no longer in the list. Refresh to see the "
                            + "current one."
                    }
                }
                .buttonStyle(VQuietButtonStyle(tint: V.accent))
            }
        case .openCapture(let id):
            Button("Review") {
                AppNavigation.shared.go(.capture, captureID: id)
            }
            .buttonStyle(VQuietButtonStyle(tint: V.accent))
        case .openFlow(let id):
            Button("Review run") {
                AppNavigation.shared.go(.flows, flowID: id)
            }
            .buttonStyle(VQuietButtonStyle(tint: V.accent))
        case .openFlowHistory:
            Button("Flows") { AppNavigation.shared.go(.flows) }
                .buttonStyle(VQuietButtonStyle(tint: V.accent))
        case .openScreen(let destination):
            Button(destination == .approvals ? "Review" : "Open") {
                AppNavigation.shared.go(destination)
            }
            .buttonStyle(VQuietButtonStyle(tint: V.accent))
        case nil:
            EmptyView()
        @unknown default:
            EmptyView()
        }
    }

    private func suggestionsCard(_ briefing: Briefing) -> some View {
        Group {
            if !briefing.suggestions.isEmpty {
                Card(tone: .gold) {
                    VStack(alignment: .leading, spacing: V.Space.s) {
                        HStack(spacing: V.Space.s) {
                            Text("Suggested next actions")
                                .font(V.Font.sectionTitle).foregroundStyle(V.text)
                            Text("Suggestions")
                                .font(.system(size: 9, weight: .semibold))
                                .foregroundStyle(V.gold)
                                .padding(.horizontal, 5).padding(.vertical, 1)
                                .background(V.goldSoft, in: Capsule())
                        }
                        Text("These are derived from counts in your own data, not from a model "
                             + "guessing your priorities.")
                            .font(V.Font.caption).foregroundStyle(V.textTertiary)
                        ForEach(Array(briefing.suggestions.enumerated()), id: \.offset) { _, text in
                            HStack(alignment: .firstTextBaseline, spacing: V.Space.s) {
                                Image(systemName: "arrow.right")
                                    .font(.system(size: 9, weight: .semibold))
                                    .foregroundStyle(V.gold)
                                Text(text)
                                    .font(V.Font.body).foregroundStyle(V.text)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                    }
                }
            }
        }
    }

    private func footer(_ briefing: Briefing) -> some View {
        VStack(alignment: .leading, spacing: V.Space.xs) {
            Text("Generated \(briefing.generatedAt.formatted(date: .abbreviated, time: .standard)). "
                 + "Each section shows when its source was read; a source that failed says so "
                 + "instead of looking empty.")
                .font(V.Font.caption).foregroundStyle(V.textTertiary)
                .fixedSize(horizontal: false, vertical: true)
            Text("Scheduled briefings are not part of this phase — this briefing is on demand.")
                .font(V.Font.caption).foregroundStyle(V.textTertiary)
        }
    }

    // MARK: - Symbols

    private func symbol(for card: Briefing.Card) -> String {
        switch card.kind {
        case .event: return "calendar"
        case .taskDue: return "clock"
        case .taskOverdue: return "exclamationmark.circle.fill"
        case .taskUpcoming: return "calendar.badge.clock"
        case .captureAwaitingReview: return "tray"
        case .flowFailure, .scheduleFailure: return "xmark.octagon.fill"
        case .approval: return "checkmark.shield.fill"
        case .providerIssue: return "key.fill"
        }
    }

    private func colour(for card: Briefing.Card) -> Color {
        switch card.kind {
        case .taskOverdue, .flowFailure, .scheduleFailure: return V.danger
        case .approval: return V.gold
        case .providerIssue: return V.warning
        case .captureAwaitingReview: return V.accent
        default: return V.textTertiary
        }
    }

    // MARK: - Load

    private var currentTasks: [APIModels.PlannerTask] { tasksCache }

    @State private var tasksCache: [APIModels.PlannerTask] = []

    private func load(refresh: Bool = false) async {
        if refresh { isRefreshing = true } else if briefing == nil { isLoading = true }
        defer { isLoading = false; isRefreshing = false }

        var inputs = BriefingBuilder.Inputs()

        // Agenda: the one read that carries calendar events, due/overdue tasks and
        // the calendar's own failure flag.
        do {
            inputs.agenda = try await api.plannerToday()
        } catch {
            inputs.agendaError = "Today's agenda could not be read (\(error.localizedDescription))."
        }

        // The flat task list, for "coming up" and to cross-check due/overdue.
        do {
            let tasks = try await api.plannerTasks()
            tasksCache = tasks
            inputs.tasks = tasks
        } catch {
            inputs.tasksError = "Your tasks could not be read (\(error.localizedDescription))."
        }

        // Local inbox.
        inputs.captureItems = capture.items
        // Local flow history.
        inputs.flowHistory = flows.history

        // Schedules and approvals.
        do {
            inputs.schedules = try await api.scheduledJobs()
        } catch {
            inputs.schedulesError = "Scheduled jobs could not be read "
                + "(\(error.localizedDescription))."
        }
        do {
            inputs.approvals = try await api.pendingConfirmations()
        } catch {
            inputs.approvalsError = "Approvals could not be read (\(error.localizedDescription))."
        }

        // Provider health, only when it is a real problem.
        if let health = backend.health, health.llm_enabled == false {
            inputs.providerIssue = health.llm_source == "none"
                ? "No AI provider is configured. Conversational answers and document summaries "
                  + "are unavailable; Mac actions still work."
                : "The AI provider (\(health.ai_provider ?? "unknown")) is not answering."
        }

        briefing = BriefingBuilder().build(inputs)
        lastLoaded = Date()
    }

    private func openTask(_ ref: String) {
        // The planner owns tasks; there is no per-task detail screen, so opening
        // means showing the list it lives in.
        AppNavigation.shared.go(.planner, prefill: ref)
    }

    private func complete(_ task: APIModels.PlannerTask) async {
        completing = task.task_ref
        defer { completing = nil }
        do {
            _ = try await api.completePlannerTask(task.task_ref)
            actionMessage = "Marked “\(task.title)” done."
            await load(refresh: true)
        } catch {
            actionMessage = "Could not complete “\(task.title)”: \(error.localizedDescription)"
        }
    }
}