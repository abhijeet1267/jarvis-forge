import SwiftUI

/// Agent Control Tower: which workers exist, what they are doing, and controls
/// that actually stop them.
///
/// The screen exists because a system with many agents needs a place to see them
/// all at once and a stop that is real. Its rule is the rule the backend keeps:
/// a pause or a stop takes effect at the **next** dispatch, not inside a call
/// already running. So the boundary note is shown verbatim beside the controls,
/// and a "cancel" is offered as its own action because it is not a pause -- a
/// cancelled worker does not come back on its own.
struct ControlTowerView: View {
    @State private var snapshot: APIModels.TowerSnapshot?
    @State private var loading = false
    @State private var acting: String?
    @State private var errorMessage: String?
    @State private var note: String?

    private let api = APIClient.shared

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: V.Space.l) {
                PageHeader(
                    title: "Agent Control Tower",
                    subtitle: "What every worker can do, what it is doing, and a stop that is real."
                ) {
                    Button(loading ? "Refreshing…" : "Refresh") {
                        Task { await load() }
                    }
                    .buttonStyle(VSecondaryButtonStyle())
                    .disabled(loading)
                }

                if let errorMessage {
                    Text(errorMessage)
                        .font(V.Font.caption).foregroundColor(V.danger)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let note {
                    Text(note)
                        .font(V.Font.caption).foregroundColor(V.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }

                if let snapshot {
                    summaryCard(snapshot)
                    ForEach(snapshot.workers) { worker in
                        workerCard(worker)
                    }
                    Text(snapshot.note)
                        .font(V.Font.caption).foregroundColor(V.textTertiary)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.top, V.Space.xs)
                } else if loading {
                    Text("Loading workers…")
                        .font(V.Font.body).foregroundColor(V.textSecondary)
                }
            }
            .padding(V.Space.xl)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(V.background)
        .task { await load() }
    }

    // MARK: - Summary

    @ViewBuilder
    private func summaryCard(_ snapshot: APIModels.TowerSnapshot) -> some View {
        let s = snapshot.summary
        HStack(spacing: V.Space.l) {
            stat("\(s.total)", "workers", V.text)
            stat("\(s.running)", "running", s.running > 0 ? V.accent : V.textSecondary)
            stat("\(s.paused)", "paused", s.paused > 0 ? V.warning : V.textSecondary)
            stat("\(s.budget_exhausted)", "out of budget",
                 s.budget_exhausted > 0 ? V.warning : V.textSecondary)
            stat("\(s.calls_made)", "calls", V.textSecondary)
            stat("\(s.calls_refused)", "refused",
                 s.calls_refused > 0 ? V.warning : V.textSecondary)
            Spacer()
        }
        .padding(V.Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(V.surface)
        .clipShape(RoundedRectangle(cornerRadius: V.Radius.card))
    }

    private func stat(_ value: String, _ label: String, _ tone: Color) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(value).font(V.Font.pageTitle).foregroundColor(tone)
            Text(label).font(V.Font.caption).foregroundColor(V.textTertiary)
        }
    }

    // MARK: - Worker

    @ViewBuilder
    private func workerCard(_ worker: APIModels.Worker) -> some View {
        VStack(alignment: .leading, spacing: V.Space.s) {
            HStack(alignment: .firstTextBaseline, spacing: V.Space.s) {
                Label(stateLabel(worker.state), systemImage: stateSymbol(worker.state))
                    .font(V.Font.caption).foregroundColor(stateColor(worker.state))
                Text(worker.name).font(V.Font.label).foregroundColor(V.text)
                Text("· \(worker.tool_count) tool(s)")
                    .font(V.Font.caption).foregroundColor(V.textTertiary)
                Spacer()
                budgetLabel(worker)
            }

            if let reason = worker.paused_reason, !reason.isEmpty {
                Text(reason).font(V.Font.caption).foregroundColor(V.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let error = worker.last_error, !error.isEmpty {
                Text("last error: \(error)")
                    .font(V.Font.caption).foregroundColor(V.danger)
                    .lineLimit(2)
            }

            HStack(spacing: V.Space.s) {
                Text(counters(worker))
                    .font(V.Font.mono).foregroundColor(V.textTertiary)
                Spacer()
                if worker.state == "stopped" {
                    Text("Stopped — restart the process to bring it back.")
                        .font(V.Font.caption).foregroundColor(V.textTertiary)
                } else if worker.state == "paused" {
                    actionButton("Resume", key: "resume:\(worker.name)") {
                        await control("/agents/\(worker.name)/resume")
                    }
                } else {
                    actionButton("Pause", key: "pause:\(worker.name)") {
                        await control("/agents/\(worker.name)/pause")
                    }
                }
                if worker.state != "stopped" {
                    actionButton("Stop", key: "cancel:\(worker.name)", tone: V.danger) {
                        await control("/agents/\(worker.name)/cancel")
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
    private func budgetLabel(_ worker: APIModels.Worker) -> some View {
        if let budget = worker.call_budget {
            Text("\(worker.calls_made)/\(budget) calls")
                .font(V.Font.mono).foregroundColor(V.textTertiary)
        } else {
            Text("unbounded").font(V.Font.mono).foregroundColor(V.textTertiary)
        }
    }

    private func counters(_ worker: APIModels.Worker) -> String {
        var parts: [String] = []
        if worker.in_flight > 0 { parts.append("\(worker.in_flight) in flight") }
        parts.append("\(worker.calls_made) made")
        if worker.calls_refused > 0 { parts.append("\(worker.calls_refused) refused") }
        if worker.failures > 0 { parts.append("\(worker.failures) failed") }
        if let ms = worker.last_duration_ms { parts.append("last \(ms) ms") }
        return parts.joined(separator: " · ")
    }

    private func actionButton(_ title: String, key: String,
                              tone: Color = V.accent,
                              work: @escaping () async -> Void) -> some View {
        Button(acting == key ? "Working…" : title) {
            Task { await work() }
        }
        .buttonStyle(VSecondaryButtonStyle())
        .tint(tone)
        .disabled(acting != nil)
    }

    private func stateLabel(_ state: String) -> String {
        switch state {
        case "idle": return "idle"
        case "running": return "running"
        case "paused": return "paused"
        case "budget_exhausted": return "out of budget"
        case "stopped": return "stopped"
        default: return state
        }
    }

    private func stateSymbol(_ state: String) -> String {
        switch state {
        case "idle": return "moon.zzz"
        case "running": return "bolt.fill"
        case "paused": return "pause.circle"
        case "budget_exhausted": return "gauge.with.dots.needle.0percent"
        case "stopped": return "stop.circle"
        default: return "circle"
        }
    }

    private func stateColor(_ state: String) -> Color {
        switch state {
        case "running": return V.accent
        case "paused", "budget_exhausted": return V.warning
        case "stopped": return V.danger
        default: return V.textTertiary
        }
    }

    // MARK: - Data

    private func load() async {
        loading = true
        errorMessage = nil
        do {
            snapshot = try await api.get("/control-tower", as: APIModels.TowerSnapshot.self)
        } catch {
            errorMessage = error.localizedDescription
        }
        loading = false
    }

    private func control(_ path: String) async {
        acting = path
        errorMessage = nil
        note = nil
        do {
            _ = try await api.postEmpty(path)
            await load()
        } catch {
            errorMessage = error.localizedDescription
        }
        acting = nil
    }
}