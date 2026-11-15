import SwiftUI

/// Mission Control: the live state of every mission, and what actually verified it.
///
/// Three rules from the rest of the app apply here and are the reason this view
/// is not a simple list:
///
/// * **A count is a claim about the backend.** "No missions" is only shown when
///   the read succeeded; a failed read shows the error and nothing else.
/// * **A verdict is not a colour.** Each mission states in words whether it is
///   verified, partial, failed or unknown, because a person reading a screenshot
///   or using VoiceOver gets no information from a green dot.
/// * **Verification is inspectable.** Selecting a mission opens ProofLens, which
///   answers claim by claim what was observed -- including "nothing was", which
///   is the honest answer for a mission whose verifier could not check.
struct MissionControlView: View {

    @State private var missions: [APIModels.Mission] = []
    @State private var selected: APIModels.Mission?
    @State private var proof: APIModels.MissionProof?
    @State private var isLoading = true
    @State private var errorMessage: String?
    /// The goal being composed. Creating a mission from here is the whole point
    /// of a control screen -- a list you can only read is a log, not a control.
    @State private var newGoal = ""
    @State private var newCondition = ""
    @State private var creating = false
    @State private var acting: String?
    @State private var runNote: String?

    private let api = APIClient.shared

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: V.Space.l) {
                PageHeader(title: "Mission Control",
                           subtitle: "What Vantage was asked to do, and what it can prove.")

                if let error = errorMessage {
                    ErrorBanner(message: error, retry: { Task { await load() } })
                }

                composer

                if missions.isEmpty && !isLoading && errorMessage == nil {
                    EmptyState(
                        symbol: "target",
                        title: "No missions yet",
                        message: "A mission records an outcome and the conditions that "
                            + "prove it. None has been created on this backend.")
                }

                if !missions.isEmpty {
                    SectionHeader("Missions")
                    VStack(spacing: V.Space.s) {
                        ForEach(missions) { mission in
                            MissionRow(mission: mission,
                                       isSelected: selected?.mission_id == mission.mission_id)
                                .onTapGesture {
                                    selected = mission
                                    Task { await loadProof(mission.mission_id) }
                                }
                        }
                    }
                }

                if let mission = selected {
                    controls(for: mission)
                    ProofLensPanel(mission: mission, proof: proof)
                }
            }
            .padding(V.Space.gutter)
        }
        .background(V.background)
        .task { await load() }
    }

    private func load() async {
        isLoading = true
        errorMessage = nil
        do {
            let response: APIModels.MissionList = try await api.get("/missions?limit=50")
            missions = response.missions
        } catch {
            // Keep the previous list rather than claiming there are none.
            errorMessage = error.localizedDescription
        }
        isLoading = false
    }

    private func loadProof(_ missionID: String) async {
        proof = nil
        do {
            proof = try await api.get("/missions/\(missionID)/proof")
        } catch {
            proof = nil
        }
    }

    // MARK: - Controls

    /// Act on the selected mission: run it, or approve a parked one.
    ///
    /// Approving issues a scoped lease and submits the confirmation the mission
    /// is actually waiting on, so the resume is the same durable invocation
    /// rather than a fresh attempt. The button says what it will do; it never
    /// shows a success state the backend did not report.
    @ViewBuilder
    private func controls(for mission: APIModels.Mission) -> some View {
        VStack(alignment: .leading, spacing: V.Space.s) {
            SectionHeader("Actions")
            HStack(spacing: V.Space.s) {
                switch mission.state {
                case "draft":
                    actionButton("Run this mission", busyKey: "run:\(mission.mission_id)") {
                        await run(mission, approving: false)
                    }
                case "awaiting_approval":
                    actionButton("Approve and run", busyKey: "approve:\(mission.mission_id)") {
                        await run(mission, approving: true)
                    }
                case "running":
                    Text("This mission is running.").font(V.Font.caption)
                        .foregroundColor(V.textSecondary)
                default:
                    Text("Nothing to do: this mission is \(mission.state).")
                        .font(V.Font.caption).foregroundColor(V.textTertiary)
                }
                Spacer()
            }

            if let note = runNote {
                Text(note)
                    .font(V.Font.caption)
                    .foregroundColor(V.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            if mission.state == "awaiting_approval" {
                Text("Approving grants a scoped, single-use lease for this mission's "
                     + "steps and submits the confirmation it is waiting on.")
                    .font(V.Font.caption)
                    .foregroundColor(V.textTertiary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(V.Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(V.surface)
        .clipShape(RoundedRectangle(cornerRadius: V.Radius.card))
        .overlay(RoundedRectangle(cornerRadius: V.Radius.card)
            .stroke(V.border, lineWidth: 1))
    }

    private func actionButton(_ title: String, busyKey: String,
                              work: @escaping () async -> Void) -> some View {
        Button(acting == busyKey ? "Working…" : title) {
            Task { await work() }
        }
        .buttonStyle(VPrimaryButtonStyle())
        .disabled(acting != nil)
    }

    /// Run or approve. Approval issues a lease first, then submits the
    /// confirmation the mission recorded when it parked.
    private func run(_ mission: APIModels.Mission, approving: Bool) async {
        acting = (approving ? "approve:" : "run:") + mission.mission_id
        runNote = nil
        defer { acting = nil }
        do {
            var approval: APIModels.MissionApproval?
            if approving {
                let scope = mission.steps?.first?.params?["path"].map { [$0] } ?? []
                let lease: APIModels.MissionLease = try await api.post(
                    "/missions/\(mission.mission_id)/leases",
                    body: APIModels.MissionLeaseRequest(
                        capability: "write_file", scope: scope, max_uses: 8,
                        ttl_seconds: 600),
                    as: APIModels.MissionLease.self)
                guard let confirmation = parkedConfirmation(mission) else {
                    runNote = "This mission is waiting on a confirmation this app "
                        + "cannot read, so it was not approved."
                    return
                }
                approval = APIModels.MissionApproval(
                    confirmation_id: confirmation, lease_id: lease.lease_id)
            }

            let updated: APIModels.Mission = try await api.post(
                "/missions/\(mission.mission_id)/run",
                body: APIModels.MissionRunRequest(approval: approval),
                as: APIModels.Mission.self)

            runNote = "Ended \(updated.state); outcome: \(updated.verdict)."
                + ((updated.reason ?? "").isEmpty ? "" : " \((updated.reason ?? ""))")
            selected = updated
            await load()
        } catch {
            runNote = error.localizedDescription
        }
    }

    /// The confirmation a parked step recorded, read from its detail line.
    private func parkedConfirmation(_ mission: APIModels.Mission) -> String? {
        for step in mission.steps ?? [] {
            guard let detail = step.detail else { continue }
            if let range = detail.range(of: "confirmation ") {
                let rest = detail[range.upperBound...]
                let id = rest.prefix { !$0.isWhitespace }
                if !id.isEmpty && id != "unspecified" { return String(id) }
            }
        }
        return nil
    }

    // MARK: - Composer

        /// Create a mission: a goal, and at least one condition that proves it.
        ///
        /// The acceptance condition is required here rather than optional, because a
        /// mission with nothing to check cannot honestly complete -- the backend
        /// refuses it, and asking up front is kinder than letting the person find
        /// out at the end.
        private var composer: some View {
            VStack(alignment: .leading, spacing: V.Space.s) {
                SectionHeader("New mission")
                TextField("What should Vantage achieve?", text: $newGoal)
                    .textFieldStyle(.roundedBorder)
                    .font(V.Font.body)
                TextField("How will we know it worked? (a condition you can check)",
                          text: $newCondition)
                    .textFieldStyle(.roundedBorder)
                    .font(V.Font.body)

                HStack(spacing: V.Space.s) {
                    Button(creating ? "Creating…" : "Create mission") {
                        Task { await createMission() }
                    }
                    .buttonStyle(VPrimaryButtonStyle())
                    .disabled(newGoal.trimmingCharacters(in: .whitespaces).isEmpty
                              || newCondition.trimmingCharacters(in: .whitespaces).isEmpty
                              || creating)
                    Spacer()
                }
            }
            .padding(V.Space.m)
            .background(V.surface)
            .clipShape(RoundedRectangle(cornerRadius: V.Radius.card))
            .overlay(RoundedRectangle(cornerRadius: V.Radius.card)
                .stroke(V.border, lineWidth: 1))
        }

        private func createMission() async {
            creating = true
            errorMessage = nil
            do {
                let created: APIModels.Mission = try await api.post(
                    "/missions",
                    body: APIModels.CreateMissionRequest(
                        request: newGoal.trimmingCharacters(in: .whitespaces),
                        outcome: newGoal.trimmingCharacters(in: .whitespaces),
                        conditions: [APIModels.CreateMissionCondition(
                            statement: newCondition.trimmingCharacters(in: .whitespaces))],
                        steps: []),
                    as: APIModels.Mission.self)
                newGoal = ""
                newCondition = ""
                selected = created
                await load()
            } catch {
                errorMessage = error.localizedDescription
            }
            creating = false
        }
}

// MARK: - Rows

private struct MissionRow: View {
    let mission: APIModels.Mission
    let isSelected: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: V.Space.xs) {
            HStack(spacing: V.Space.s) {
                Text((mission.outcome ?? "").isEmpty ? mission.request : (mission.outcome ?? ""))
                    .font(V.Font.cardTitle)
                    .foregroundColor(V.text)
                    .lineLimit(2)
                Spacer(minLength: V.Space.s)
                VerdictBadge(verdict: mission.verdict)
            }

            HStack(spacing: V.Space.s) {
                Text(mission.state.replacingOccurrences(of: "_", with: " "))
                    .font(V.Font.caption)
                    .foregroundColor(V.textTertiary)
                if let summary = mission.condition_summary, !summary.isEmpty {
                    Text("· \(summaryLine(summary))")
                        .font(V.Font.caption)
                        .foregroundColor(V.textTertiary)
                }
            }
        }
        .padding(V.Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(isSelected ? V.surfaceAlt : V.surface)
        .clipShape(RoundedRectangle(cornerRadius: V.Radius.card))
        .overlay(RoundedRectangle(cornerRadius: V.Radius.card)
            .stroke(isSelected ? V.accent : V.border, lineWidth: 1))
    }

    private func summaryLine(_ summary: [String: Int]) -> String {
        let parts = ["verified", "failed", "unknown", "pending"]
            .compactMap { key -> String? in
                guard let n = summary[key], n > 0 else { return nil }
                return "\(n) \(key)"
            }
        return parts.joined(separator: ", ")
    }
}

private struct VerdictBadge: View {
    let verdict: String

    var body: some View {
        // Colour *and* words: the label carries the meaning on its own.
        let (label, tone) = style
        Text(label)
            .font(V.Font.label)
            .foregroundColor(tone)
            .padding(.horizontal, V.Space.s)
            .padding(.vertical, V.Space.xxs)
            .background(tone.opacity(0.12))
            .clipShape(Capsule())
    }

    private var style: (String, Color) {
        switch verdict {
        case "verified": return ("Verified", V.success)
        case "failed": return ("Failed", V.danger)
        case "partial": return ("Partially verified", V.warning)
        default: return ("Not verified", V.textSecondary)
        }
    }
}

// MARK: - ProofLens

private struct ProofLensPanel: View {
    let mission: APIModels.Mission
    let proof: APIModels.MissionProof?

    var body: some View {
        VStack(alignment: .leading, spacing: V.Space.s) {
            SectionHeader("ProofLens — why Vantage believes this")

            if let proof {
                ForEach(proof.claims) { claim in
                    VStack(alignment: .leading, spacing: V.Space.xxs) {
                        Text(claim.claim)
                            .font(V.Font.body)
                            .foregroundColor(V.text)
                        Text(claim.supported
                             ? "Supported by an observation, below."
                             : "Nothing observed. This claim rests on no evidence.")
                            .font(V.Font.caption)
                            .foregroundColor(claim.supported ? V.textSecondary : V.warning)
                        ForEach(Array(claim.observations.enumerated()), id: \.offset) { _, obs in
                            Text("• \(obs.observation)")
                                .font(V.Font.mono)
                                .foregroundColor(V.textSecondary)
                        }
                    }
                    .padding(V.Space.m)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(V.surface)
                    .clipShape(RoundedRectangle(cornerRadius: V.Radius.card))
                }

                Text(proof.evidence_integrity)
                    .font(V.Font.caption)
                    .foregroundColor(V.textTertiary)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                Text("Reading the evidence…")
                    .font(V.Font.caption)
                    .foregroundColor(V.textTertiary)
            }
        }
    }
}