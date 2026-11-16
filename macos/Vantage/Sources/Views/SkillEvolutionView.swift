import SwiftUI

/// Skill Evolution Lab: propose the next version of a skill from what its runs
/// measured — and say plainly what the lab will not do.
///
/// The screen is built around one rule: a proposal can only **remove or
/// tighten**. It never adds a step, never adds a capability or a scope prefix,
/// never lowers a risk tier, and never stores anything by itself. Accepting is a
/// separate button, and the previous version is kept. That is what makes a
/// "learn from history" feature safe to have at all: a proposal a person ignores
/// changes nothing, and one they accept cannot widen what the skill may do.
struct SkillEvolutionView: View {
    @State private var lab: APIModels.EvolutionLab?
    @State private var skills: [APIModels.Skill] = []
    @State private var selected: String?
    @State private var proposal: APIModels.EvolutionProposal?
    @State private var observations: APIModels.EvolutionObservations?
    @State private var accepted: APIModels.EvolutionAccept?
    @State private var loading = false
    @State private var accepting = false

    private let api = APIClient.shared

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: V.Space.l) {
                PageHeader(
                    title: "Skill Evolution",
                    subtitle: "The next version of a skill, derived from what its runs measured."
                ) {
                    Button(loading ? "Reading…" : "Refresh") { Task { await load() } }
                        .buttonStyle(VSecondaryButtonStyle())
                        .disabled(loading)
                }

                if let lab { ruleCard(lab) }

                if skills.isEmpty && !loading {
                    EmptyState(symbol: "wand.and.stars",
                               title: "No skills yet",
                               message: "A skill has to exist and have run before the "
                                 + "lab can propose anything. Recorded runs are the only "
                                 + "evidence it uses.")
                } else {
                    pickerCard
                }

                if let accepted {
                    HStack(alignment: .firstTextBaseline, spacing: V.Space.s) {
                        Image(systemName: "checkmark.seal.fill")
                            .font(.system(size: 11)).foregroundColor(V.success)
                        Text("Stored as v\(accepted.version). \(accepted.note)")
                            .font(V.Font.body).foregroundColor(V.text)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .padding(V.Space.m)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(V.surfaceAlt)
                    .clipShape(RoundedRectangle(cornerRadius: V.Radius.card))
                }

                if let proposal { proposalCard(proposal) }
                if let observations { observationsCard(observations) }
            }
            .padding(V.Space.xl)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(V.background)
        .task { await load() }
    }

    // MARK: - The rule

    @ViewBuilder
    private func ruleCard(_ lab: APIModels.EvolutionLab) -> some View {
        VStack(alignment: .leading, spacing: V.Space.s) {
            Text(lab.note)
                .font(V.Font.body).foregroundColor(V.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
            HStack(alignment: .top, spacing: V.Space.xl) {
                VStack(alignment: .leading, spacing: 4) {
                    Text("CAN CHANGE").font(V.Font.caption).foregroundColor(V.textTertiary)
                    ForEach(lab.can_change, id: \.self) { line in
                        Text("• \(line)").font(V.Font.caption)
                            .foregroundColor(V.textSecondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                VStack(alignment: .leading, spacing: 4) {
                    Text("WILL NOT CHANGE").font(V.Font.caption).foregroundColor(V.textTertiary)
                    ForEach(lab.will_not_change, id: \.self) { line in
                        Text("• \(line)").font(V.Font.caption)
                            .foregroundColor(V.warning)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
            Text("A proposal needs \(lab.min_runs) recorded runs. One demonstration is not evidence.")
                .font(V.Font.caption).foregroundColor(V.textTertiary)
        }
        .padding(V.Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(V.surface)
        .clipShape(RoundedRectangle(cornerRadius: V.Radius.card))
    }

    // MARK: - Picking a skill

    @ViewBuilder
    private var pickerCard: some View {
        VStack(alignment: .leading, spacing: V.Space.s) {
            Text("SKILL").font(V.Font.caption).foregroundColor(V.textTertiary)
            ForEach(skills) { skill in
                Button {
                    Task { await inspect(skill.skill_id) }
                } label: {
                    HStack(alignment: .firstTextBaseline, spacing: V.Space.s) {
                        Image(systemName: selected == skill.skill_id
                              ? "circle.inset.filled" : "circle")
                            .font(.system(size: 10))
                            .foregroundColor(selected == skill.skill_id ? V.accent : V.textTertiary)
                        Text(skill.name.isEmpty ? skill.skill_id : skill.name)
                            .font(V.Font.label).foregroundColor(V.text)
                        Text("v\(skill.version)").font(V.Font.caption)
                            .foregroundColor(V.textTertiary)
                        Spacer()
                        reliability(skill.reliability)
                    }
                    .padding(V.Space.s)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(selected == skill.skill_id ? V.surfaceAlt : Color.clear)
                    .clipShape(RoundedRectangle(cornerRadius: V.Radius.control))
                }
                .buttonStyle(.plain)
            }
        }
        .padding(V.Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(V.surface)
        .clipShape(RoundedRectangle(cornerRadius: V.Radius.card))
    }

    @ViewBuilder
    private func reliability(_ r: APIModels.SkillReliability?) -> some View {
        let runs = r?.runs ?? 0
        let verified = r?.verified_successes ?? 0
        if runs == 0 {
            Text("never run").font(V.Font.caption).foregroundColor(V.textTertiary)
        } else {
            Text("\(runs) run\(runs == 1 ? "" : "s") · \(verified) verified")
                .font(V.Font.caption)
                // Green only for something that verified. A run count is not a
                // success, so "0 verified" is amber, not green.
                .foregroundColor(verified > 0 ? V.success : V.warning)
        }
    }

    // MARK: - The proposal

    @ViewBuilder
    private func proposalCard(_ p: APIModels.EvolutionProposal) -> some View {
        VStack(alignment: .leading, spacing: V.Space.s) {
            HStack(spacing: V.Space.s) {
                Text("PROPOSAL").font(V.Font.caption).foregroundColor(V.textTertiary)
                Text("from v\(p.from_version)").font(V.Font.caption)
                    .foregroundColor(V.textTertiary)
                Spacer()
                if p.changes_anything {
                    Button(accepting ? "Storing…" : "Store as next version") {
                        Task { await accept(p) }
                    }
                    .buttonStyle(VPrimaryButtonStyle())
                    .disabled(accepting)
                }
            }

            if p.changes_anything {
                ForEach(p.changes, id: \.self) { change in
                    HStack(alignment: .firstTextBaseline, spacing: V.Space.s) {
                        Image(systemName: "minus.circle")
                            .font(.system(size: 11)).foregroundColor(V.accent)
                        Text(change).font(V.Font.body).foregroundColor(V.text)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            } else {
                Text("No change is proposed. \(p.evidence?.runs ?? 0) run(s) recorded; "
                     + "nothing they show can be fixed without widening what the skill asks for.")
                    .font(V.Font.body).foregroundColor(V.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            ForEach(p.notes, id: \.self) { note in
                HStack(alignment: .firstTextBaseline, spacing: V.Space.s) {
                    Image(systemName: "info.circle")
                        .font(.system(size: 11)).foregroundColor(V.warning)
                    Text(note).font(V.Font.caption).foregroundColor(V.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }

            if let evidence = p.evidence {
                Divider().overlay(V.surfaceAlt)
                HStack(spacing: V.Space.l) {
                    metric("runs", "\(evidence.runs)")
                    metric("verified", "\(evidence.verified_successes)", V.success)
                    metric("not verified", "\(evidence.failures)", V.warning)
                    metric("unknown", "\(evidence.unknowns)")
                    if let last = evidence.last_verdict, !last.isEmpty {
                        metric("last verdict", last)
                    }
                }
            }
        }
        .padding(V.Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(V.surface)
        .clipShape(RoundedRectangle(cornerRadius: V.Radius.card))
    }

    @ViewBuilder
    private func metric(_ label: String, _ value: String, _ color: Color = V.textTertiary) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(value).font(V.Font.cardTitle).foregroundColor(color)
            Text(label).font(V.Font.caption).foregroundColor(V.textTertiary)
        }
    }

    // MARK: - What the last run actually observed

    @ViewBuilder
    private func observationsCard(_ o: APIModels.EvolutionObservations) -> some View {
        VStack(alignment: .leading, spacing: V.Space.s) {
            Text("LAST RUN — WHAT EACH CONDITION OBSERVED")
                .font(V.Font.caption).foregroundColor(V.textTertiary)
            if o.observations.isEmpty {
                Text(o.note).font(V.Font.caption).foregroundColor(V.textSecondary)
            } else {
                ForEach(o.observations) { condition in
                    HStack(alignment: .firstTextBaseline, spacing: V.Space.s) {
                        Image(systemName: symbol(condition.status))
                            .font(.system(size: 11)).foregroundColor(color(condition.status))
                        VStack(alignment: .leading, spacing: 2) {
                            Text("\(condition.condition_id) · \(condition.statement)")
                                .font(V.Font.label).foregroundColor(V.text)
                                .lineLimit(2)
                            Text(condition.observation.isEmpty
                                 ? "never checked" : condition.observation)
                                .font(V.Font.caption).foregroundColor(V.textTertiary)
                                .lineLimit(2).fixedSize(horizontal: false, vertical: true)
                        }
                        Spacer()
                        Text(condition.status).font(V.Font.caption)
                            .foregroundColor(color(condition.status))
                    }
                    .padding(V.Space.s)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(V.surfaceAlt)
                    .clipShape(RoundedRectangle(cornerRadius: V.Radius.control))
                }
                Text(o.note).font(V.Font.caption).foregroundColor(V.textTertiary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(V.Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(V.surface)
        .clipShape(RoundedRectangle(cornerRadius: V.Radius.card))
    }

    private func symbol(_ status: String) -> String {
        switch status {
        case "verified": return "checkmark.circle.fill"
        case "failed": return "xmark.circle.fill"
        case "pending": return "clock"
        default: return "questionmark.circle"
        }
    }

    private func color(_ status: String) -> Color {
        switch status {
        case "verified": return V.success
        case "failed": return V.danger
        case "pending": return V.warning
        default: return V.textTertiary
        }
    }

    // MARK: - Data

    private func load() async {
        loading = true
        do {
            async let labTask = api.get("/skill-evolution", as: APIModels.EvolutionLab.self)
            async let skillsTask = api.get("/intelligence/skills", as: APIModels.SkillList.self)
            lab = try await labTask
            skills = try await skillsTask.skills
        } catch {
            // A read failure leaves the screen empty rather than showing stale claims.
        }
        loading = false
        // Open on a skill rather than an empty pane. A proposal read is a GET --
        // it changes nothing -- so picking one is safe before anybody asks.
        if selected == nil, let first = skills.first {
            await inspect(first.skill_id)
        }
    }

    private func inspect(_ skillID: String) async {
        selected = skillID
        accepted = nil
        observations = nil
        do {
            async let proposalTask = api.get("/skill-evolution/\(skillID)",
                                             as: APIModels.EvolutionProposal.self)
            async let obsTask = api.get("/skill-evolution/\(skillID)/observations",
                                        as: APIModels.EvolutionObservations.self)
            proposal = try await proposalTask
            observations = try? await obsTask
        } catch {
            proposal = nil
        }
    }

    private func accept(_ p: APIModels.EvolutionProposal) async {
        accepting = true
        do {
            // No manifest is sent: the backend re-derives the proposal it showed
            // and stores that, so the version written is exactly the one on
            // screen rather than a lossy copy echoed back.
            accepted = try await api.post(
                "/skill-evolution/\(p.skill_id)/accept",
                body: AcceptBody(from_version: p.from_version),
                as: APIModels.EvolutionAccept.self)
            await load()
            await inspect(p.skill_id)
        } catch {
            // The backend refuses when the proposal went stale or names no
            // change; the proposal card stays up so the reasons are still read.
        }
        accepting = false
    }
}

/// Names which version this supersedes. The manifest is deliberately omitted:
/// the lab re-derives it, so a client cannot store something it was not shown.
private struct AcceptBody: Codable {
    let from_version: Int
}