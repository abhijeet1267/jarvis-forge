import SwiftUI

/// Live Mission Steering: change a running plan, and see honestly what that
/// change can and cannot reach.
///
/// The screen exists to answer one question before the person commits to a
/// change: *"what does this revision leave standing?"* A rule added now binds
/// future work; it does not reach into a file a completed step already wrote.
/// So every revision is previewed first, and the preview names the completed
/// steps and the resources they touched in the same breath as what the new plan
/// adds.
///
/// Two rules from the rest of the app apply:
///
/// * **Nothing is applied by looking.** Preview is a separate, read-only call;
///   a revision is committed only by the steering button, and only after the
///   person has seen the preview.
/// * **A refusal is named, not hidden.** A proposed step the mission's own
///   intent refuses is shown with the rule it broke, so the person can correct
///   the plan rather than being told "no".
struct MissionSteeringView: View {
    @State private var missions: [APIModels.Mission] = []
    @State private var selected: APIModels.Mission?
    /// The plan being composed. Seeded from the mission's current steps, so a
    /// revision starts from the plan that exists rather than a blank form.
    @State private var draftSteps: [DraftStep] = []
    @State private var reason = ""
    @State private var preview: APIModels.SteerPreview?
    @State private var committed: APIModels.SteeringReport?
    @State private var loading = false
    @State private var previewing = false
    @State private var committing = false
    @State private var errorMessage: String?

    private let api = APIClient.shared

    /// One editable step. Mirrors the wire's `SteerRequest.Step` but holds a
    /// stable identity so SwiftUI can diff the list while a person edits it.
    private struct DraftStep: Identifiable {
        let id: String
        var title: String
        var tool: String
        var path: String
        /// The step's other parameters, carried through untouched. The editor
        /// only exposes `path`, and dropping the rest on commit would silently
        /// re-write a step's tool call -- exactly the "editing the record"
        /// failure the backend refuses.
        var extra: [String: String]

        init(id: String, title: String, tool: String, path: String,
             extra: [String: String] = [:]) {
            self.id = id
            self.title = title
            self.tool = tool
            self.path = path
            self.extra = extra
        }

        init(from step: APIModels.MissionStep) {
            id = step.step_id
            title = step.title
            tool = step.tool ?? ""
            var rest = step.params ?? [:]
            path = rest.removeValue(forKey: "path") ?? ""
            extra = rest
        }
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: V.Space.l) {
                PageHeader(
                    title: "Steering",
                    subtitle: "Change a plan while it runs. Finished work stays finished."
                )

                if let errorMessage {
                    Text(errorMessage)
                        .font(V.Font.caption).foregroundColor(V.danger)
                        .fixedSize(horizontal: false, vertical: true)
                }

                missionPicker

                if let mission = selected {
                    editor(for: mission)
                } else if !missions.isEmpty {
                    Text("Pick a mission above to steer it.")
                        .font(V.Font.body).foregroundColor(V.textSecondary)
                }
            }
            .padding(V.Space.xl)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(V.background)
        .task { await load() }
    }

    // MARK: - Picker

    @ViewBuilder
    private var missionPicker: some View {
        VStack(alignment: .leading, spacing: V.Space.s) {
            HStack {
                Text("MISSION").font(V.Font.caption).foregroundColor(V.textTertiary)
                Spacer()
                Button("Refresh") { Task { await load() } }
                    .buttonStyle(VQuietButtonStyle())
                    .disabled(loading)
            }

            if missions.isEmpty {
                Text(loading ? "Loading…"
                     : "There are no missions to steer. Create one in Mission Control.")
                    .font(V.Font.caption).foregroundColor(V.textTertiary)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                ForEach(steerableMissions) { mission in
                    Button {
                        select(mission)
                    } label: {
                        HStack(alignment: .firstTextBaseline, spacing: V.Space.s) {
                            Image(systemName: selected?.mission_id == mission.mission_id
                                  ? "largecircle.fill.circle" : "circle")
                                .font(.system(size: 11))
                                .foregroundColor(selected?.mission_id == mission.mission_id
                                                 ? V.accent : V.textTertiary)
                            VStack(alignment: .leading, spacing: 2) {
                                Text(mission.request).font(V.Font.body)
                                    .foregroundColor(V.text).lineLimit(1)
                                Text("\(mission.state) · revision \(mission.revision) · "
                                     + "\(mission.steps?.count ?? 0) step(s)")
                                    .font(V.Font.mono).foregroundColor(V.textTertiary)
                            }
                            Spacer()
                        }
                        .contentShape(Rectangle())
                        .padding(.vertical, 3)
                    }
                    .buttonStyle(.plain)
                }

                if steerableMissions.isEmpty {
                    Text("No mission is steerable right now. A plan that has "
                         + "finished cannot be changed -- its outcome is the "
                         + "record of the plan that ran.")
                        .font(V.Font.caption).foregroundColor(V.textTertiary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .padding(V.Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(V.surface)
        .clipShape(RoundedRectangle(cornerRadius: V.Radius.card))
    }

    /// A plan that has finished is not steerable: its outcome is the record of
    /// what ran. Filtering here rather than greying out rows keeps the list an
    /// answer to "what can I act on" rather than a catalogue of everything.
    private var steerableMissions: [APIModels.Mission] {
        let open: Set<String> = ["draft", "planned", "awaiting_approval", "running",
                                 "paused", "recovering", "blocked"]
        return missions.filter { open.contains($0.state) }
    }

    // MARK: - Editor

    @ViewBuilder
    private func editor(for mission: APIModels.Mission) -> some View {
        VStack(alignment: .leading, spacing: V.Space.m) {
            HStack(alignment: .firstTextBaseline) {
                SectionHeader("Plan")
                Spacer()
                Text("revision \(mission.revision)")
                    .font(V.Font.mono).foregroundColor(V.textTertiary)
            }

            ForEach($draftSteps) { $step in
                draftRow(step: $step, mission: mission)
            }

            HStack(spacing: V.Space.s) {
                Button("Add a step") { addStep() }
                    .buttonStyle(VSecondaryButtonStyle())
                Spacer()
            }

            VStack(alignment: .leading, spacing: V.Space.xs) {
                Text("WHY").font(V.Font.caption).foregroundColor(V.textTertiary)
                TextField("What changed, and why (required)", text: $reason)
                    .textFieldStyle(.roundedBorder)
                    .font(V.Font.body)
            }

            HStack(spacing: V.Space.s) {
                Button(previewing ? "Previewing…" : "Preview the change") {
                    Task { await runPreview() }
                }
                .buttonStyle(VSecondaryButtonStyle())
                .disabled(previewing || committing)

                Button(committing ? "Applying…" : "Apply revision") {
                    Task { await commit() }
                }
                .buttonStyle(VPrimaryButtonStyle())
                // A revision commits with whatever is in the box, matching what the preview
    // showed. Requiring a fresh preview is what makes "a person should not apply
    // a revision they have not seen" true.
    .disabled(committing || previewStale(mission))
                Spacer()
            }

            if let preview {
                previewCard(preview, mission: mission)
            }
            if let committed {
                committedCard(committed)
            }
        }
        .padding(V.Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(V.surface)
        .clipShape(RoundedRectangle(cornerRadius: V.Radius.card))
    }

    @ViewBuilder
    private func draftRow(step: Binding<DraftStep>, mission: APIModels.Mission) -> some View {
        let isDone = (mission.steps ?? []).first(where: { $0.step_id == step.wrappedValue.id })
            .map { $0.status == "done" || $0.status == "failed" } ?? false

        VStack(alignment: .leading, spacing: V.Space.xs) {
            HStack(alignment: .firstTextBaseline, spacing: V.Space.s) {
                Text(step.wrappedValue.id)
                    .font(V.Font.mono).foregroundColor(V.textTertiary)
                TextField("Title", text: step.title)
                    .textFieldStyle(.roundedBorder)
                    .font(V.Font.body)
                    .disabled(isDone)
                if isDone {
                    Label("already ran", systemImage: "lock.fill")
                        .font(V.Font.caption).foregroundColor(V.textTertiary)
                }
            }
            HStack(spacing: V.Space.s) {
                TextField("tool", text: step.tool)
                    .textFieldStyle(.roundedBorder)
                    .font(V.Font.mono)
                    .frame(maxWidth: 180)
                    .disabled(isDone)
                TextField("path (optional)", text: step.path)
                    .textFieldStyle(.roundedBorder)
                    .font(V.Font.mono)
                    .disabled(isDone)
                Button {
                    removeStep(step.wrappedValue.id)
                } label: {
                    Image(systemName: "minus.circle")
                }
                .buttonStyle(VQuietButtonStyle())
                .disabled(isDone || draftSteps.count <= 1)
            }
            if isDone {
                Text("This step's effect already happened. A revision cannot change "
                     + "it or take it back.")
                    .font(V.Font.caption).foregroundColor(V.textTertiary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(V.Space.s)
        .background(V.surfaceAlt)
        .clipShape(RoundedRectangle(cornerRadius: V.Radius.control))
    }

    // MARK: - Preview

    @ViewBuilder
    private func previewCard(_ preview: APIModels.SteerPreview,
                             mission: APIModels.Mission) -> some View {
        VStack(alignment: .leading, spacing: V.Space.s) {
            HStack {
                Text("PREVIEW").font(V.Font.caption).foregroundColor(V.textTertiary)
                Spacer()
                Text("revision \(preview.from_revision) → \(preview.to_revision)")
                    .font(V.Font.mono).foregroundColor(V.textTertiary)
            }

            Text(preview.note)
                .font(V.Font.body).foregroundColor(V.text)
                .fixedSize(horizontal: false, vertical: true)

            if !preview.already_done.isEmpty {
                SectionHeader("Already happened — a revision cannot undo this")
                ForEach(preview.already_done) { effect in
                    HStack(alignment: .firstTextBaseline, spacing: V.Space.s) {
                        Image(systemName: "checkmark.circle")
                            .foregroundColor(V.textTertiary).font(.system(size: 11))
                        VStack(alignment: .leading, spacing: 2) {
                            Text(effect.title.isEmpty ? effect.step_id : effect.title)
                                .font(V.Font.label).foregroundColor(V.text)
                            Text("\(effect.tool) · "
                                 + (effect.resource.isEmpty
                                    ? "no named resource" : effect.resource))
                                .font(V.Font.mono).foregroundColor(V.textTertiary)
                                .lineLimit(1)
                        }
                        Spacer()
                        Text(effect.status)
                            .font(V.Font.caption).foregroundColor(V.textSecondary)
                    }
                    .padding(V.Space.xs)
                }
            }

            if !preview.intent_refusals.isEmpty {
                SectionHeader("Refused by this mission's original intent")
                ForEach(preview.intent_refusals) { refusal in
                    VStack(alignment: .leading, spacing: 2) {
                        HStack(spacing: V.Space.xs) {
                            Image(systemName: "xmark.octagon")
                                .foregroundColor(V.danger).font(.system(size: 11))
                            Text(refusal.step_id)
                                .font(V.Font.mono).foregroundColor(V.text)
                            Text(refusal.violation)
                                .font(V.Font.caption).foregroundColor(V.danger)
                        }
                        Text(refusal.reason)
                            .font(V.Font.caption).foregroundColor(V.textSecondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .padding(V.Space.xs)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(V.surfaceAlt)
                    .clipShape(RoundedRectangle(cornerRadius: V.Radius.control))
                }
            }

            if preview.requires_new_approval {
                Text("This expands what the mission asks for, so the approval you "
                     + "gave the previous plan does not cover it. Running it will "
                     + "ask you again at the step that needs it.")
                    .font(V.Font.caption).foregroundColor(V.warning)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(V.Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(V.background)
        .clipShape(RoundedRectangle(cornerRadius: V.Radius.control))
        .overlay(RoundedRectangle(cornerRadius: V.Radius.control)
            .stroke(V.border, lineWidth: 1))
    }

    @ViewBuilder
    private func committedCard(_ report: APIModels.SteeringReport) -> some View {
        VStack(alignment: .leading, spacing: V.Space.xs) {
            HStack {
                Label("Revision \(report.plan_revision) applied",
                      systemImage: "checkmark.circle")
                    .font(V.Font.caption).foregroundColor(V.success)
                Spacer()
            }
            Text(report.note)
                .font(V.Font.caption).foregroundColor(V.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(V.Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(V.surfaceAlt)
        .clipShape(RoundedRectangle(cornerRadius: V.Radius.control))
    }

    // MARK: - Logic

    private func select(_ mission: APIModels.Mission) {
        selected = mission
        draftSteps = (mission.steps ?? []).map(DraftStep.init(from:))
        reason = ""
        preview = nil
        committed = nil
    }

    private func addStep() {
        let next = "S\(draftSteps.count + 1)"
        // A fresh id that is not already in use -- adding after a step was
        // removed could otherwise collide with one still in the plan.
        var candidate = next
        var n = draftSteps.count + 1
        while draftSteps.contains(where: { $0.id == candidate }) {
            n += 1
            candidate = "S\(n)"
        }
        draftSteps.append(DraftStep(id: candidate, title: "", tool: "", path: ""))
        preview = nil
    }

    private func removeStep(_ id: String) {
        draftSteps.removeAll { $0.id == id }
        preview = nil
    }

    /// Is the preview for a plan the person has since edited or a revision the
    /// backend has since moved past? Applying a stale preview would commit a
    /// revision nobody saw.
    private func previewStale(_ mission: APIModels.Mission) -> Bool {
        guard let preview else { return true }
        if preview.from_revision != mission.revision { return true }
        return preview.steps != draftSteps.map(\.id)
    }

    private func wireSteps() -> [APIModels.SteerRequest.Step] {
        draftSteps.map { step in
            var params = step.extra
            if !step.path.isEmpty { params["path"] = step.path }
            return .init(step_id: step.id,
                         title: step.title.isEmpty ? nil : step.title,
                         tool: step.tool.isEmpty ? nil : step.tool,
                         params: params.isEmpty ? nil : params)
        }
    }

    private func runPreview() async {
        guard let mission = selected else { return }
        previewing = true
        errorMessage = nil
        committed = nil
        do {
            preview = try await api.post(
                "/missions/\(mission.mission_id)/steer/preview",
                body: APIModels.SteerRequest(steps: wireSteps(), reason: reason),
                as: APIModels.SteerPreview.self)
        } catch {
            preview = nil
            errorMessage = error.localizedDescription
        }
        previewing = false
    }

    private func commit() async {
        guard let mission = selected else { return }
        committing = true
        errorMessage = nil
        do {
            let result: APIModels.SteerResult = try await api.post(
                "/missions/\(mission.mission_id)/steer",
                body: APIModels.SteerRequest(steps: wireSteps(), reason: reason),
                as: APIModels.SteerResult.self)
            committed = result.steering
            preview = nil
            // Reload so the mission list and this editor reflect the new
            // revision rather than the one the person started from.
            await load()
            if let fresh = missions.first(where: { $0.mission_id == result.mission_id }) {
                select(fresh)
                committed = result.steering
            }
        } catch {
            errorMessage = error.localizedDescription
        }
        committing = false
    }

    private func load() async {
        loading = true
        errorMessage = nil
        do {
            let response: APIModels.MissionList = try await api.get("/missions?limit=50")
            missions = response.missions
            if let current = selected,
               let fresh = missions.first(where: { $0.mission_id == current.mission_id }) {
                selected = fresh
            } else if selected == nil, let first = steerableMissions.first {
                // Open on a plan rather than an empty panel. Seeding the editor
                // applies nothing -- a revision is committed only by Preview
                // then Apply -- so this costs the person nothing.
                select(first)
            }
        } catch {
            errorMessage = error.localizedDescription
        }
        loading = false
    }
}