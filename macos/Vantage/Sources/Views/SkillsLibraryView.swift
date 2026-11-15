import SwiftUI

/// Skills Library: what Vantage can repeat, and what each has actually proven.
///
/// The screen is built around one rule: a skill's reliability is **measured**,
/// never implied. A skill that has never run shows "never run" rather than a
/// green checkmark, because a manifest existing is not evidence that the
/// automation works.
///
/// Running a skill goes through the backend's mission engine, so it is gated
/// exactly like any other work: if the step needs approval, this screen shows
/// that the run is awaiting it instead of pretending the skill finished.
struct SkillsLibraryView: View {

    @State private var skills: [APIModels.Skill] = []
    @State private var isLoading = true
    @State private var errorMessage: String?
    @State private var busy: String?
    @State private var lastRun: [String: APIModels.SkillRun] = [:]

    private let api = APIClient.shared

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: V.Space.l) {
                PageHeader(title: "Skills Library",
                           subtitle: "Reusable automations, with the runs that proved them.")

                if let error = errorMessage {
                    ErrorBanner(message: error, retry: { Task { await load() } })
                }

                if skills.isEmpty && !isLoading && errorMessage == nil {
                    EmptyState(
                        symbol: "square.stack.3d.up",
                        title: "No skills yet",
                        message: "A skill is a saved, permissioned automation. None has "
                            + "been stored on this backend.")
                }

                ForEach(skills) { skill in
                    skillRow(skill)
                }
            }
            .padding(V.Space.gutter)
        }
        .background(V.background)
        .task { await load() }
    }

    private func skillRow(_ skill: APIModels.Skill) -> some View {
        VStack(alignment: .leading, spacing: V.Space.xs) {
            HStack(spacing: V.Space.s) {
                Text(skill.name.isEmpty ? skill.skill_id : skill.name)
                    .font(V.Font.cardTitle)
                    .foregroundColor(V.text)
                Text("v\(skill.version)")
                    .font(V.Font.caption)
                    .foregroundColor(V.textTertiary)
                Spacer()
                reliabilityBadge(skill.reliability)
            }

            if !skill.description.isEmpty {
                Text(skill.description)
                    .font(V.Font.caption)
                    .foregroundColor(V.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            if !skill.required_capabilities.isEmpty {
                Text("Needs: \(skill.required_capabilities.joined(separator: ", "))")
                    .font(V.Font.mono)
                    .foregroundColor(V.textTertiary)
            }

            HStack(spacing: V.Space.m) {
                Button(busy == skill.skill_id ? "Running…" : "Run") {
                    Task { await run(skill) }
                }
                .buttonStyle(VPrimaryButtonStyle())
                .disabled(busy != nil)

                if let run = lastRun[skill.skill_id] {
                    Text(runLine(run))
                        .font(V.Font.caption)
                        .foregroundColor(run.verdict == "verified" ? V.success : V.warning)
                }
                Spacer()
            }
        }
        .padding(V.Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(V.surface)
        .clipShape(RoundedRectangle(cornerRadius: V.Radius.card))
        .overlay(RoundedRectangle(cornerRadius: V.Radius.card)
            .stroke(V.border, lineWidth: 1))
    }

    /// Measured counters, said in words. Never a guessed percentage.
    private func reliabilityBadge(_ reliability: APIModels.SkillReliability?) -> some View {
        let text: String
        let tone: Color
        if let reliability, let rate = reliability.success_rate {
            text = "\(Int(rate * 100))% of \(reliability.runs) runs"
            tone = rate >= 0.8 ? V.success : (rate > 0 ? V.warning : V.danger)
        } else {
            text = "never run"
            tone = V.textTertiary
        }
        return Text(text)
            .font(V.Font.caption)
            .foregroundColor(tone)
            .padding(.horizontal, V.Space.s)
            .padding(.vertical, V.Space.xxs)
            .background(tone.opacity(0.1))
            .clipShape(Capsule())
    }

    private func runLine(_ run: APIModels.SkillRun) -> String {
        switch run.state {
        case "completed": return "Verified against its own checks."
        case "awaiting_approval": return "Waiting for approval before it can run."
        case "blocked": return "Refused: \(run.reason)"
        case "partially_completed": return "Ran, but the outcome is \(run.verdict)."
        default: return "Ended \(run.state)."
        }
    }

    private func load() async {
        isLoading = true
        errorMessage = nil
        do {
            let response: APIModels.SkillList = try await api.get("/intelligence/skills")
            skills = response.skills
        } catch {
            errorMessage = error.localizedDescription
        }
        isLoading = false
    }

    private func run(_ skill: APIModels.Skill) async {
        busy = skill.skill_id
        errorMessage = nil
        do {
            let run: APIModels.SkillRun = try await api.post(
                "/intelligence/skills/\(skill.skill_id)/run",
                body: APIModels.SkillRunRequest(
                    available_capabilities: skill.required_capabilities,
                    allowed_scope: skill.required_scope),
                as: APIModels.SkillRun.self)
            lastRun[skill.skill_id] = run
            await load()
        } catch {
            errorMessage = error.localizedDescription
        }
        busy = nil
    }
}