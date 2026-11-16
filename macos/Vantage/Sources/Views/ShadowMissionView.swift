import SwiftUI

/// ShadowMission: what a plan would do, before anything runs.
///
/// The screen exists to answer one question honestly — "what will this change,
/// and what will stop it" — without performing any of it. Every row states its
/// own verdict, so a step that could not be simulated never borrows the
/// appearance of one that was.
struct ShadowMissionView: View {
    @State private var missions: [APIModels.Mission] = []
    @State private var selected: APIModels.Mission?
    @State private var report: APIModels.ShadowReport?
    @State private var loading = false
    @State private var running = false
    @State private var errorMessage: String?

    private let api = APIClient.shared

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: V.Space.l) {
                PageHeader(
                    title: "ShadowMission",
                    subtitle: "See what a plan would do. Nothing is executed."
                ) {
                    Button(running ? "Shadowing…" : "Shadow it") {
                        Task { await shadow() }
                    }
                    .buttonStyle(VPrimaryButtonStyle())
                    .disabled(selected == nil || running)
                }

                if let errorMessage {
                    Text(errorMessage)
                        .font(V.Font.caption)
                        .foregroundColor(V.warning)
                        .fixedSize(horizontal: false, vertical: true)
                }

                missionPicker

                if let report {
                    reportCard(report)
                } else if selected == nil && !missions.isEmpty {
                    Text("Pick a mission above to see what it would do.")
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
                Text("PLAN")
                    .font(V.Font.caption).foregroundColor(V.textTertiary)
                Spacer()
                Button("Refresh") { Task { await load() } }
                    .buttonStyle(VQuietButtonStyle())
                    .disabled(loading)
            }

            if missions.isEmpty {
                Text(loading ? "Loading…" : "There are no missions to shadow yet. "
                     + "Create one in Mission Control, or record an automation in "
                     + "the Studio and save it.")
                    .font(V.Font.caption).foregroundColor(V.textTertiary)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                // A shadow is most useful before a plan has run, so drafts and
                // planned missions are listed first -- but any mission can be
                // shadowed, because "what would this do again" is a real
                // question too.
                ForEach(missions) { mission in
                    Button {
                        selected = mission
                        report = nil
                        // Shadow on selection: the question "what would this
                        // do" is the whole reason the screen exists, and making
                        // it a second click adds a step that never changes the
                        // answer. Nothing is executed either way.
                        Task { await shadow() }
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
                                Text("\(mission.mission_id) · \(mission.state) · "
                                     + "\(mission.steps?.count ?? 0) step(s)")
                                    .font(V.Font.mono).foregroundColor(V.textTertiary)
                            }
                            Spacer()
                        }
                        .contentShape(Rectangle())
                        .padding(.vertical, 3)
                    }
                    .buttonStyle(.plain)
                    .disabled(running)
                }
            }
        }
        .padding(V.Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(V.surface)
        .clipShape(RoundedRectangle(cornerRadius: V.Radius.card))
    }

    // MARK: - Report

    @ViewBuilder
    private func reportCard(_ report: APIModels.ShadowReport) -> some View {
        VStack(alignment: .leading, spacing: V.Space.m) {
            HStack(alignment: .firstTextBaseline, spacing: V.Space.s) {
                Text("SHADOW")
                    .font(V.Font.caption).foregroundColor(V.textTertiary)
                Spacer()
                Text(summaryLine(report.summary))
                    .font(V.Font.mono).foregroundColor(V.textTertiary)
            }

            Text(report.conclusion)
                .font(V.Font.body).foregroundColor(V.text)
                .fixedSize(horizontal: false, vertical: true)

            ForEach(report.steps) { step in
                stepRow(step)
            }

            Text(report.note)
                .font(V.Font.caption).foregroundColor(V.textTertiary)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.top, V.Space.xs)
        }
        .padding(V.Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(V.surface)
        .clipShape(RoundedRectangle(cornerRadius: V.Radius.card))
    }

    @ViewBuilder
    private func stepRow(_ step: APIModels.ShadowReport.Step) -> some View {
        VStack(alignment: .leading, spacing: V.Space.xs) {
            HStack(alignment: .firstTextBaseline, spacing: V.Space.s) {
                Label(verdictLabel(step.verdict), systemImage: verdictSymbol(step.verdict))
                    .font(V.Font.caption)
                    .foregroundColor(verdictColor(step.verdict))
                Text(step.title).font(V.Font.body).foregroundColor(V.text)
                Spacer()
                Text(step.tool).font(V.Font.mono).foregroundColor(V.textTertiary)
            }

            if let detail = step.detail {
                Text(detail)
                    .font(V.Font.caption).foregroundColor(V.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            // Before/after are only shown when the shadow actually has them. A
            // step that was refused or not simulated has no effect to compare,
            // and drawing empty boxes would imply it did.
            if step.before != nil || step.after != nil {
                VStack(alignment: .leading, spacing: 2) {
                    if let before = step.before {
                        Text("before: \(previewText(before))")
                            .font(V.Font.mono).foregroundColor(V.textTertiary)
                            .lineLimit(3)
                    }
                    if let after = step.after {
                        Text("after:  \(previewText(after))")
                            .font(V.Font.mono).foregroundColor(V.textSecondary)
                            .lineLimit(3)
                    }
                }
                .padding(.leading, V.Space.s)
            }
        }
        .padding(V.Space.s)
        .background(V.surfaceAlt)
        .clipShape(RoundedRectangle(cornerRadius: V.Radius.control))
    }

    private func previewText(_ text: String) -> String {
        if text.isEmpty { return "(empty)" }
        return text.replacingOccurrences(of: "\n", with: "⏎")
    }

    private func summaryLine(_ s: APIModels.ShadowReport.Summary) -> String {
        var parts = ["\(s.steps) step(s)"]
        if s.would_run > 0 { parts.append("\(s.would_run) would run") }
        if s.would_need_approval > 0 { parts.append("\(s.would_need_approval) need approval") }
        if s.would_be_refused > 0 { parts.append("\(s.would_be_refused) refused") }
        if s.not_simulated > 0 { parts.append("\(s.not_simulated) unknown") }
        return parts.joined(separator: " · ")
    }

    private func verdictLabel(_ verdict: String) -> String {
        switch verdict {
        case "would_run": return "would run"
        case "predicted": return "would run (predicted)"
        case "would_need_approval": return "needs your approval"
        case "would_be_refused": return "would be refused"
        case "not_simulated": return "effect unknown"
        default: return verdict
        }
    }

    private func verdictSymbol(_ verdict: String) -> String {
        switch verdict {
        case "would_run", "predicted": return "checkmark.circle"
        case "would_need_approval": return "hand.raised"
        case "would_be_refused": return "xmark.octagon"
        case "not_simulated": return "questionmark.circle"
        default: return "circle"
        }
    }

    private func verdictColor(_ verdict: String) -> Color {
        switch verdict {
        case "would_run": return V.success
        case "predicted": return V.success
        case "would_need_approval": return V.warning
        case "would_be_refused": return V.danger
        case "not_simulated": return V.textSecondary
        default: return V.textTertiary
        }
    }

    // MARK: - Data

    private func load() async {
        loading = true
        errorMessage = nil
        do {
            let response: APIModels.MissionList = try await api.get("/missions?limit=50")
            missions = response.missions
            // Keep a selection that still exists rather than dropping it.
            if let current = selected,
               let fresh = missions.first(where: { $0.mission_id == current.mission_id }) {
                selected = fresh
            } else if selected != nil {
                selected = nil
                report = nil
            }

            // Opening the screen shadows the newest plan rather than showing an
            // empty panel. A shadow executes nothing, so doing it unasked costs
            // the person nothing and answers the question they came for.
            if selected == nil, let first = missions.first {
                selected = first
                await shadow()
            }
        } catch {
            errorMessage = error.localizedDescription
        }
        loading = false
    }

    private func shadow() async {
        guard let mission = selected else { return }
        running = true
        errorMessage = nil
        do {
            report = try await api.post("/missions/\(mission.mission_id)/shadow",
                                        as: APIModels.ShadowReport.self)
        } catch {
            report = nil
            errorMessage = error.localizedDescription
        }
        running = false
    }
}