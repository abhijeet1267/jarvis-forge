import SwiftUI

/// Automation Studio — demonstrate something, review the draft, then run it.
///
/// The screen is one flow with four honest states, in this order: nothing
/// recording → recording (steps appear as they are captured) → stopped, with a
/// reviewable draft → saved as a mission contract and run through the real
/// permission gate.
///
/// Three things this screen deliberately does not do:
///
/// - **It never shows a draft as verified.** A demonstration proves what was
///   done, not that doing it again works. `verdict` starts `unverified` and only
///   a replay moves it, so the draft header says "not yet proven" until a run
///   says otherwise.
/// - **It never hides a gap.** A step the recorder refused (a password field, a
///   token) appears in the list marked "needs input" with the parameter's name
///   and no value. The person is owed the knowledge that something happened
///   there.
/// - **It never runs anything itself.** Saving turns the draft into a mission
///   contract through `/missions`, and running goes through `/missions/{id}/run`,
///   which is the same gated executor every other mission uses. A second
///   execution path in the UI would be a way around the permission engine.
struct AutomationStudioView: View {

    @State private var phase: Phase = .idle
    @State private var recording: APIModels.RecordingSession?
    @State private var draft: APIModels.RecordingDraft?
    @State private var errorMessage: String?
    @State private var busy = false
    @State private var name = ""

    // Capture form: the tool a step names, and its parameters. The kinds are
    // offered from the recorder's own list rather than a hand-written one, so a
    // kind that cannot be replayed is not offerable here.
    @State private var stepKind = APIModels.AutomationKinds.names[0]
    @State private var stepLabel = ""
    @State private var stepParams: [APIModels.AutomationKinds.ParamField] = []

    @State private var missionId: String?
    @State private var runState = ""
    @State private var runVerdict: String?
    @State private var runSteps: [APIModels.MissionRunStep] = []
    @State private var parkedStepId: String?
    @State private var parkedConfirmation: String?

    private let api = APIClient.shared

    private enum Phase: Equatable {
        case idle, recording, stopped, running
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: V.Space.l) {
                PageHeader(title: "Automation Studio",
                           subtitle: "Show it once. Review what was captured. Then run it.")
                { headerActions }

                if let errorMessage {
                    ErrorBanner(message: errorMessage, retry: { Task { await reload() } })
                }

                if phase == .idle { explainer }
                if phase == .recording || phase == .stopped { capturePanel }
                if let draft { draftPanel(draft) }
                if let runVerdict { runPanel(runVerdict) }
            }
            .padding(V.Space.gutter)
        }
        .background(V.background)
        .task { await reload() }
    }

    // MARK: - Header

    private var headerActions: some View {
        HStack(spacing: V.Space.s) {
            if phase == .idle || phase == .stopped {
                Button("Start recording") { Task { await start() } }
                    .buttonStyle(VPrimaryButtonStyle())
                    .disabled(busy)
            }
            if phase == .recording {
                Button("Stop") { Task { await stop() } }
                    .buttonStyle(VPrimaryButtonStyle())
                    .disabled(busy)
            }
            if phase == .stopped || draft != nil {
                Button("Discard") { Task { await discard() } }
                    .buttonStyle(VSecondaryButtonStyle())
                    .disabled(busy)
            }
        }
    }

    // MARK: - Explainer

    private var explainer: some View {
        VStack(alignment: .leading, spacing: V.Space.s) {
            Text("How this works")
                .font(V.Font.cardTitle).foregroundColor(V.text)
            ForEach(Array(Self.explainerLines.enumerated()), id: \.offset) { _, line in
                HStack(alignment: .top, spacing: V.Space.s) {
                    Image(systemName: "circle.fill")
                        .font(.system(size: 4))
                        .foregroundColor(V.textTertiary)
                        .padding(.top, 6)
                    Text(line)
                        .font(V.Font.body)
                        .foregroundColor(V.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .padding(V.Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(V.surface)
        .clipShape(RoundedRectangle(cornerRadius: V.Radius.card))
    }

    private static let explainerLines = [
        "Press Start, then perform the actions you want repeated. Each one is captured as a typed step — a tool and its parameters — never a screen coordinate.",
        "A value that looks like a secret is refused at capture time. The step appears marked “needs input” so you can see the gap, and replay asks for the value at run time.",
        "When you stop, the recording becomes a draft. It is unverified until it runs for real; saving it makes a mission contract that goes through the same permission engine as any other action.",
    ]

    // MARK: - Capture

    private var capturePanel: some View {
        VStack(alignment: .leading, spacing: V.Space.s) {
            HStack {
                SectionHeader(phase == .recording ? "Recording" : "Captured")
                Spacer()
                if let recording {
                    Text("\(recording.steps.count) step(s)")
                        .font(V.Font.caption).foregroundColor(V.textTertiary)
                }
            }

            if phase == .recording { captureForm }

            if let recording, recording.steps.isEmpty {
                Text("Nothing captured yet.")
                    .font(V.Font.body).foregroundColor(V.textSecondary)
            } else if let recording {
                ForEach(recording.steps) { step in stepRow(step) }
            }
        }
        .padding(V.Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(V.surface)
        .clipShape(RoundedRectangle(cornerRadius: V.Radius.card))
    }

    private var captureForm: some View {
        VStack(alignment: .leading, spacing: V.Space.s) {
            HStack(spacing: V.Space.s) {
                Picker("", selection: $stepKind) {
                    ForEach(APIModels.AutomationKinds.names, id: \.self) { kind in
                        Text(kind).tag(kind)
                    }
                }
                .labelsHidden()
                .frame(width: 220)
                .onChange(of: stepKind) { _ in resetParams() }

                TextField("What you did (optional)", text: $stepLabel)
                    .textFieldStyle(.roundedBorder)
                    .font(V.Font.body)
                Spacer(minLength: 0)
            }

            ForEach($stepParams) { $field in
                HStack(spacing: V.Space.s) {
                    Text(field.name)
                        .font(V.Font.mono).foregroundColor(V.textTertiary)
                        .frame(width: 90, alignment: .leading)
                    if field.isChoice {
                        Picker("", selection: $field.value) {
                            ForEach(field.options, id: \.self) { Text($0).tag($0) }
                        }
                        .labelsHidden()
                        .frame(width: 140)
                    } else {
                        TextField(field.placeholder, text: $field.value)
                            .textFieldStyle(.roundedBorder)
                            .font(V.Font.mono)
                    }
                    if field.isSensitive {
                        // Named, not hidden: the person should see why this one
                        // will not be stored rather than wonder why it vanished.
                        Text("not stored")
                            .font(V.Font.caption).foregroundColor(V.warning)
                    }
                    Spacer(minLength: 0)
                }
            }

            HStack(spacing: V.Space.s) {
                Button("Capture step") { Task { await captureStep() } }
                    .buttonStyle(VSecondaryButtonStyle())
                    .disabled(busy)
                Text("Only the actions you explicitly add here are recorded.")
                    .font(V.Font.caption).foregroundColor(V.textTertiary)
                Spacer()
            }
        }
        .padding(.bottom, V.Space.s)
    }

    private func stepRow(_ step: APIModels.RecordedStep) -> some View {
        HStack(alignment: .top, spacing: V.Space.s) {
            Image(systemName: step.skipped ? "lock.fill" : "checkmark.circle.fill")
                .font(.system(size: 10, weight: .bold))
                .foregroundColor(step.skipped ? V.warning : V.success)
                .frame(width: 14)
            VStack(alignment: .leading, spacing: 1) {
                Text(step.label.isEmpty ? step.kind : step.label)
                    .font(V.Font.body).foregroundColor(V.text)
                if step.skipped {
                    Text(step.skip_reason)
                        .font(V.Font.caption).foregroundColor(V.warning)
                        .fixedSize(horizontal: false, vertical: true)
                } else {
                    Text(step.kind)
                        .font(V.Font.mono).foregroundColor(V.textTertiary)
                }
            }
            Spacer()
        }
    }

    // MARK: - Draft

    private func draftPanel(_ draft: APIModels.RecordingDraft) -> some View {
        VStack(alignment: .leading, spacing: V.Space.s) {
            HStack(spacing: V.Space.s) {
                SectionHeader("Draft")
                Spacer()
                // A demonstration is not a proof. This is the whole verdict.
                Label(draft.verdict == "unverified" ? "not yet proven" : draft.verdict,
                      systemImage: draft.verdict == "unverified"
                      ? "questionmark.circle" : "checkmark.seal.fill")
                    .font(V.Font.caption)
                    .foregroundColor(draft.verdict == "unverified" ? V.warning : V.success)
            }

            ForEach(Array(draft.steps.enumerated()), id: \.offset) { index, step in
                HStack(alignment: .top, spacing: V.Space.s) {
                    Text("\(index + 1).")
                        .font(V.Font.mono).foregroundColor(V.textTertiary)
                        .frame(width: 20, alignment: .trailing)
                    VStack(alignment: .leading, spacing: 1) {
                        Text(step.title)
                            .font(V.Font.body).foregroundColor(V.text)
                        Text(step.tool)
                            .font(V.Font.mono).foregroundColor(V.textTertiary)
                    }
                    Spacer()
                    if let need = step.needs_input, !need.isEmpty {
                        Text("needs input")
                            .font(V.Font.caption).foregroundColor(V.warning)
                    } else if (step.params ?? [:]).isEmpty {
                        Text("no captured values")
                            .font(V.Font.caption).foregroundColor(V.warning)
                    }
                }
            }

            if !draft.required_scope.isEmpty {
                Text("Touches")
                    .font(V.Font.caption).foregroundColor(V.textTertiary)
                    .padding(.top, V.Space.xs)
                ForEach(draft.required_scope, id: \.self) { scope in
                    Text(scope)
                        .font(V.Font.mono).foregroundColor(V.textSecondary)
                        .lineLimit(1).truncationMode(.middle)
                }
            }

            Text(draft.notes)
                .font(V.Font.caption).foregroundColor(V.textTertiary)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.top, V.Space.xs)

            HStack(spacing: V.Space.s) {
                TextField("Name this automation", text: $name)
                    .textFieldStyle(.roundedBorder)
                    .font(V.Font.body)
                    .frame(maxWidth: 280)
                Button(busy ? "Saving…" : "Save & run") { Task { await saveAndRun(draft) } }
                    .buttonStyle(VPrimaryButtonStyle())
                    .disabled(busy || !draft.steps.allSatisfy { !($0.params ?? [:]).isEmpty })
                    .help(draft.steps.allSatisfy { !($0.params ?? [:]).isEmpty }
                          ? "" : "A step with no captured values cannot be replayed.")
                Spacer()
            }
            .padding(.top, V.Space.xs)
        }
        .padding(V.Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(V.surface)
        .clipShape(RoundedRectangle(cornerRadius: V.Radius.card))
    }

    // MARK: - Run result

    private func runPanel(_ verdict: String) -> some View {
        let parked = runState == "awaiting_approval"
        return VStack(alignment: .leading, spacing: V.Space.s) {
            HStack(spacing: V.Space.s) {
                Image(systemName: parked ? "hand.raised.fill"
                      : verdict == "verified" ? "checkmark.seal.fill"
                      : "exclamationmark.triangle.fill")
                    .foregroundColor(parked ? V.warning
                                     : verdict == "verified" ? V.success : V.warning)
                Text(parked
                     ? "Stopped for your approval. Nothing has run yet."
                     : verdict == "verified"
                     ? "Every step ran and every acceptance condition was verified."
                     : "The outcome is \(verdict).")
                    .font(V.Font.body)
                    .foregroundColor(parked ? V.warning
                                     : verdict == "verified" ? V.text : V.warning)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer()
                if parked, parkedStepId != nil {
                    Button(busy ? "Approving…" : "Approve and run") { Task { await approveParked() } }
                        .buttonStyle(VPrimaryButtonStyle())
                        .disabled(busy)
                }
            }
            if parked {
                // Explaining the stop is the point: a person who does not know
                // why it paused will assume it failed.
                Text("This automation includes an action Vantage asks a person to "
                     + "confirm before it runs. Approving authorizes only these "
                     + "steps, once.")
                    .font(V.Font.caption).foregroundColor(V.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let missionId {
                Text("Mission \(missionId)")
                    .font(V.Font.mono).foregroundColor(V.textTertiary)
            }
            ForEach(Array(runSteps.enumerated()), id: \.offset) { _, step in
                HStack(alignment: .top, spacing: V.Space.s) {
                    Image(systemName: step.ok ? "checkmark" : "xmark")
                        .font(.system(size: 9, weight: .bold))
                        .foregroundColor(step.ok ? V.success : V.danger)
                        .frame(width: 12)
                    VStack(alignment: .leading, spacing: 1) {
                        Text(step.title).font(V.Font.body).foregroundColor(V.text)
                        if !step.detail.isEmpty {
                            Text(step.detail)
                                .font(V.Font.caption).foregroundColor(V.textSecondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    Spacer()
                }
            }
        }
        .padding(V.Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(V.surface)
        .clipShape(RoundedRectangle(cornerRadius: V.Radius.card))
    }

    // MARK: - Actions

    private func reload() async {
        errorMessage = nil
        do {
            let current = try await api.get("/intelligence/recordings/current",
                                            as: APIModels.CurrentRecording.self)
            if let session = current.recording {
                recording = session
                phase = session.open ? .recording : .stopped
                if !session.open { await loadDraft() }
            } else {
                recording = nil
                draft = nil
                if phase != .idle { phase = .idle }
            }
        } catch {
            errorMessage = error.localizedDescription
        }
        resetParams()
    }

    private func loadDraft() async {
        do {
            draft = try await api.post("/intelligence/recordings/draft",
                                       as: APIModels.RecordingDraft.self)
        } catch {
            // No draft for a stopped-but-discarded recording is not an error the
            // person needs to see; the recording panel already says what happened.
            draft = nil
        }
    }

    private func start() async {
        busy = true
        errorMessage = nil
        defer { busy = false }
        do {
            let session = try await api.post(
                "/intelligence/recordings",
                body: APIModels.RecordingStartRequest(name: name),
                as: APIModels.RecordingSession.self)
            recording = session
            draft = nil
            runVerdict = nil
            runSteps = []
            missionId = nil
            phase = .recording
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func stop() async {
        busy = true
        errorMessage = nil
        defer { busy = false }
        do {
            recording = try await api.post("/intelligence/recordings/stop",
                                           as: APIModels.RecordingSession.self)
            phase = .stopped
            await loadDraft()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func captureStep() async {
        busy = true
        errorMessage = nil
        defer { busy = false }
        var params: [String: String] = [:]
        for field in stepParams where !field.value.isEmpty {
            params[field.name] = field.value
        }
        do {
            _ = try await api.post(
                "/intelligence/recordings/steps",
                body: APIModels.RecordingStepRequest(kind: stepKind,
                                                     label: stepLabel, params: params),
                as: APIModels.RecordedStep.self)
            stepLabel = ""
            resetParams()
            await refreshRecording()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func refreshRecording() async {
        if let current = try? await api.get("/intelligence/recordings/current",
                                            as: APIModels.CurrentRecording.self) {
            recording = current.recording
        }
    }

    private func discard() async {
        busy = true
        errorMessage = nil
        defer { busy = false }
        _ = try? await api.delete("/intelligence/recordings/current",
                                  as: APIModels.RecordingDiscardResult.self)
        recording = nil
        draft = nil
        runVerdict = nil
        runSteps = []
        missionId = nil
        phase = .idle
        name = ""
    }

    /// Save the draft as a mission contract, then run it through the gated executor.
    ///
    /// Saving and running are separate requests on purpose: the contract exists
    /// even if the run is refused, so a person can see what was attempted and
    /// why it stopped rather than losing both.
    ///
    /// A level-2 action parks the mission at `awaiting_approval` instead of
    /// running it. That is the permission engine doing its job, so the screen
    /// says exactly that and offers the approval — it never retries silently to
    /// get past the gate.
    private func saveAndRun(_ draft: APIModels.RecordingDraft) async {
        busy = true
        errorMessage = nil
        defer { busy = false }

        let title = name.trimmingCharacters(in: .whitespaces)

        // A step with no captured parameters cannot be replayed: the executor
        // rejects it with "missing required parameter ...". Saving it anyway
        // would create a mission the person asked to run and that was never
        // going to. Name the step and stop instead.
        let unrunnable = draft.steps.filter { ($0.params ?? [:]).isEmpty }
        guard unrunnable.isEmpty else {
            let names = unrunnable.map { $0.title }.joined(separator: ", ")
            errorMessage = "Cannot run yet — \(names) captured no values"
                + (unrunnable.contains { $0.needs_input != nil }
                   ? ". Vantage does not store passwords or other sensitive fields, "
                     + "so that step cannot be replayed as recorded. Discard and "
                     + "record an automation that does not include it."
                   : ". Discard and record that step again.")
            return
        }

        let steps = draft.steps.enumerated().map { index, step in
            APIModels.MissionStepDraft(step_id: "S\(index + 1)",
                                       title: step.title,
                                       tool: step.tool,
                                       params: step.params ?? [:])
        }
        guard !steps.isEmpty else {
            errorMessage = "This draft has no steps to run."
            return
        }

        do {
            let mission = try await api.post(
                "/missions",
                body: APIModels.MissionCreateRequest(
                    request: title.isEmpty ? draft.name : title,
                    outcome: draft.notes,
                    steps: steps),
                as: APIModels.Mission.self)
            missionId = mission.mission_id
            await runMission(mission.mission_id)
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func runMission(_ id: String) async {
        do {
            let run = try await api.post("/missions/\(id)/run",
                                         body: APIModels.MissionRunRequest(approval: nil),
                                         as: APIModels.Mission.self)
            apply(run)
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func apply(_ mission: APIModels.Mission) {
        // The verdict and state are the server's; the screen never re-derives
        // them. A parked mission has no verdict yet, and saying so is correct.
        runState = mission.state
        runVerdict = mission.verdict
        runSteps = (mission.steps ?? []).map { step in
            APIModels.MissionRunStep(title: step.title,
                                     ok: step.status == "done",
                                     detail: step.detail ?? step.status)
        }
        parkedConfirmation = nil
        if mission.state == "awaiting_approval" {
            for step in mission.steps ?? [] where step.status == "pending" {
                if let detail = step.detail,
                   let range = detail.range(of: "confirmation ") {
                    parkedConfirmation = detail[range.upperBound...]
                        .split(separator: " ").first.map(String.init)?
                        .trimmingCharacters(in: CharacterSet(charactersIn: ")"))
                    parkedStepId = step.step_id
                }
            }
        }
    }

    /// Ask the server to decide one step, then resume. The confirmation the
    /// runner matches is the one the parked step named — the screen sends it
    /// back rather than inventing one.
    private func approveParked() async {
        guard let missionId, let stepId = parkedStepId else { return }
        busy = true
        errorMessage = nil
        defer { busy = false }
        do {
            _ = try await api.post("/missions/\(missionId)/steps/\(stepId)/decide",
                                   body: APIModels.MissionDecision(decision: "approve"),
                                   as: APIModels.Mission.self)
            let confirmation = parkedConfirmation
            let run = try await api.post(
                "/missions/\(missionId)/run",
                body: APIModels.MissionRunRequest(approval: confirmation.map {
                    APIModels.MissionApproval(confirmation_id: $0, lease_id: "")
                }),
                as: APIModels.Mission.self)
            apply(run)
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func resetParams() {
        stepParams = APIModels.AutomationKinds.fields(for: stepKind)
    }
}