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

    private let api = APIClient.shared

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: V.Space.l) {
                PageHeader(title: "Mission Control",
                           subtitle: "What Vantage was asked to do, and what it can prove.")

                if let error = errorMessage {
                    ErrorBanner(message: error, retry: { Task { await load() } })
                }

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