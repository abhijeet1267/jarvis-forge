import SwiftUI
import AppKit

/// Vantage Flows — saved sequences of real actions.
///
/// Three panes in one screen rather than three windows: a list of saved flows, an
/// editor for the selected one, and the run history. That keeps the shared store
/// and engine visible in one place, which is also where approvals land.
///
/// Buttons are only enabled when the thing they do is possible: Run is disabled
/// while a run is in flight, per-step fields are limited to the arguments that
/// action actually takes, and the editor refuses to save a flow with an unknown
/// action rather than storing something unrunnable.
struct FlowsView: View {

    @ObservedObject private var store = FlowStore.shared
    @ObservedObject private var engine = FlowRuntime.shared.engine
    @ObservedObject private var control = MacControlCoordinator.shared

    @State private var selection: UUID?
    @State private var draft: Flow?
    @State private var isNew = false
    @State private var confirmDelete = false
    @State private var pendingRetry: FlowRun?
    @State private var describeText = ""
    @State private var showHistory = true
    @State private var saveNote: String?

    /// A flow drafted by another feature (a capture item, a document's action
    /// list). Consumed once and shown as an *unsaved* draft; the screen never
    /// saves or runs it on the sender's behalf.
    @Binding var handoff: Flow?
    /// A flow whose history row a briefing card asked to review.
    static var pendingFocus: UUID?

    private let authoring: FlowAuthoring

    init(handoff: Binding<Flow?> = .constant(nil)) {
        _handoff = handoff
        authoring = FlowAuthoring(applications: MacControlCoordinator.shared.installedApplications)
    }

    var body: some View {
        HStack(spacing: 0) {
            listPane
                .frame(width: 250)
            Rectangle().fill(V.divider).frame(width: 1)
            detailPane
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .background(V.background)
        .task {
            // The palette and Command Center route approvals here.
            FlowRuntime.shared.attach { selection = draft?.id ?? store.flows.first?.id }
            control.refreshPermissions()
            control.refreshApplications()
            selectFirstIfNeeded()
            consumeHandoff()
        }
        .onChange(of: handoff?.id) { _ in consumeHandoff() }
        .vConfirm("Delete this flow?",
                  message: "“\(draft?.name ?? "")” will be removed from your saved flows. "
                    + "Run history is kept, so previous runs stay readable.",
                  confirm: "Delete",
                  isPresented: $confirmDelete) {
            if let flow = draft { _ = store.delete(flow) }
            draft = nil
            selection = nil
        }
        .vConfirm("Run it again?",
                  message: retryMessage,
                  confirm: "Run again",
                  isPresented: Binding(get: { pendingRetry != nil },
                                       set: { if !$0 { pendingRetry = nil } }),
                  destructive: false) {
            if let run = pendingRetry, let flow = store.flows.first(where: { $0.id == run.flowID }) {
                pendingRetry = nil
                Task { await engine.run(flow) }
            }
        }
    }

    /// A row must never be highlighted without its editor loaded.
    ///
    /// Measured in a render: the sidebar showed the seeded flow with the
    /// selection tint while the detail pane still said "No flow selected",
    /// because `selection` and `draft` were set independently. Keeping them in
    /// one step removes that state entirely.
    private func selectFirstIfNeeded() {
        if draft == nil, let first = store.flows.first {
            selection = first.id
            draft = first
        } else if let id = selection, draft == nil,
                  let selected = store.flows.first(where: { $0.id == id }) {
            draft = selected
        }
    }

    /// Adopt a draft another screen produced, and a flow a briefing card asked to
    /// inspect. Both are read-only handoffs: nothing is saved or run here.
    private func consumeHandoff() {
        if let incoming = handoff {
            // A new identity, so accepting the draft cannot overwrite the flow it
            // was drafted from.
            var draft = Flow(name: incoming.name, description: incoming.description,
                             steps: incoming.steps, failurePolicy: incoming.failurePolicy)
            if let policy = PreferenceRuntime.shared.store.defaultFlowPolicy(),
               incoming.failurePolicy == .stop {
                // The preference only supplies the default for a *new* draft; it
                // never overrides a policy the draft already carries.
                draft.failurePolicy = policy
            }
            self.draft = draft
            selection = draft.id
            isNew = true
            showHistory = false
            saveNote = "Review this drafted flow, then Save. It has not been saved or run."
            handoff = nil
            Self.pendingFocus = nil
            return
        }
        if let id = Self.pendingFocus {
            Self.pendingFocus = nil
            if let flow = store.flows.first(where: { $0.id == id }) {
                selection = flow.id
                draft = flow
                isNew = false
                showHistory = true
            }
        }
    }

    private var retryMessage: String {
        guard let run = pendingRetry else { return "" }
        return engine.retrySafety(for: run).reason
    }

    // MARK: - List

    private var listPane: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text("FLOWS").font(V.Font.caption.weight(.semibold)).tracking(0.6)
                    .foregroundStyle(V.textTertiary)
                Spacer()
                Button {
                    newFlow()
                } label: {
                    Image(systemName: "plus")
                }
                .buttonStyle(.plain)
                .foregroundStyle(V.accent)
                .help("New flow")
            }
            .padding(.horizontal, V.Space.l)
            .padding(.top, V.Space.l)
            .padding(.bottom, V.Space.s)

            ScrollView {
                LazyVStack(alignment: .leading, spacing: 2) {
                    ForEach(store.flows) { flow in
                        Button {
                            select(flow)
                        } label: {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(flow.name).font(V.Font.body)
                                    .foregroundStyle(V.text).lineLimit(1)
                                Text("\(flow.steps.count) steps")
                                    .font(V.Font.caption).foregroundStyle(V.textTertiary)
                            }
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.horizontal, V.Space.m)
                            .padding(.vertical, 7)
                            .background(selection == flow.id ? V.accentSoft : Color.clear,
                                        in: RoundedRectangle(cornerRadius: 6))
                        }
                        .buttonStyle(.plain)
                    }
                    if store.flows.isEmpty {
                        Text("No flows yet.\nCreate one below, or describe it: “create a flow that opens Safari”.")
                            .font(V.Font.caption).foregroundStyle(V.textTertiary)
                            .fixedSize(horizontal: false, vertical: true)
                            .padding(V.Space.m)
                    }
                }
                .padding(.horizontal, V.Space.s)
            }

            Divider().overlay(V.divider)
            Button {
                showHistory.toggle()
            } label: {
                Label(showHistory ? "Hide history" : "Show history (\(store.history.count))",
                      systemImage: "clock.arrow.circlepath")
                    .font(V.Font.caption)
            }
            .buttonStyle(.plain)
            .foregroundStyle(V.textSecondary)
            .padding(V.Space.m)
        }
        .background(V.surfaceAlt)
    }

    // MARK: - Detail

    private var detailPane: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: V.Space.section) {
                if let draft {
                    editor(draft)
                    if showHistory { historySection }
                } else {
                    EmptyState(symbol: "square.stack.3d.up",
                               title: "No flow selected",
                               message: "Choose a flow on the left, or describe one: "
                                 + "“create a Start Workday flow that opens Safari, Finder "
                                 + "and my Documents folder, then sets volume to 25 percent”.",
                               actionTitle: "New flow",
                               action: { newFlow() })
                }
            }
            .padding(.horizontal, V.Space.gutter)
            .padding(.vertical, V.Space.section)
            .frame(maxWidth: 820)
            .frame(maxWidth: .infinity, alignment: .center)
        }
    }

    private func editor(_ flow: Flow) -> some View {
        VStack(alignment: .leading, spacing: V.Space.section) {
            header(flow)
            if let saveNote {
                Notice(kind: .info, message: saveNote, actionTitle: "Dismiss") {
                    self.saveNote = nil
                }
            }
            if let live = engine.live, live.flowName == flow.name {
                runStatus(live)
            }
            if let pending = engine.pendingApproval, pending.flow.id == flow.id {
                approvalCard(pending)
            }
            let problems = flow.validate()
            if !problems.isEmpty {
                Notice(kind: .error,
                       message: problems.map(\.message).joined(separator: " "))
            }
            stepsSection(flow)
            policySection(flow)
            describeSection
        }
    }

    private func header(_ flow: Flow) -> some View {
        VStack(alignment: .leading, spacing: V.Space.m) {
            HStack(alignment: .firstTextBaseline, spacing: V.Space.s) {
                TextField("Flow name", text: Binding(
                    get: { draft?.name ?? flow.name },
                    set: { draft?.name = $0 }))
                    .textFieldStyle(.plain)
                    .font(V.Font.pageTitle)
                    .foregroundStyle(V.text)
                Spacer(minLength: V.Space.s)
                Button("Save") { saveDraft() }
                    .buttonStyle(VPrimaryButtonStyle())
                    .disabled(engine.isRunning || flow.validate().count > 0)
                Menu {
                    Button("Run") { runDraft() }
                    Button("Duplicate") { duplicate() }
                    Button("Delete…", role: .destructive) { confirmDelete = true }
                } label: {
                    Image(systemName: "ellipsis.circle.fill")
                }
                .menuStyle(.borderlessButton)
                .frame(width: 30)
            }
            TextField("Description (optional)", text: Binding(
                get: { draft?.description ?? "" },
                set: { draft?.description = $0 }))
                .textFieldStyle(.plain)
                .font(V.Font.body)
                .foregroundStyle(V.textSecondary)

            HStack(spacing: V.Space.s) {
                Button {
                    runDraft()
                } label: {
                    Label(engine.isRunning ? "Running…" : "Run flow",
                          systemImage: "play.fill")
                }
                .buttonStyle(VPrimaryButtonStyle())
                .disabled(engine.isRunning || flow.validate().count > 0 || engine.pendingApproval != nil)

                if engine.isRunning {
                    Button("Stop") { engine.cancel() }
                        .buttonStyle(VSecondaryButtonStyle())
                }
                if let run = engine.lastRun, run.flowID == flow.id {
                    Button("Run again") { requestRetry(run) }
                        .buttonStyle(VSecondaryButtonStyle())
                        .disabled(engine.isRunning)
                }
                if !control.permissions.permits(nil) {
                    EmptyView()
                }
                Spacer(minLength: 0)
                Text("\(flow.steps.count) steps · \(flow.failurePolicy == .stop ? "stops at the first failure" : "continues past safe steps")")
                    .font(V.Font.caption).foregroundStyle(V.textTertiary)
            }
        }
    }

    @ViewBuilder
    private func runStatus(_ live: FlowEngine.Live) -> some View {
        Card(tone: live.phase == .failed ? .danger
                : (live.phase == .completed ? .plain : .ivory)) {
            VStack(alignment: .leading, spacing: V.Space.s) {
                HStack(spacing: V.Space.s) {
                    if live.phase == .running || live.phase == .preparing || live.phase == .cancelling {
                        ProgressView().controlSize(.small)
                    }
                    Text(phaseLabel(live.phase))
                        .font(V.Font.label).foregroundStyle(V.text)
                    Spacer()
                    if live.totalSteps > 0 {
                        Text("\(live.outcomes.count)/\(live.totalSteps)")
                            .font(V.Font.mono).foregroundStyle(V.textSecondary)
                    }
                }
                if !live.currentTitle.isEmpty {
                    Text(live.currentTitle)
                        .font(V.Font.body).foregroundStyle(V.textSecondary)
                }
                Text(live.note)
                    .font(V.Font.caption).foregroundStyle(V.textTertiary)
                    .fixedSize(horizontal: false, vertical: true)
                ForEach(live.outcomes) { outcome in
                    outcomeRow(outcome)
                }
            }
        }
    }

    private func phaseLabel(_ phase: FlowEngine.Live.Phase) -> String {
        switch phase {
        case .preparing: return "Checking steps"
        case .awaitingApproval: return "Waiting for approval"
        case .running: return "Running"
        case .cancelling: return "Stopping"
        case .completed: return "Completed"
        case .completedWithErrors: return "Completed with errors"
        case .cancelled: return "Cancelled"
        case .failed: return "Stopped"
        }
    }

    private func approvalCard(_ pending: FlowEngine.PendingApproval) -> some View {
        Card(tone: .gold) {
            VStack(alignment: .leading, spacing: V.Space.s) {
                Text("This step needs your approval")
                    .font(V.Font.cardTitle).foregroundStyle(V.text)
                Text(pending.summary)
                    .font(V.Font.body).foregroundStyle(V.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
                Text("Steps after it have not run yet.")
                    .font(V.Font.caption).foregroundStyle(V.textTertiary)
                HStack(spacing: V.Space.s) {
                    Button("Run this step") {
                        Task { await engine.approvePending() }
                    }
                    .buttonStyle(VPrimaryButtonStyle())
                    Button("Cancel flow") { engine.cancelPending() }
                        .buttonStyle(VSecondaryButtonStyle())
                }
            }
        }
    }

    @ViewBuilder
    private func outcomeRow(_ outcome: FlowStepOutcome) -> some View {
        let colour: Color = {
            switch outcome.resultStatus {
            case .verified: return V.accent
            case .unverified: return V.warning
            case .refused: return V.textTertiary
            case .failed: return V.danger
            case .cancelled: return V.textTertiary
            }
        }()
        HStack(alignment: .firstTextBaseline, spacing: V.Space.s) {
            Image(systemName: outcome.resultStatus == .verified ? "checkmark.circle.fill"
                    : (outcome.resultStatus == .unverified ? "questionmark.circle.fill"
                       : "xmark.circle.fill"))
                .font(.system(size: 10, weight: .semibold))
                .foregroundStyle(colour)
            VStack(alignment: .leading, spacing: 1) {
                Text(outcome.title).font(V.Font.label).foregroundStyle(V.text)
                Text(outcome.message)
                    .font(V.Font.caption).foregroundStyle(V.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 0)
            Text("\(outcome.durationMS) ms")
                .font(V.Font.mono).foregroundStyle(V.textTertiary)
        }
    }

    // MARK: - Steps

    private func stepsSection(_ flow: Flow) -> some View {
        SectionBlock(title: "Steps", trailing: AnyView(
            Menu {
                ForEach(MacActionKind.allCases, id: \.self) { kind in
                    if let policy = MacActionPolicy.forKind(kind) {
                        Button("\(kind.displayName) — \(policy.risk.displayName)") {
                            addStep(kind)
                        }
                    }
                }
            } label: {
                Label("Add step", systemImage: "plus")
                    .font(V.Font.caption)
            }
            .menuStyle(.borderlessButton)
            .frame(width: 100)
        )) {
            VStack(spacing: V.Space.s) {
                ForEach(Array((draft ?? flow).steps.enumerated()), id: \.element.id) { index, step in
                    stepCard(step, index: index)
                }
                if (draft ?? flow).steps.isEmpty {
                    Card {
                        Text("No steps yet. Add one with the menu above.")
                            .font(V.Font.body).foregroundStyle(V.textSecondary)
                    }
                }
            }
        }
    }

    private func stepCard(_ step: FlowStep, index: Int) -> some View {
        let policy = step.kind.flatMap(MacActionPolicy.forKind)
        return Card {
            VStack(alignment: .leading, spacing: V.Space.s) {
                HStack(spacing: V.Space.s) {
                    Text("\(index + 1)")
                        .font(V.Font.mono).foregroundStyle(V.textTertiary)
                        .frame(width: 16, alignment: .trailing)
                    Text(step.title).font(V.Font.cardTitle).foregroundStyle(V.text)
                    if let policy {
                        Text(policy.risk.displayName)
                            .font(V.Font.caption)
                            .foregroundStyle(policy.risk.requiresConfirmation ? V.gold : V.textTertiary)
                            .padding(.horizontal, 6).padding(.vertical, 1)
                            .background(policy.risk.requiresConfirmation ? V.goldSoft : V.fill,
                                        in: Capsule())
                    }
                    Spacer(minLength: V.Space.s)
                    // Reorder and remove act on the draft, not the saved copy,
                    // so nothing is destroyed until Save.
                    Button { moveStep(index, by: -1) } label: { Image(systemName: "arrow.up") }
                        .buttonStyle(.plain).disabled(index == 0)
                    Button { moveStep(index, by: 1) } label: { Image(systemName: "arrow.down") }
                        .buttonStyle(.plain).disabled(index == (draft ?? Flow(name: "")).steps.count - 1)
                    Button { removeStep(index) } label: { Image(systemName: "minus.circle") }
                        .buttonStyle(.plain).foregroundStyle(V.danger)
                }
                argumentFields(step, index: index)
                if let policy, policy.risk.requiresConfirmation {
                    Text("This step asks for approval when the flow reaches it.")
                        .font(V.Font.caption).foregroundStyle(V.gold)
                }
                if step.kind == nil {
                    Text("Not supported by this build — remove it or replace it.")
                        .font(V.Font.caption).foregroundStyle(V.danger)
                }
            }
        }
    }

    /// Only the arguments this action actually takes, so an invalid field cannot
    /// even be typed. The router still validates the values before running.
    @ViewBuilder
    private func argumentFields(_ step: FlowStep, index: Int) -> some View {
        let binding = Binding<FlowStep>(
            get: { (draft ?? Flow(name: "")).steps[safe: index] ?? step },
            set: { updated in
                guard var flow = draft, flow.steps.indices.contains(index) else { return }
                flow.steps[index] = updated
                draft = flow
            })
        switch step.kind {
        case .openApplication, .activateApplication, .quitApplication:
            appField(binding, label: "Application")
        case .minimizeWindow, .closeWindow, .focusWindow, .listWindows:
            appField(binding, label: "Application (blank = frontmost)")
        case .openFolder, .openFile:
            VField(label: "Folder or path", placeholder: "Downloads",
                   text: binding.arguments.path)
        case .openURL:
            VField(label: "Web address", placeholder: "https://example.com",
                   text: binding.arguments.url)
        case .setVolume:
            HStack(spacing: V.Space.s) {
                Text("Level").font(V.Font.label).foregroundStyle(V.textSecondary)
                Slider(value: Binding(
                    get: { Double(binding.wrappedValue.arguments.percent ?? 50) },
                    set: { binding.wrappedValue.arguments.percent = Int($0.rounded()) }),
                       in: 0...100, step: 1)
                    .frame(maxWidth: 220)
                Text("\(binding.wrappedValue.arguments.percent ?? 50)%")
                    .font(V.Font.mono).foregroundStyle(V.textSecondary)
            }
        case .setMuted:
            Picker("State", selection: Binding(
                get: { binding.wrappedValue.arguments.flag ?? true },
                set: { binding.wrappedValue.arguments.flag = $0 })) {
                Text("Mute").tag(true)
                Text("Unmute").tag(false)
            }
            .pickerStyle(.segmented)
            .frame(maxWidth: 200)
        case .arrangeWindow:
            Picker("Side", selection: Binding(
                get: { binding.wrappedValue.arguments.side ?? .left },
                set: { binding.wrappedValue.arguments.side = $0 })) {
                ForEach(MacWindowSide.allCases, id: \.self) { Text($0.displayName).tag($0) }
            }
            .pickerStyle(.segmented)
            .frame(maxWidth: 380)
        case .openSystemSettings:
            Picker("Page", selection: binding.arguments.settingsPane) {
                Text("Privacy").tag("")
                Text("Accessibility").tag("accessibility")
                Text("Automation").tag("automation")
                Text("Sound").tag("sound")
                Text("Displays").tag("displays")
            }
            .frame(maxWidth: 240)
        case .listRunningApps, .frontmostApp, .systemInfo, .getVolume,
             .googleAgenda, .googleOverdueTasks, .openToday,
             .readClipboard, .displays, .audioDevices, .currentTime:
            Text("No arguments.").font(V.Font.caption).foregroundStyle(V.textTertiary)
        case .writeClipboard, .postNotification:
            VStack(alignment: .leading, spacing: V.Space.s) {
                VField(label: step.kind == .postNotification ? "Title" : nil,
                       placeholder: "Message", text: binding.arguments.notificationTitle)
                VField(label: "Text", placeholder: "The text to copy or show",
                       text: binding.arguments.text)
            }
        case .moveFile:
            VStack(alignment: .leading, spacing: V.Space.s) {
                VField(label: "File", placeholder: "~/Downloads/report.pdf",
                       text: binding.arguments.path)
                VField(label: "Destination", placeholder: "~/Documents/report.pdf",
                       text: binding.arguments.destination)
            }
        case .renameFile:
            HStack(spacing: V.Space.s) {
                VField(label: "File", placeholder: "~/Downloads/report.pdf",
                       text: binding.arguments.path)
                VField(label: "New name", placeholder: "final.pdf",
                       text: binding.arguments.newName)
            }
        case .deleteFile:
            VField(label: "File (moved to the Trash)", placeholder: "~/Downloads/old.pdf",
                   text: binding.arguments.path)
        case .googleCreateTask:
            VStack(alignment: .leading, spacing: V.Space.s) {
                VField(label: "Task title", placeholder: "Submit the report",
                       text: binding.arguments.text)
                Text("The task is created in Google Tasks, on the connected account. "
                     + "Running the flow asks for your approval first.")
                    .font(V.Font.caption).foregroundStyle(V.textTertiary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        case .googleCreateEvent:
            VStack(alignment: .leading, spacing: V.Space.s) {
                VField(label: "Event title", placeholder: "Review meeting",
                       text: binding.arguments.title)
                VField(label: "Start", placeholder: "tomorrow at 10 AM",
                       text: binding.arguments.startDate)
                HStack(spacing: V.Space.s) {
                    Text("Minutes").font(V.Font.label).foregroundStyle(V.textSecondary)
                    VField(label: nil, placeholder: "60",
                           text: Binding(
                            get: { binding.wrappedValue.arguments.durationMinutes.map(String.init) ?? "" },
                            set: { binding.wrappedValue.arguments.durationMinutes = Int($0) }))
                        .frame(width: 80)
                    VField(label: "Location", placeholder: "(optional)",
                           text: binding.arguments.location)
                }
                Text("Running the flow asks for your approval before the event is written. "
                     + "A read-only calendar refuses the write and says so.")
                    .font(V.Font.caption).foregroundStyle(V.textTertiary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        case nil:
            EmptyView()
        }
    }

    private func appField(_ binding: Binding<FlowStep>, label: String) -> some View {
        HStack(spacing: V.Space.s) {
            VField(label: label, placeholder: "Safari", text: binding.arguments.application)
            if !control.installedApplications.isEmpty {
                Menu {
                    ForEach(control.installedApplications.prefix(40), id: \.self) { name in
                        Button(name) { binding.wrappedValue.arguments.application = name }
                    }
                } label: { Image(systemName: "chevron.down") }
                    .menuStyle(.borderlessButton)
                    .frame(width: 22)
            }
        }
    }

    // MARK: - Policy and describe

    private func policySection(_ flow: Flow) -> some View {
        SectionBlock(title: "On failure") {
            Card {
                Picker("", selection: Binding(
                    get: { draft?.failurePolicy ?? flow.failurePolicy },
                    set: { draft?.failurePolicy = $0 })) {
                    ForEach(FlowFailurePolicy.allCases, id: \.self) { policy in
                        Text(policy.displayName).tag(policy)
                    }
                }
                .pickerStyle(.radioGroup)
                .labelsHidden()
            }
        }
    }

    private var describeSection: some View {
        SectionBlock(title: "Describe a flow") {
            Card {
                VStack(alignment: .leading, spacing: V.Space.s) {
                    HStack(spacing: V.Space.s) {
                        VField(placeholder: "Describe the flow…",
                               text: $describeText)
                        Button("Draft") { draftFromText() }
                            .buttonStyle(VSecondaryButtonStyle())
                            .disabled(describeText.trimmingCharacters(in: .whitespaces).isEmpty)
                    }
                    Text("This builds an editable draft. Nothing is saved or run until you "
                         + "press Save and then Run.")
                        .font(V.Font.caption).foregroundStyle(V.textTertiary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }

    // MARK: - History

    private var historySection: some View {
        SectionBlock(title: "Recent runs", trailing: AnyView(
            Button("Clear") { store.clearHistory() }
                .buttonStyle(.plain)
                .font(V.Font.caption)
                .foregroundStyle(V.textSecondary)
                .disabled(store.history.isEmpty)
        )) {
            Card {
                if store.history.isEmpty {
                    Text("No runs yet.")
                        .font(V.Font.body).foregroundStyle(V.textSecondary)
                } else {
                    VStack(spacing: 0) {
                        ForEach(store.history) { run in
                            historyRow(run)
                            if run.id != store.history.last?.id {
                                Divider().overlay(V.divider)
                            }
                        }
                    }
                }
            }
        }
    }

    private func historyRow(_ run: FlowRun) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: V.Space.s) {
            Image(systemName: symbol(run.state))
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(colour(run.state))
                .frame(width: 14)
            VStack(alignment: .leading, spacing: 2) {
                Text(run.flowName).font(V.Font.label).foregroundStyle(V.text)
                Text(run.stopReason.isEmpty
                     ? "\(run.verifiedCount)/\(run.stepCount) steps verified"
                     : run.stopReason)
                    .font(V.Font.caption).foregroundStyle(V.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: V.Space.s)
            VStack(alignment: .trailing, spacing: 2) {
                Text(run.startedAt.formatted(date: .abbreviated, time: .shortened))
                    .font(V.Font.caption).foregroundStyle(V.textTertiary)
                Text("\(run.durationMS) ms")
                    .font(V.Font.mono).foregroundStyle(V.textTertiary)
            }
        }
        .padding(.vertical, V.Space.s)
    }

    private func symbol(_ state: FlowRun.State) -> String {
        switch state {
        case .completed: return "checkmark.circle.fill"
        case .completedWithErrors: return "questionmark.circle.fill"
        case .cancelled: return "slash.circle.fill"
        case .failed: return "xmark.octagon.fill"
        case .running: return "clock"
        }
    }

    private func colour(_ state: FlowRun.State) -> Color {
        switch state {
        case .completed: return V.accent
        case .completedWithErrors: return V.warning
        case .cancelled, .running: return V.textTertiary
        case .failed: return V.danger
        }
    }

    // MARK: - Actions

    private func select(_ flow: Flow) {
        selection = flow.id
        draft = flow
        isNew = false
        saveNote = nil
    }

    private func newFlow() {
        let flow = Flow(name: "New Flow")
        draft = flow
        isNew = true
        selection = flow.id
        saveNote = "Give it a name, add steps, then Save."
    }

    private func saveDraft() {
        guard let flow = draft else { return }
        let problems = flow.validate()
        guard problems.isEmpty else {
            saveNote = problems.map(\.message).joined(separator: " ")
            return
        }
        switch store.save(flow) {
        case .success:
            selection = flow.id
            isNew = false
            saveNote = "Saved. “\(flow.name)” has \(flow.steps.count) steps."
            draft = store.flows.first { $0.id == flow.id } ?? flow
        case .failure(let error):
            saveNote = "Could not save: \(error.localizedDescription)"
        }
    }

    private func duplicate() {
        guard let flow = draft else { return }
        let copy = store.duplicate(flow)
        draft = copy
        selection = copy.id
        saveNote = "Duplicated as “\(copy.name)”."
    }

    private func runDraft() {
        guard let flow = draft else { return }
        // Run what is saved when it exists, so an unsaved edit cannot silently
        // change what runs. Unsaved-new flows run their current draft.
        let target = store.flows.first { $0.id == flow.id } ?? flow
        guard target.validate().isEmpty else {
            saveNote = "Fix the steps before running: "
                + target.validate().map(\.message).joined(separator: " ")
            return
        }
        Task { await engine.run(target) }
    }

    private func requestRetry(_ run: FlowRun) {
        let safety = engine.retrySafety(for: run)
        if safety.safe {
            if let flow = store.flows.first(where: { $0.id == run.flowID }) {
                Task { await engine.run(flow) }
            }
        } else {
            pendingRetry = run
        }
    }

    private func addStep(_ kind: MacActionKind) {
        guard var flow = draft else { return }
        var args = StoredArguments()
        // Fill the one argument a person almost always means, so a new step is
        // valid on arrival instead of immediately flagged.
        switch kind {
        case .openFolder: args.path = "Downloads"
        case .openApplication, .activateApplication, .quitApplication: args.application = "Safari"
        case .openURL: args.url = "https://example.com"
        case .setVolume: args.percent = 30
        case .setMuted: args.flag = true
        case .arrangeWindow: args.side = .left
        case .openSystemSettings: args.settingsPane = ""
        default: break
        }
        flow.steps.append(FlowStep(actionID: kind.id, arguments: args))
        draft = flow
    }

    private func removeStep(_ index: Int) {
        guard var flow = draft, flow.steps.indices.contains(index) else { return }
        flow.steps.remove(at: index)
        draft = flow
    }

    private func moveStep(_ index: Int, by delta: Int) {
        guard var flow = draft else { return }
        let target = index + delta
        guard flow.steps.indices.contains(target) else { return }
        flow.steps.swapAt(index, target)
        draft = flow
    }

    private func draftFromText() {
        switch authoring.interpret(describeText, store: store) {
        case .draft(let flow), .incompleteDraft(let flow, _):
            // An editable draft, not a saved or executed flow.
            var new = flow
            new = Flow(name: flow.name, description: flow.description,
                       steps: flow.steps, failurePolicy: flow.failurePolicy)
            draft = new
            selection = new.id
            isNew = true
            saveNote = flow.steps.isEmpty
                ? "Nothing in that sentence matched an action. Add steps by hand."
                : "Drafted \(flow.steps.count) steps. Review, then Save — it has not run."
        case .unknownFlow(let message):
            saveNote = message
        case .run, .describe, .notAFlowRequest:
            saveNote = "That reads as a command, not a description of a flow. "
                + "Try “create a flow that opens Safari and sets volume to 30 percent”."
        }
    }
}

private extension Array {
    subscript(safe index: Int) -> Element? {
        indices.contains(index) ? self[index] : nil
    }
}